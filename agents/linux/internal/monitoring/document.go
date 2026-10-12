package monitoring

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
	// Embedded so calendar schedules are correct on a minimal host with no
	// system tzdata installed, rather than silently falling back to UTC.
	// SCHEDULING.md §4 requires this; without it a nightly maintenance window
	// on a container image would run at the wrong hour for half the year.
	_ "time/tzdata"
)

// ChecksumPrefix is on every checksum, so a bare hex digest cannot be mistaken
// for one and a future algorithm change is visible in the data.
const ChecksumPrefix = "sha256:"

// ChecksumLength is the rendered length: the prefix plus 64 hex characters.
const ChecksumLength = len(ChecksumPrefix) + 64

// Schedule is when a module runs.
//
// One struct for both kinds rather than an interface or a union, because a
// configuration document is decoded from JSON whose shape is decided by a
// field — and a Go interface cannot be unmarshalled into without a custom
// decoder that would have to dispatch on Kind anyway. The fields that do not
// apply to a kind are simply absent from the document and stay zero, which
// [Schedule.Validate] checks.
//
// Every duration is an int of **seconds**. Deliberately not time.Duration:
// encoding/json renders a Duration as its nanosecond count, so a 30-second
// interval would go onto the wire as 30000000000 and be read by the controller
// as nine and a half centuries. Conversion happens at the boundary — see
// [Schedule.Interval] — so no wire struct ever holds a Duration.
type Schedule struct {
	Kind string `json:"kind"`

	// Interval.
	IntervalSeconds int `json:"interval_seconds"`
	JitterSeconds   int `json:"jitter_seconds"`

	// Calendar.
	Timezone           string   `json:"timezone"`
	Times              []string `json:"times"`
	DaysOfWeek         []int    `json:"days_of_week"`
	DaysOfMonth        []int    `json:"days_of_month"`
	Months             []int    `json:"months"`
	OnMissed           string   `json:"on_missed"`
	MissedGraceSeconds int      `json:"missed_grace_seconds"`
	DstNonexistent     string   `json:"dst_nonexistent"`
	DstAmbiguous       string   `json:"dst_ambiguous"`
}

// Interval returns the interval as a duration, for the scheduler.
//
// The one place seconds become a Duration, so the conversion exists once and
// cannot be done the wrong way round somewhere else.
func (s Schedule) Interval() time.Duration {
	return time.Duration(s.IntervalSeconds) * time.Second
}

// Jitter returns the jitter as a duration.
func (s Schedule) Jitter() time.Duration {
	return time.Duration(s.JitterSeconds) * time.Second
}

// ModuleConfig is one module's settings, fully resolved by the controller.
//
// There is no "inherit" here and there cannot be: the agent receives a
// resolved document and merges nothing (AGENT_ARCHITECTURE.md §6.1), so
// Enabled is a bool rather than a *bool. A pointer would be a field the agent
// could be asked to interpret, and interpreting it would mean implementing
// layer precedence a second time, in a second language, where it could
// disagree.
type ModuleConfig struct {
	Enabled  bool     `json:"enabled"`
	Schedule Schedule `json:"schedule"`
}

// Limits are the bounds the controller enforces, sent so the agent can
// re-validate while the controller is unreachable.
type Limits struct {
	MinIntervalSeconds int `json:"min_interval_seconds"`
	MaxIntervalSeconds int `json:"max_interval_seconds"`
	// ModuleMinIntervalSeconds is the controller's per-module floor. The agent
	// enforces the stricter of this and its own registry's, so neither a
	// controller that has raised a floor nor an agent that has can be talked
	// past.
	ModuleMinIntervalSeconds map[string]int `json:"module_min_interval_seconds"`
}

// Document is a complete configuration for one host.
type Document struct {
	DocumentVersion int    `json:"document_version"`
	RegistryVersion int    `json:"registry_version"`
	HostID          string `json:"host_id"`
	Version         int64  `json:"version"`
	GeneratedAt     string `json:"generated_at"`

	Modules map[string]ModuleConfig `json:"modules"`
	Limits  Limits                  `json:"limits"`
}

// EnabledModules returns the identifiers of the modules that should collect,
// sorted, so logs and reports are stable between runs.
func (d Document) EnabledModules() []string {
	enabled := make([]string, 0, len(d.Modules))
	for id, module := range d.Modules {
		if module.Enabled {
			enabled = append(enabled, id)
		}
	}
	sort.Strings(enabled)
	return enabled
}

// Checksum computes the digest of the exact bytes a document arrived as.
//
// Takes bytes, not a [Document], and that is the point. Re-serialising a
// decoded document would make the digest depend on Go's encoding/json
// producing byte-identical output to Python's json — which it does not, for
// key order, integer-versus-float rendering and non-ASCII escaping. So the
// agent checksums what it received and never what it reconstructed.
func Checksum(document []byte) string {
	sum := sha256.Sum256(document)
	return ChecksumPrefix + hex.EncodeToString(sum[:])
}

// VerifyChecksum reports whether a document's bytes match a claimed digest.
//
// Compared as a plain string rather than with a constant-time comparison:
// this is an integrity check against corruption and misconfiguration, not a
// secret. There is no secret to leak by timing — the document and its digest
// both arrive in the same frame from the same party — and pretending otherwise
// would suggest this defends against something it does not.
func VerifyChecksum(document []byte, claimed string) bool {
	return len(claimed) == ChecksumLength && Checksum(document) == claimed
}

// Parse decodes and validates a configuration document.
//
// Returns the document, every problem with it, and a decode error. The three
// are distinct outcomes and a caller has to tell them apart:
//
//   - a decode error means the bytes are not a document at all;
//   - problems mean it is a document the agent will not run;
//   - neither means it is usable.
//
// Every problem is returned, not the first. An operator whose configuration
// was refused should get one list rather than discovering the next fault after
// fixing each one — the same rule the controller follows (SCHEDULING.md §10),
// and the reason a NACK carries a slice.
func Parse(raw []byte) (Document, []string, error) {
	var document Document
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	// Unknown fields are refused rather than ignored. A field this build does
	// not know is a controller newer than the agent, and silently dropping it
	// is how an agent comes to believe it has applied a configuration it only
	// partly understood.
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return Document{}, nil, fmt.Errorf("configuration is not a valid document: %w", err)
	}

	return document, document.Validate(), nil
}

// Validate returns every reason this agent will not run a document.
//
// An empty slice means it will. This runs even though the controller validated
// the same document when it was saved, for two reasons: the agent is the
// component that must never act on a configuration it cannot honour, and a
// problem may be *host-specific* — a module this build does not have — which
// the controller could not have known about.
func (d Document) Validate() []string {
	var problems []string

	if d.DocumentVersion != DocumentVersion {
		// Refused rather than best-efforted. A document shape this build does
		// not know may give a field a meaning this build would get wrong, and
		// the safe behaviour is to keep running the last known good one.
		problems = append(problems, fmt.Sprintf(
			"document_version is %d; this agent understands %d. Upgrade the agent.",
			d.DocumentVersion, DocumentVersion,
		))
		// Returned early: every check below reads fields whose meaning depends
		// on the shape, so continuing would produce problems that are
		// artefacts of the mismatch rather than real.
		return problems
	}

	if d.Version < 1 {
		problems = append(problems, fmt.Sprintf("version must be at least 1, got %d", d.Version))
	}
	if d.HostID == "" {
		problems = append(problems, "host_id is missing")
	}
	if len(d.Modules) == 0 {
		// Not the same as "every module disabled", which is the normal state
		// of a fresh host and arrives as five entries with enabled=false. An
		// empty map means the controller said nothing about any module, and
		// applying it would leave the agent unable to tell "off" from "not
		// mentioned".
		problems = append(problems, "modules is empty; expected an entry for every known module")
	}

	ids := make([]string, 0, len(d.Modules))
	for id := range d.Modules {
		ids = append(ids, id)
	}
	// Sorted, so the problem list is deterministic — otherwise two runs on the
	// same bad document would produce differently-ordered rejections and the
	// controller would store whichever arrived.
	sort.Strings(ids)

	for _, id := range ids {
		problems = append(problems, d.validateModule(id, d.Modules[id])...)
	}
	return problems
}

func (d Document) validateModule(id string, config ModuleConfig) []string {
	var problems []string

	descriptor, known := Lookup(id)
	if !known {
		// Only a problem when the module is *enabled*. A document that
		// mentions a module this agent does not have, switched off, costs
		// nothing and refusing it would make every controller upgrade that
		// adds a module break every older agent in the fleet — which is
		// exactly the situation this protocol is versioned to survive.
		if config.Enabled {
			problems = append(problems, fmt.Sprintf(
				"module %q is enabled but this agent does not have it; known: %s",
				id, strings.Join(ModuleIDs(), ", "),
			))
		}
		return problems
	}

	if !config.Enabled {
		// A disabled module's schedule is not run, so an invalid one is not a
		// reason to refuse the whole document. Checking it anyway would mean a
		// stale setting on a module nobody uses could block every other
		// module's configuration.
		return problems
	}

	if !descriptor.Accepts(config.Schedule.Kind) {
		problems = append(problems, fmt.Sprintf(
			"module %q does not accept a %q schedule; permitted: %s",
			id, config.Schedule.Kind, strings.Join(descriptor.ScheduleKinds, ", "),
		))
		return problems
	}

	floor := descriptor.MinIntervalSeconds
	if controllerFloor, ok := d.Limits.ModuleMinIntervalSeconds[id]; ok && controllerFloor > floor {
		// The stricter of the two. Taking the controller's blindly would let a
		// compromised one lower this agent's floor; taking only the agent's
		// would ignore an operator-visible limit the controller is enforcing.
		floor = controllerFloor
	}
	problems = append(problems, config.Schedule.Validate(id, floor, d.Limits)...)
	return problems
}

// Validate returns every reason a schedule cannot be run for a module.
//
// Exported rather than kept private, because the scheduler needs it too: a
// schedule that reaches the scheduler has come from a validated document, but
// the scheduler is also where a *reconfiguration* lands, and re-checking at
// that boundary is cheaper than reasoning about whether every path to it was
// validated. The floor is passed in rather than looked up, so the caller
// decides whose floor applies — see [Document.validateModule], which takes
// the stricter of the agent's and the controller's.
func (s Schedule) Validate(moduleID string, floor int, limits Limits) []string {
	var problems []string

	switch s.Kind {
	case KindInterval:
		ceiling := MaxIntervalSeconds
		if limits.MaxIntervalSeconds > 0 && limits.MaxIntervalSeconds < ceiling {
			ceiling = limits.MaxIntervalSeconds
		}
		if s.IntervalSeconds < floor {
			problems = append(problems, fmt.Sprintf(
				"module %q: interval_seconds is %d, below the minimum of %d",
				moduleID, s.IntervalSeconds, floor,
			))
		}
		if s.IntervalSeconds > ceiling {
			problems = append(problems, fmt.Sprintf(
				"module %q: interval_seconds is %d, above the maximum of %d",
				moduleID, s.IntervalSeconds, ceiling,
			))
		}
		if s.JitterSeconds < 0 {
			problems = append(problems, fmt.Sprintf(
				"module %q: jitter_seconds is negative (%d)", moduleID, s.JitterSeconds,
			))
		} else if s.IntervalSeconds > 0 && s.JitterSeconds >= s.IntervalSeconds {
			problems = append(problems, fmt.Sprintf(
				"module %q: jitter_seconds (%d) must be less than interval_seconds (%d)",
				moduleID, s.JitterSeconds, s.IntervalSeconds,
			))
		}

	case KindCalendar:
		problems = append(problems, s.validateCalendar(moduleID)...)

	case "":
		problems = append(problems, fmt.Sprintf("module %q: schedule has no kind", moduleID))

	default:
		problems = append(problems, fmt.Sprintf(
			"module %q: unknown schedule kind %q", moduleID, s.Kind,
		))
	}

	return problems
}

func (s Schedule) validateCalendar(moduleID string) []string {
	var problems []string

	if s.Timezone == "" {
		problems = append(problems, fmt.Sprintf("module %q: timezone is missing", moduleID))
	} else if _, err := time.LoadLocation(s.Timezone); err != nil {
		problems = append(problems, fmt.Sprintf(
			"module %q: timezone %q is not a known IANA zone name", moduleID, s.Timezone,
		))
	}

	if len(s.Times) == 0 {
		problems = append(problems, fmt.Sprintf("module %q: times is empty", moduleID))
	}
	seen := make(map[string]bool, len(s.Times))
	for _, entry := range s.Times {
		if !validWallClock(entry) {
			problems = append(problems, fmt.Sprintf(
				"module %q: times entry %q is not a valid HH:MM local time", moduleID, entry,
			))
			continue
		}
		if seen[entry] {
			problems = append(problems, fmt.Sprintf(
				"module %q: times contains %q more than once", moduleID, entry,
			))
		}
		seen[entry] = true
	}

	problems = append(problems, validateIntSet(moduleID, "days_of_week", s.DaysOfWeek, 0, 6)...)
	problems = append(problems, validateIntSet(moduleID, "days_of_month", s.DaysOfMonth, 1, 31)...)
	problems = append(problems, validateIntSet(moduleID, "months", s.Months, 1, 12)...)

	switch s.OnMissed {
	case OnMissedSkip, OnMissedRunOnce, "":
	default:
		problems = append(problems, fmt.Sprintf(
			"module %q: unknown on_missed %q", moduleID, s.OnMissed,
		))
	}
	switch s.DstNonexistent {
	case DstShiftForward, DstSkip, "":
	default:
		problems = append(problems, fmt.Sprintf(
			"module %q: unknown dst_nonexistent %q", moduleID, s.DstNonexistent,
		))
	}
	switch s.DstAmbiguous {
	case DstFirst, DstSecond, DstBoth, "":
	default:
		problems = append(problems, fmt.Sprintf(
			"module %q: unknown dst_ambiguous %q", moduleID, s.DstAmbiguous,
		))
	}
	if s.MissedGraceSeconds < 0 {
		problems = append(problems, fmt.Sprintf(
			"module %q: missed_grace_seconds is negative (%d)", moduleID, s.MissedGraceSeconds,
		))
	}

	return problems
}

// Policy values, mirroring docs/SCHEDULING.md §5 and §6.
const (
	OnMissedSkip    = "skip"
	OnMissedRunOnce = "run_once"

	DstShiftForward = "shift_forward"
	DstSkip         = "skip"

	DstFirst  = "first"
	DstSecond = "second"
	DstBoth   = "both"
)

func validateIntSet(moduleID, field string, values []int, low, high int) []string {
	if values == nil {
		// nil means "every value", which is how the wire format spells it. An
		// *empty* slice is different and is refused below: it reads as "no
		// values", and a schedule matching no day can never fire.
		return nil
	}
	if len(values) == 0 {
		return []string{fmt.Sprintf(
			"module %q: %s is an empty list; omit it to mean every value", moduleID, field,
		)}
	}
	var problems []string
	seen := make(map[int]bool, len(values))
	for _, value := range values {
		switch {
		case value < low || value > high:
			problems = append(problems, fmt.Sprintf(
				"module %q: %s entry %d is outside %d-%d", moduleID, field, value, low, high,
			))
		case seen[value]:
			problems = append(problems, fmt.Sprintf(
				"module %q: %s contains %d more than once", moduleID, field, value,
			))
		default:
			seen[value] = true
		}
	}
	return problems
}

// validWallClock reports whether a string is exactly HH:MM with a real hour
// and minute.
//
// Strict about shape as well as range, matching the controller: "3:00" and
// "03:0" are refused so that one instant has one spelling, and two schedules
// that fire at the same moment cannot serialise differently — which would give
// them different checksums.
func validWallClock(value string) bool {
	if len(value) != 5 || value[2] != ':' {
		return false
	}
	hours, err := strconv.Atoi(value[:2])
	if err != nil || hours < 0 || hours > 23 {
		return false
	}
	// Atoi accepts a leading sign, so "+1" would pass the length check and
	// parse. Rejected explicitly, because the controller's own check is on
	// ASCII digits and the two must agree about what a time looks like.
	if !isASCIIDigits(value[:2]) || !isASCIIDigits(value[3:]) {
		return false
	}
	minutes, err := strconv.Atoi(value[3:])
	return err == nil && minutes >= 0 && minutes <= 59
}

func isASCIIDigits(value string) bool {
	for index := 0; index < len(value); index++ {
		if value[index] < '0' || value[index] > '9' {
			return false
		}
	}
	return len(value) > 0
}
