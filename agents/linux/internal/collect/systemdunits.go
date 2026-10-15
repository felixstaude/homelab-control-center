package collect

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// SystemctlPaths are the only binaries this module will run.
//
// Absolute, in a fixed order, compiled in and not configurable — rule 1 and
// rule 5 of [ADR-021]. A `PATH` lookup would let a writable directory
// earlier in the search path hijack the call, which on a machine where the
// agent is the thing watching for trouble is the wrong direction to be
// wrong in.
//
// Two entries because the location genuinely differs: Debian and Ubuntu put
// it in /usr/bin, and some minimal images still have the /bin split.
var SystemctlPaths = []string{"/usr/bin/systemctl", "/bin/systemctl"}

// MaxUnits bounds how many units one host may report.
//
// Lower than the other caps on purpose. The module requires an explicit
// list, so this is a backstop against a pathological configuration rather
// than the mechanism — and an operator naming more than thirty-two services
// is asking a question this product does not answer well.
const MaxUnits = 32

// systemctlTimeout bounds one invocation. Rule 6 of ADR-021.
//
// `systemctl` talks to pid 1 over a socket; a slow answer means systemd is
// in trouble, which is worth reporting as a failed collection rather than
// waiting out.
const systemctlTimeout = 10 * time.Second

// MaxSystemctlOutputBytes bounds what will be read from it. Rule 6.
const MaxSystemctlOutputBytes = 4 << 20

// unitNamePattern is what a unit name may contain.
//
// Rule 2: the only thing substituted into an argument vector is a name from
// the operator's own list, and it is validated first. systemd's own grammar
// allows letters, digits, `:`, `_`, `.`, `-` and `@`; nothing here permits a
// leading dash, which is what would turn a unit name into an option.
var unitNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9:_.@-]{0,127}$`)

// permittedUnitSuffixes are the unit types this module will ask about.
//
// Not every type systemd has. A `.device` or `.scope` unit is kernel or
// session bookkeeping that appears and disappears constantly, and reporting
// one as a series would be unbounded cardinality dressed up as monitoring.
var permittedUnitSuffixes = []string{
	".service", ".socket", ".timer", ".target", ".mount", ".path",
}

// SystemdUnits reports the state of named systemd units.
//
// # It runs `systemctl`, and that needs justifying
//
// systemd has no pseudo-file interface. The alternatives are its D-Bus API,
// which means a D-Bus implementation in an agent whose point is being one
// static binary, or `systemctl`, which is a command — and
// `ARCHITECTURE §10.5` excludes "arbitrary command or shell execution on
// hosts".
//
// The difference is not a technicality, and [ADR-021] is where the rules
// that make it a difference were written down before any module needed them.
// This module satisfies all eight: a fixed allow-list of binaries resolved
// from absolute paths, fixed argument vectors with only validated unit names
// substituted, no shell anywhere, read-only subcommands (`list-units` and
// `show`), a timeout, an output cap, one invocation at a time, and no root.
//
// `systemctl --output=json` is also a *structured* interface rather than a
// human-readable one. That matters: this project does not parse the output
// of `df` or `docker ps`, and the reason is that their columns move between
// implementations. JSON from systemd does not.
//
// # An explicit list is required, and that is the design
//
// A host has hundreds of units. Collecting all of them would be a
// cardinality decision made on the operator's behalf, and nearly every
// series would be a unit nobody has ever thought about. So
// [SystemdUnits.Units] names what to watch, and without it the only thing
// reported is the host-wide failed count — which needs no list, has
// cardinality one, and is the single most useful number systemd can give:
// "is anything broken on this machine".
type SystemdUnits struct {
	// Units is the operator's list, from HCC_AGENT_SYSTEMD_UNITS. Empty
	// means only the host-wide failed count is reported.
	Units []string

	// ProcRoot is where /proc is mounted, for the host's own uptime — which
	// is what turns systemd's monotonic timestamps into a duration.
	ProcRoot string

	// Run executes systemctl. Injected so tests describe a machine's units
	// without needing that machine, and without running anything.
	Run func(ctx context.Context, args ...string) ([]byte, error)
}

// NewSystemdUnits builds a systemd collector.
func NewSystemdUnits(units []string, procRoot string) *SystemdUnits {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	cleaned := make([]string, 0, len(units))
	for _, unit := range units {
		trimmed := strings.TrimSpace(unit)
		if trimmed == "" {
			continue
		}
		// Defaulted here rather than demanded of the operator: `nginx` is
		// what somebody types and `nginx.service` is what systemd calls it.
		if !hasPermittedSuffix(trimmed) && !strings.Contains(trimmed, ".") {
			trimmed += ".service"
		}
		cleaned = append(cleaned, trimmed)
	}
	return &SystemdUnits{Units: cleaned, ProcRoot: procRoot}
}

func hasPermittedSuffix(unit string) bool {
	for _, suffix := range permittedUnitSuffixes {
		if strings.HasSuffix(unit, suffix) {
			return true
		}
	}
	return false
}

// Module implements [Collector].
func (s *SystemdUnits) Module() string { return "systemd" }

// Available implements [Collector].
func (s *SystemdUnits) Available() Availability {
	if _, err := os.Stat(SystemdRuntimeDir); err != nil {
		// The directory systemd itself documents as the test for "am I pid
		// 1 here". Checked rather than running `systemctl is-system-running`,
		// which answers "degraded" on a perfectly usable machine and
		// "offline" inside a container where systemd is present and not
		// booted.
		return Availability{
			Available: false,
			Reason: "systemd is not running as pid 1 on this host, so there are no " +
				"units to report. A container is the usual reason",
		}
	}
	if s.systemctl() == "" {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"systemctl was not found at %s", strings.Join(SystemctlPaths, " or "),
			),
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), systemctlTimeout)
	defer cancel()
	if _, err := s.list(ctx); err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("systemctl could not list units: %v", err),
		}
	}
	return Availability{Available: true}
}

// SystemdRuntimeDir is the directory whose existence means systemd is pid 1.
//
// The same constant internal/install uses, duplicated rather than imported
// so a collector does not depend on the installer. Four words in two places
// is cheaper than that edge.
const SystemdRuntimeDir = "/run/systemd/system"

// Collect implements [Collector].
func (s *SystemdUnits) Collect(ctx context.Context) ([]Sample, error) {
	units, err := s.list(ctx)
	if err != nil {
		return nil, err
	}

	failed := 0
	byName := make(map[string]listedUnit, len(units))
	for _, unit := range units {
		if unit.ActiveState == "failed" {
			failed++
		}
		byName[unit.Unit] = unit
	}

	// The host-wide answer, which needs no list and is the number an
	// operator checks first.
	samples := []Sample{{Metric: "units_failed", Value: float64(failed)}}

	wanted := s.Units
	if len(wanted) > MaxUnits {
		wanted = wanted[:MaxUnits]
	}
	if len(wanted) == 0 {
		return finish(samples), nil
	}

	properties, err := s.show(ctx, wanted)
	if err != nil {
		// The failed count still stands: it came from a call that worked.
		return finish(samples), nil
	}
	bootTime := s.hostUptimeMicroseconds()

	for _, name := range wanted {
		labels := map[string]string{"unit": name}
		detail, known := properties[name]
		listed, listedOK := byName[name]

		// A unit the operator named and systemd does not have. Reported as
		// **absent** rather than as inactive: "this service is stopped" and
		// "this service is not installed" are different problems, and a 0
		// would say the first about the second.
		//
		// Two ways to be absent, and both have to be checked. It can be
		// missing from the output entirely — or, far more often, present
		// with `LoadState=not-found`, because `systemctl show` answers for
		// anything you ask it about.
		if !known && !listedOK {
			continue
		}
		switch detail["LoadState"] {
		case "not-found", "bad-setting", "error":
			continue
		}

		state := detail["ActiveState"]
		if state == "" && listedOK {
			state = listed.ActiveState
		}
		if state == "" {
			continue
		}
		samples = append(samples,
			Sample{Metric: "active", Labels: labels, Value: boolValue(state == "active")},
			Sample{Metric: "failed", Labels: labels, Value: boolValue(state == "failed")},
		)

		if restarts, err := strconv.ParseFloat(detail["NRestarts"], 64); err == nil {
			samples = append(samples, Sample{
				Metric: "restarts", Labels: labels, Value: restarts,
			})
		}

		// Whether the unit is set to start at boot. Absent for `static`,
		// `generated` and `transient`, where the question does not apply —
		// and answering it anyway would mean a `static` unit permanently
		// reporting "not enabled", which reads as a misconfiguration and is
		// not one.
		switch detail["UnitFileState"] {
		case "enabled", "enabled-runtime":
			samples = append(samples, Sample{Metric: "enabled", Labels: labels, Value: 1})
		case "disabled", "masked", "masked-runtime":
			samples = append(samples, Sample{Metric: "enabled", Labels: labels, Value: 0})
		}

		// Monotonic microseconds since boot, which is why the host's own
		// uptime is needed: systemd's human-readable `ActiveEnterTimestamp`
		// is a localised date string, and parsing one of those is exactly
		// what this project does not do.
		if bootTime > 0 && state == "active" {
			if entered, err := strconv.ParseFloat(detail["ActiveEnterTimestampMonotonic"], 64); err == nil && entered > 0 {
				if seconds := (bootTime - entered) / 1_000_000; seconds >= 0 {
					samples = append(samples, Sample{
						Metric: "uptime_seconds", Labels: labels, Value: seconds,
					})
				}
			}
		}
	}

	return finish(samples), nil
}

// finish applies the package's shared validity and ordering rules.
func finish(samples []Sample) []Sample {
	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept
}

// listedUnit is one entry of `systemctl list-units --output=json`.
type listedUnit struct {
	Unit        string `json:"unit"`
	Load        string `json:"load"`
	ActiveState string `json:"active"`
	SubState    string `json:"sub"`
}

// list asks systemd for every unit it knows.
//
// `--output=json` needs systemd 246 (December 2020). An older one answers
// with a table, which does not parse as JSON and so becomes a clear failure
// rather than a column-counting guess that works until a release changes the
// widths.
func (s *SystemdUnits) list(ctx context.Context) ([]listedUnit, error) {
	output, err := s.run(ctx,
		"list-units", "--type=service", "--all", "--output=json", "--no-pager", "--no-legend",
	)
	if err != nil {
		return nil, err
	}
	var units []listedUnit
	if err := json.Unmarshal(output, &units); err != nil {
		return nil, fmt.Errorf(
			"systemctl did not answer with JSON; this needs systemd 246 or newer: %w", err,
		)
	}
	return units, nil
}

// showProperties are the only properties asked for.
//
// Named explicitly rather than taking everything `systemctl show` prints:
// the full set is about two hundred fields per unit including `Environment`,
// which can hold secrets. Asking for four is both cheaper and the difference
// between a monitoring agent and an exfiltration tool.
var showProperties = []string{
	// LoadState is what distinguishes "this service is stopped" from "this
	// service is not installed". `systemctl show` answers for a unit it has
	// never heard of — `LoadState=not-found`, `ActiveState=inactive` — so
	// without this, a typo in the operator's list becomes a series
	// permanently reporting a stopped service that does not exist, with a
	// restart count of zero to make it look measured. Found by running the
	// collector against a name chosen to be absent.
	"Id", "LoadState", "ActiveState", "NRestarts", "UnitFileState",
	"ActiveEnterTimestampMonotonic",
}

// show reads the named units' properties in one invocation.
//
// One process for every unit rather than one each: `systemctl show` accepts
// several units and separates their records with a blank line, so a list of
// thirty costs one fork instead of thirty.
func (s *SystemdUnits) show(ctx context.Context, units []string) (map[string]map[string]string, error) {
	args := []string{"show", "--no-pager"}
	for _, property := range showProperties {
		args = append(args, "--property="+property)
	}
	for _, unit := range units {
		// Rule 2, enforced here: the only operator-supplied value that ever
		// reaches an argument vector, validated against a pattern that
		// cannot produce a leading dash.
		if !unitNamePattern.MatchString(unit) {
			continue
		}
		args = append(args, unit)
	}
	output, err := s.run(ctx, args...)
	if err != nil {
		return nil, err
	}

	// Records separated by a blank line, `Key=Value` within. Keyed by the
	// record's own `Id`, not by position: `systemctl show` omits a record
	// entirely for a unit it has never heard of, so matching by index would
	// attribute one unit's properties to another.
	properties := make(map[string]map[string]string, len(units))
	current := map[string]string{}
	flush := func() {
		if id := current["Id"]; id != "" {
			properties[id] = current
		}
		current = map[string]string{}
	}
	scanner := bufio.NewScanner(bytes.NewReader(output))
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			flush()
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		current[key] = value
	}
	flush()
	return properties, scanner.Err()
}

// hostUptimeMicroseconds reads /proc/uptime, which is what systemd's
// monotonic timestamps are relative to.
func (s *SystemdUnits) hostUptimeMicroseconds() float64 {
	raw, err := os.ReadFile(filepath.Join(s.ProcRoot, "uptime")) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(raw))
	if len(fields) == 0 {
		return 0
	}
	seconds, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0
	}
	return seconds * 1_000_000
}

// systemctl resolves the binary, or "".
func (s *SystemdUnits) systemctl() string {
	for _, candidate := range SystemctlPaths {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
	}
	return ""
}

// run is the one place a process is started. Rules 3, 5 and 6.
func (s *SystemdUnits) run(ctx context.Context, args ...string) ([]byte, error) {
	if s.Run != nil {
		return s.Run(ctx, args...)
	}
	binary := s.systemctl()
	if binary == "" {
		return nil, fmt.Errorf(
			"systemctl was not found at %s", strings.Join(SystemctlPaths, " or "),
		)
	}
	ctx, cancel := context.WithTimeout(ctx, systemctlTimeout)
	defer cancel()

	// exec with a slice. No shell, so there is no quoting or injection
	// surface — rule 3, and the reason the unit-name pattern above is a
	// second line rather than the only one.
	command := exec.CommandContext(ctx, binary, args...) //nolint:gosec // a compiled-in absolute path with a fixed argv
	// An empty environment except for a C locale: `systemctl` formats some
	// output by locale, and a monitoring agent should read the same bytes on
	// every machine regardless of who installed it.
	command.Env = []string{"LC_ALL=C", "LANG=C"}
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	if stdout.Len() > MaxSystemctlOutputBytes {
		return nil, fmt.Errorf("systemctl produced more than %d bytes", MaxSystemctlOutputBytes)
	}
	if err != nil {
		detail := strings.TrimSpace(stderr.String())
		if len(detail) > 200 {
			detail = detail[:200]
		}
		return nil, fmt.Errorf("systemctl %s: %w (%s)", args[0], err, detail)
	}
	return stdout.Bytes(), nil
}
