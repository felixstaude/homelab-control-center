package collect_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
)

// A fake `systemctl`.
//
// Injected rather than run for real, because the machine running `go test`
// has its own units and its own systemd version — so a test against the real
// binary could assert almost nothing, which is the same reason the /proc
// collectors read fixtures.
//
// The *invocations* are recorded, because the properties this module has to
// hold are about the argument vectors as much as about the parsing: a fixed
// argv, no shell, an explicit property list, and nothing operator-supplied
// that could become an option.
type fakeSystemctl struct {
	listJSON   string
	showOutput string
	listErr    error
	showErr    error
	calls      [][]string
}

func (f *fakeSystemctl) run(_ context.Context, args ...string) ([]byte, error) {
	f.calls = append(f.calls, args)
	switch args[0] {
	case "list-units":
		if f.listErr != nil {
			return nil, f.listErr
		}
		return []byte(f.listJSON), nil
	case "show":
		if f.showErr != nil {
			return nil, f.showErr
		}
		return []byte(f.showOutput), nil
	default:
		return nil, fmt.Errorf("unexpected subcommand %q", args[0])
	}
}

// systemdFixture wires a collector onto a fake systemctl and a /proc/uptime.
func systemdFixture(t *testing.T, units []string, fake *fakeSystemctl) *collect.SystemdUnits {
	t.Helper()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "uptime"), []byte("100000.00 900000.00\n"), 0o644); err != nil {
		t.Fatalf("cannot write the uptime fixture: %v", err)
	}
	collector := collect.NewSystemdUnits(units, root)
	collector.Run = fake.run
	return collector
}

const twoFailedUnits = `[
  {"unit":"nginx.service","load":"loaded","active":"active","sub":"running"},
  {"unit":"postgresql.service","load":"loaded","active":"failed","sub":"failed"},
  {"unit":"cups.service","load":"loaded","active":"failed","sub":"failed"},
  {"unit":"ssh.service","load":"loaded","active":"inactive","sub":"dead"}
]`

// show output for nginx and postgresql, records separated by a blank line.
// 100000 s of host uptime is 100_000_000_000 µs; nginx entered at
// 40_000_000_000 µs, so it has been up 60 000 s.
const twoUnitsShown = `Id=nginx.service
LoadState=loaded
ActiveState=active
NRestarts=2
UnitFileState=enabled
ActiveEnterTimestampMonotonic=40000000000

Id=postgresql.service
LoadState=loaded
ActiveState=failed
NRestarts=7
UnitFileState=enabled
ActiveEnterTimestampMonotonic=0
`

func TestTheFailedCountNeedsNoUnitList(t *testing.T) {
	// The single most useful number systemd can give — "is anything broken
	// on this machine" — has cardinality one and needs no configuration. A
	// module that reported nothing until an operator typed a list would be
	// a module most hosts never benefit from.
	fake := &fakeSystemctl{listJSON: twoFailedUnits}
	collector := systemdFixture(t, nil, fake)

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	value, found := labelled(samples, "units_failed", "", "")
	if !found {
		t.Fatalf("no units_failed: %v", samples)
	}
	if value != 2 {
		t.Errorf("units_failed = %v, want 2", value)
	}
	// And nothing else, because nothing was asked for.
	for _, sample := range samples {
		if len(sample.Labels) != 0 {
			t.Errorf("a per-unit series was produced with no list: %+v", sample)
		}
	}
	for _, call := range fake.calls {
		if call[0] == "show" {
			t.Error("systemctl show was invoked with no units to show")
		}
	}
}

func TestNamedUnitsAreReportedWithTheirState(t *testing.T) {
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"nginx", "postgresql.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if value, _ := labelled(samples, "active", "unit", "nginx.service"); value != 1 {
		t.Errorf("active(nginx) = %v, want 1", value)
	}
	if value, _ := labelled(samples, "failed", "unit", "nginx.service"); value != 0 {
		t.Errorf("failed(nginx) = %v, want 0", value)
	}
	if value, _ := labelled(samples, "active", "unit", "postgresql.service"); value != 0 {
		t.Errorf("active(postgresql) = %v, want 0", value)
	}
	if value, _ := labelled(samples, "failed", "unit", "postgresql.service"); value != 1 {
		t.Errorf("failed(postgresql) = %v, want 1", value)
	}
	if value, _ := labelled(samples, "restarts", "unit", "postgresql.service"); value != 7 {
		t.Errorf("restarts(postgresql) = %v, want 7", value)
	}
}

func TestABareNameBecomesAServiceUnit(t *testing.T) {
	// `nginx` is what somebody types; `nginx.service` is what systemd calls
	// it. Making the operator know that would be a configuration file full
	// of typos.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"nginx"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "active", "unit", "nginx.service"); !found {
		t.Errorf("a bare name did not resolve to a service unit: %v", samples)
	}
}

func TestInactiveAndFailedAreDifferentSeries(t *testing.T) {
	// A unit an operator stopped on purpose and a unit that died are
	// different facts. One metric with three values would need the
	// interface to know an encoding, which is where a 2 quietly becomes
	// "unknown" in one place and "failed" in another.
	fake := &fakeSystemctl{
		listJSON: twoFailedUnits,
		showOutput: `Id=ssh.service
LoadState=loaded
ActiveState=inactive
NRestarts=0
UnitFileState=disabled
ActiveEnterTimestampMonotonic=0
`,
	}
	collector := systemdFixture(t, []string{"ssh.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	active, _ := labelled(samples, "active", "unit", "ssh.service")
	failed, _ := labelled(samples, "failed", "unit", "ssh.service")
	if active != 0 || failed != 0 {
		t.Errorf("a deliberately stopped unit reports active=%v failed=%v; want 0 and 0",
			active, failed)
	}
}

func TestAUnitThatDoesNotExistIsAbsentRatherThanStopped(t *testing.T) {
	// The defect this test exists for, found by running the collector
	// against a name chosen to be absent: `systemctl show` answers for a
	// unit it has never heard of, with `LoadState=not-found` and
	// `ActiveState=inactive`. Without checking LoadState, a typo in the
	// operator's list becomes a series permanently reporting a stopped
	// service that does not exist — with a restart count of zero, to make
	// it look measured.
	fake := &fakeSystemctl{
		listJSON: twoFailedUnits,
		showOutput: `Id=typo.service
LoadState=not-found
ActiveState=inactive
NRestarts=0
UnitFileState=
ActiveEnterTimestampMonotonic=0
`,
	}
	collector := systemdFixture(t, []string{"typo.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, metric := range []string{"active", "failed", "restarts"} {
		if _, found := labelled(samples, metric, "unit", "typo.service"); found {
			t.Errorf("a unit that does not exist reported %s", metric)
		}
	}
	// The host-wide count is still there: it came from a call that worked.
	if _, found := labelled(samples, "units_failed", "", ""); !found {
		t.Error("the failed count was lost along with the missing unit")
	}
}

func TestEnabledIsAbsentForUnitsWhereTheQuestionDoesNotApply(t *testing.T) {
	// A `static` unit cannot be enabled or disabled. Reporting 0 would make
	// it permanently look like a misconfiguration.
	fake := &fakeSystemctl{
		listJSON: twoFailedUnits,
		showOutput: `Id=dbus.service
LoadState=loaded
ActiveState=active
NRestarts=0
UnitFileState=static
ActiveEnterTimestampMonotonic=40000000000
`,
	}
	collector := systemdFixture(t, []string{"dbus.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "enabled", "unit", "dbus.service"); found {
		t.Error("a static unit was given an enabled-at-boot value")
	}
	if _, found := labelled(samples, "active", "unit", "dbus.service"); !found {
		t.Error("the rest of the unit's state was lost with it")
	}
}

func TestUptimeComesFromTheMonotonicTimestampAndTheHostsOwnUptime(t *testing.T) {
	// systemd's human-readable `ActiveEnterTimestamp` is a localised date
	// string, and parsing one of those is exactly what this project does
	// not do. The monotonic form is microseconds since boot, which needs
	// the host's uptime to become a duration.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	value, found := labelled(samples, "uptime_seconds", "unit", "nginx.service")
	if !found {
		t.Fatalf("no uptime: %v", samples)
	}
	// 100 000 s of host uptime minus 40 000 s since boot.
	if value != 60_000 {
		t.Errorf("uptime = %v, want 60000", value)
	}
}

func TestAFailedUnitHasNoUptime(t *testing.T) {
	// "Running for 0 seconds" about something that is not running is a
	// measurement of nothing.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"postgresql.service"}, fake)

	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "uptime_seconds", "unit", "postgresql.service"); found {
		t.Error("a failed unit was given an uptime")
	}
}

// --- The argument vectors ---------------------------------------------------

func TestOnlyReadOnlySubcommandsAreInvoked(t *testing.T) {
	// ADR-021 rule 4. Asserted against the argv the module actually built,
	// so a future `systemctl restart` is a failing test rather than a code
	// review somebody skipped.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	permitted := map[string]bool{"list-units": true, "show": true}
	for _, call := range fake.calls {
		if !permitted[call[0]] {
			t.Errorf("the module invoked `systemctl %s`", call[0])
		}
	}
}

func TestOnlyNamedPropertiesAreRequested(t *testing.T) {
	// `systemctl show` with no --property prints around two hundred fields
	// per unit, including Environment — which can hold secrets. Asking for
	// six is both cheaper and the difference between a monitoring agent and
	// an exfiltration tool.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, call := range fake.calls {
		if call[0] != "show" {
			continue
		}
		properties := 0
		for _, argument := range call {
			if strings.HasPrefix(argument, "--property=") {
				properties++
			}
			if argument == "--all" {
				t.Error("systemctl show was asked for every property")
			}
		}
		if properties == 0 {
			t.Error("systemctl show was invoked with no --property, which prints everything")
		}
	}
}

func TestAUnitNameThatCouldBecomeAnOptionIsRefused(t *testing.T) {
	// ADR-021 rule 2. The unit list is the only operator-supplied value
	// that reaches an argument vector, and a name beginning with a dash
	// would be read by systemctl as an option rather than a unit.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, []string{"--version", "nginx.service", "a b.service"}, fake)

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, call := range fake.calls {
		for _, argument := range call[1:] {
			if argument == "--version" {
				t.Error("a unit name beginning with a dash reached the argument vector")
			}
			if argument == "a b.service" {
				t.Error("a unit name containing a space reached the argument vector")
			}
		}
	}
}

func TestTheUnitCountIsCapped(t *testing.T) {
	names := make([]string, 0, collect.MaxUnits+10)
	for index := 0; index < collect.MaxUnits+10; index++ {
		names = append(names, fmt.Sprintf("app%d.service", index))
	}
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showOutput: twoUnitsShown}
	collector := systemdFixture(t, names, fake)

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, call := range fake.calls {
		if call[0] != "show" {
			continue
		}
		units := 0
		for _, argument := range call[1:] {
			if !strings.HasPrefix(argument, "--") {
				units++
			}
		}
		if units > collect.MaxUnits {
			t.Errorf("systemctl show was given %d units, over the %d cap", units, collect.MaxUnits)
		}
	}
}

// --- Failure --------------------------------------------------------------

func TestAnOldSystemdIsAClearRefusalRatherThanAParsingGuess(t *testing.T) {
	// --output=json needs systemd 246. An older one answers with a table,
	// and counting its columns would work until a release changed the
	// widths — the exact failure mode that makes this project refuse to
	// parse human-readable output.
	fake := &fakeSystemctl{listJSON: "UNIT LOAD ACTIVE SUB DESCRIPTION\nnginx.service loaded active running nginx\n"}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)

	_, err := collector.Collect(context.Background())

	if err == nil {
		t.Fatal("a table was accepted as JSON")
	}
	if !strings.Contains(err.Error(), "246") {
		t.Errorf("the error does not name the requirement: %v", err)
	}
}

func TestAFailingShowKeepsTheFailedCount(t *testing.T) {
	// The list worked and the show did not. The host-wide answer is still
	// true and is the one most worth having.
	fake := &fakeSystemctl{listJSON: twoFailedUnits, showErr: errors.New("exit status 1")}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("a failing show failed the whole collection: %v", err)
	}
	if value, _ := labelled(samples, "units_failed", "", ""); value != 2 {
		t.Errorf("units_failed = %v, want 2", value)
	}
}

func TestAFailingListFailsTheCollection(t *testing.T) {
	// Nothing is known. Reporting zero failed units would be the opposite
	// of the truth on a host where systemd is in trouble.
	fake := &fakeSystemctl{listErr: errors.New("Failed to connect to bus")}
	collector := systemdFixture(t, []string{"nginx.service"}, fake)

	if _, err := collector.Collect(context.Background()); err == nil {
		t.Fatal("a broken systemd produced a successful collection")
	}
}

func TestTheSystemdCollectorIdentifiesItself(t *testing.T) {
	if got := collect.NewSystemdUnits(nil, "").Module(); got != "systemd" {
		t.Errorf("systemd module = %q", got)
	}
}
