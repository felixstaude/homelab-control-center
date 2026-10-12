package monitoring_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// validDocument is a document the agent will run: every module present, all
// disabled, which is exactly what a freshly enrolled host receives.
func validDocument() map[string]any {
	modules := map[string]any{}
	for _, id := range monitoring.ModuleIDs() {
		descriptor, _ := monitoring.Lookup(id)
		modules[id] = map[string]any{
			"enabled": false,
			"schedule": map[string]any{
				"kind":             "interval",
				"interval_seconds": descriptor.MinIntervalSeconds,
				"jitter_seconds":   0,
			},
		}
	}
	return map[string]any{
		"document_version": monitoring.DocumentVersion,
		"registry_version": monitoring.RegistryVersion,
		"host_id":          "11111111-2222-3333-4444-555555555555",
		"version":          1,
		"generated_at":     "2026-10-12T12:00:00+00:00",
		"modules":          modules,
		"limits": map[string]any{
			"min_interval_seconds":        monitoring.MinIntervalSeconds,
			"max_interval_seconds":        monitoring.MaxIntervalSeconds,
			"module_min_interval_seconds": map[string]int{},
		},
	}
}

func encode(t *testing.T, document map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(document)
	if err != nil {
		t.Fatalf("cannot encode the test document: %v", err)
	}
	return raw
}

func parse(t *testing.T, document map[string]any) (monitoring.Document, []string) {
	t.Helper()
	parsed, problems, err := monitoring.Parse(encode(t, document))
	if err != nil {
		t.Fatalf("unexpected decode error: %v", err)
	}
	return parsed, problems
}

func setModule(document map[string]any, id string, config map[string]any) {
	document["modules"].(map[string]any)[id] = config
}

func enabledInterval(seconds int) map[string]any {
	return map[string]any{
		"enabled": true,
		"schedule": map[string]any{
			"kind":             "interval",
			"interval_seconds": seconds,
			"jitter_seconds":   0,
		},
	}
}

// ---------------------------------------------------------------------------
// Checksums
// ---------------------------------------------------------------------------

func TestChecksumIsOverBytesNotAReserialisation(t *testing.T) {
	// Two spellings of the same object. Their digests differ, which is the
	// whole reason the agent checksums what it received: if it decoded and
	// re-encoded first, these would collide and a tampered document could be
	// accepted under the digest of the original.
	left := []byte(`{"a":1,"b":2}`)
	right := []byte(`{"b":2,"a":1}`)
	if monitoring.Checksum(left) == monitoring.Checksum(right) {
		t.Fatal("two different byte sequences produced the same checksum")
	}
}

func TestChecksumIsPrefixedAndTheRightLength(t *testing.T) {
	sum := monitoring.Checksum([]byte("anything"))
	if !strings.HasPrefix(sum, monitoring.ChecksumPrefix) {
		t.Errorf("checksum %q is not prefixed", sum)
	}
	if len(sum) != monitoring.ChecksumLength {
		t.Errorf("checksum is %d characters, expected %d", len(sum), monitoring.ChecksumLength)
	}
}

func TestVerifyChecksumAcceptsTheRightDigest(t *testing.T) {
	document := []byte(`{"version":1}`)
	if !monitoring.VerifyChecksum(document, monitoring.Checksum(document)) {
		t.Fatal("a document did not verify against its own checksum")
	}
}

func TestVerifyChecksumRefusesAnythingElse(t *testing.T) {
	document := []byte(`{"version":1}`)
	for name, claimed := range map[string]string{
		"empty":        "",
		"bare digest":  strings.TrimPrefix(monitoring.Checksum(document), monitoring.ChecksumPrefix),
		"wrong digest": monitoring.ChecksumPrefix + strings.Repeat("a", 64),
		"truncated":    monitoring.Checksum(document)[:40],
		"padded":       monitoring.Checksum(document) + "a",
	} {
		if monitoring.VerifyChecksum(document, claimed) {
			t.Errorf("%s: accepted %q", name, claimed)
		}
	}
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

func TestAValidDocumentParsesWithNoProblems(t *testing.T) {
	document, problems := parse(t, validDocument())
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if document.Version != 1 {
		t.Errorf("version is %d, expected 1", document.Version)
	}
	if len(document.Modules) != len(monitoring.ModuleIDs()) {
		t.Errorf("got %d modules, expected %d", len(document.Modules), len(monitoring.ModuleIDs()))
	}
	if enabled := document.EnabledModules(); len(enabled) != 0 {
		t.Errorf("a fresh document enabled %v; it must enable nothing", enabled)
	}
}

func TestNotJsonIsADecodeErrorRatherThanAProblem(t *testing.T) {
	// The distinction matters to the caller: a decode error means the bytes
	// are not a document, which is a different report from "this document is
	// one I will not run".
	_, problems, err := monitoring.Parse([]byte("not json at all"))
	if err == nil {
		t.Fatal("expected a decode error")
	}
	if problems != nil {
		t.Errorf("a decode error should carry no problems, got %v", problems)
	}
}

func TestAnUnknownFieldIsRefusedRatherThanIgnored(t *testing.T) {
	// A field this build does not know means a controller newer than the
	// agent, and silently dropping it is how an agent comes to believe it
	// applied a configuration it only partly understood.
	document := validDocument()
	document["collect_everything"] = true
	_, _, err := monitoring.Parse(encode(t, document))
	if err == nil {
		t.Fatal("expected an unknown field to be refused")
	}
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

func TestAFutureDocumentVersionIsRefused(t *testing.T) {
	document := validDocument()
	document["document_version"] = monitoring.DocumentVersion + 1
	_, problems := parse(t, document)
	if len(problems) != 1 {
		t.Fatalf("expected exactly one problem, got %v", problems)
	}
	if !strings.Contains(problems[0], "Upgrade the agent") {
		t.Errorf("the problem should name the remedy, got %q", problems[0])
	}
}

func TestAFutureDocumentVersionStopsFurtherChecks(t *testing.T) {
	// Every later check reads fields whose meaning depends on the shape, so
	// continuing would produce problems that are artefacts of the mismatch
	// rather than real — and the operator would be sent to fix them.
	document := validDocument()
	document["document_version"] = 99
	document["version"] = 0
	document["host_id"] = ""
	_, problems := parse(t, document)
	if len(problems) != 1 {
		t.Fatalf("expected only the version problem, got %v", problems)
	}
}

func TestAVersionBelowOneIsRefused(t *testing.T) {
	document := validDocument()
	document["version"] = 0
	_, problems := parse(t, document)
	if !containsSubstring(problems, "version must be at least 1") {
		t.Errorf("expected a version problem, got %v", problems)
	}
}

func TestAMissingHostIdIsRefused(t *testing.T) {
	document := validDocument()
	document["host_id"] = ""
	_, problems := parse(t, document)
	if !containsSubstring(problems, "host_id is missing") {
		t.Errorf("expected a host_id problem, got %v", problems)
	}
}

func TestAnEmptyModuleMapIsRefused(t *testing.T) {
	// Not the same as "every module disabled", which arrives as five entries
	// with enabled=false. An empty map means the controller said nothing about
	// any module, and the agent could not tell "off" from "not mentioned".
	document := validDocument()
	document["modules"] = map[string]any{}
	_, problems := parse(t, document)
	if !containsSubstring(problems, "modules is empty") {
		t.Errorf("expected an empty-modules problem, got %v", problems)
	}
}

func TestAnIntervalBelowTheModuleFloorIsRefused(t *testing.T) {
	document := validDocument()
	// load's floor is 5 seconds.
	setModule(document, "load", enabledInterval(1))
	_, problems := parse(t, document)
	if !containsSubstring(problems, "below the minimum of 5") {
		t.Errorf("expected an interval-floor problem, got %v", problems)
	}
}

func TestTheStricterOfTheTwoFloorsApplies(t *testing.T) {
	// The controller raising a floor must bind even though the agent's own
	// registry is laxer. The reverse is tested below.
	document := validDocument()
	setModule(document, "cpu", enabledInterval(2))
	document["limits"].(map[string]any)["module_min_interval_seconds"] = map[string]int{"cpu": 30}
	_, problems := parse(t, document)
	if !containsSubstring(problems, "below the minimum of 30") {
		t.Errorf("expected the controller's floor to bind, got %v", problems)
	}
}

func TestAControllerCannotLowerTheAgentsFloor(t *testing.T) {
	// The other direction, and the one that is a security property rather
	// than a convenience: a compromised controller must not be able to
	// instruct an agent to sample faster than this build permits.
	document := validDocument()
	setModule(document, "load", enabledInterval(1))
	document["limits"].(map[string]any)["module_min_interval_seconds"] = map[string]int{"load": 1}
	_, problems := parse(t, document)
	if !containsSubstring(problems, "below the minimum of 5") {
		t.Errorf("the agent's own floor should still bind, got %v", problems)
	}
}

func TestAnIntervalAboveTheCeilingIsRefused(t *testing.T) {
	document := validDocument()
	setModule(document, "cpu", enabledInterval(monitoring.MaxIntervalSeconds+1))
	_, problems := parse(t, document)
	if !containsSubstring(problems, "above the maximum") {
		t.Errorf("expected a ceiling problem, got %v", problems)
	}
}

func TestJitterAtOrAboveTheIntervalIsRefused(t *testing.T) {
	document := validDocument()
	setModule(document, "cpu", map[string]any{
		"enabled": true,
		"schedule": map[string]any{
			"kind":             "interval",
			"interval_seconds": 30,
			"jitter_seconds":   30,
		},
	})
	_, problems := parse(t, document)
	if !containsSubstring(problems, "must be less than interval_seconds") {
		t.Errorf("expected a jitter problem, got %v", problems)
	}
}

func TestACalendarScheduleOnASamplerIsRefused(t *testing.T) {
	document := validDocument()
	setModule(document, "cpu", map[string]any{
		"enabled": true,
		"schedule": map[string]any{
			"kind":     "calendar",
			"timezone": "UTC",
			"times":    []string{"03:00"},
		},
	})
	_, problems := parse(t, document)
	if !containsSubstring(problems, `does not accept a "calendar" schedule`) {
		t.Errorf("expected a schedule-kind problem, got %v", problems)
	}
}

func TestAnUnknownScheduleKindIsRefused(t *testing.T) {
	document := validDocument()
	setModule(document, "cpu", map[string]any{
		"enabled":  true,
		"schedule": map[string]any{"kind": "whenever"},
	})
	_, problems := parse(t, document)
	if !containsSubstring(problems, "whenever") {
		t.Errorf("expected an unknown-kind problem, got %v", problems)
	}
}

func TestAnEnabledUnknownModuleIsRefused(t *testing.T) {
	document := validDocument()
	setModule(document, "docker", enabledInterval(10))
	_, problems := parse(t, document)
	if !containsSubstring(problems, "this agent does not have it") {
		t.Errorf("expected an unknown-module problem, got %v", problems)
	}
	if !containsSubstring(problems, "cpu") {
		t.Errorf("the problem should name what the agent does have, got %v", problems)
	}
}

func TestADisabledUnknownModuleIsAccepted(t *testing.T) {
	// Otherwise every controller upgrade that adds a module would break every
	// older agent in the fleet — which is exactly what the protocol is
	// versioned to survive.
	document := validDocument()
	setModule(document, "docker", map[string]any{
		"enabled":  false,
		"schedule": map[string]any{"kind": "interval", "interval_seconds": 10},
	})
	_, problems := parse(t, document)
	if len(problems) != 0 {
		t.Fatalf("a disabled unknown module should be accepted, got %v", problems)
	}
}

func TestADisabledModulesInvalidScheduleIsNotAProblem(t *testing.T) {
	// A disabled module's schedule is not run, so a stale setting on a module
	// nobody uses must not block every other module's configuration.
	document := validDocument()
	setModule(document, "cpu", map[string]any{
		"enabled":  false,
		"schedule": map[string]any{"kind": "interval", "interval_seconds": 0},
	})
	_, problems := parse(t, document)
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
}

func TestEveryProblemIsReportedNotTheFirst(t *testing.T) {
	document := validDocument()
	document["version"] = 0
	document["host_id"] = ""
	setModule(document, "load", enabledInterval(1))
	setModule(document, "memory", enabledInterval(0))
	_, problems := parse(t, document)
	if len(problems) < 4 {
		t.Fatalf("expected at least four problems, got %v", problems)
	}
}

func TestProblemsAreDeterministicallyOrdered(t *testing.T) {
	// Go map iteration is randomised, so two runs on the same bad document
	// would otherwise produce differently-ordered rejections and the
	// controller would store whichever arrived.
	document := validDocument()
	for _, id := range []string{"cpu", "memory", "swap", "load"} {
		setModule(document, id, enabledInterval(0))
	}
	raw := encode(t, document)
	_, first, _ := monitoring.Parse(raw)
	for attempt := 0; attempt < 20; attempt++ {
		_, again, _ := monitoring.Parse(raw)
		if strings.Join(again, "|") != strings.Join(first, "|") {
			t.Fatalf("problem order differed between runs:\n%v\n%v", first, again)
		}
	}
}

// ---------------------------------------------------------------------------
// Calendar validation
// ---------------------------------------------------------------------------

// calendarOn builds a document with a calendar schedule on a module that
// accepts one.
//
// No module in this phase does, so the schedule is attached to a *disabled*
// module and validated directly instead. That keeps the calendar rules tested
// without pretending a module accepts a kind it does not — which would be a
// fixture asserting something untrue about the registry.
func calendarSchedule(extra map[string]any) monitoring.Schedule {
	schedule := monitoring.Schedule{
		Kind:     monitoring.KindCalendar,
		Timezone: "Europe/Berlin",
		Times:    []string{"03:00"},
	}
	if value, ok := extra["timezone"].(string); ok {
		schedule.Timezone = value
	}
	if value, ok := extra["times"].([]string); ok {
		schedule.Times = value
	}
	if value, ok := extra["days_of_week"].([]int); ok {
		schedule.DaysOfWeek = value
	}
	if value, ok := extra["months"].([]int); ok {
		schedule.Months = value
	}
	if value, ok := extra["on_missed"].(string); ok {
		schedule.OnMissed = value
	}
	if value, ok := extra["dst_ambiguous"].(string); ok {
		schedule.DstAmbiguous = value
	}
	if value, ok := extra["missed_grace_seconds"].(int); ok {
		schedule.MissedGraceSeconds = value
	}
	return schedule
}

func TestAValidCalendarScheduleHasNoProblems(t *testing.T) {
	if problems := calendarSchedule(nil).Validate("test", 1, monitoring.Limits{}); len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
}

func TestCalendarValidationRejectsTheDocumentedCases(t *testing.T) {
	cases := map[string]struct {
		schedule monitoring.Schedule
		contains string
	}{
		"unknown timezone": {
			calendarSchedule(map[string]any{"timezone": "Not/AZone"}),
			"not a known IANA zone",
		},
		"empty timezone": {
			calendarSchedule(map[string]any{"timezone": ""}),
			"timezone is missing",
		},
		"no times": {
			calendarSchedule(map[string]any{"times": []string{}}),
			"times is empty",
		},
		"malformed time": {
			calendarSchedule(map[string]any{"times": []string{"3:00"}}),
			"not a valid HH:MM",
		},
		"out of range hour": {
			calendarSchedule(map[string]any{"times": []string{"24:00"}}),
			"not a valid HH:MM",
		},
		"signed time": {
			calendarSchedule(map[string]any{"times": []string{"+1:00"}}),
			"not a valid HH:MM",
		},
		"duplicate time": {
			calendarSchedule(map[string]any{"times": []string{"03:00", "03:00"}}),
			"more than once",
		},
		"weekday out of range": {
			calendarSchedule(map[string]any{"days_of_week": []int{7}}),
			"outside 0-6",
		},
		"empty weekday list": {
			calendarSchedule(map[string]any{"days_of_week": []int{}}),
			"omit it to mean every value",
		},
		"month out of range": {
			calendarSchedule(map[string]any{"months": []int{13}}),
			"outside 1-12",
		},
		"unknown on_missed": {
			calendarSchedule(map[string]any{"on_missed": "maybe"}),
			"unknown on_missed",
		},
		"unknown dst_ambiguous": {
			calendarSchedule(map[string]any{"dst_ambiguous": "whichever"}),
			"unknown dst_ambiguous",
		},
		"negative grace": {
			calendarSchedule(map[string]any{"missed_grace_seconds": -1}),
			"missed_grace_seconds is negative",
		},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			problems := testCase.schedule.Validate("test", 1, monitoring.Limits{})
			if !containsSubstring(problems, testCase.contains) {
				t.Errorf("expected a problem containing %q, got %v", testCase.contains, problems)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Durations
// ---------------------------------------------------------------------------

func TestSecondsBecomeDurationsExactlyOnce(t *testing.T) {
	// The trap this guards: encoding/json renders a time.Duration as its
	// nanosecond count, so a wire struct holding one would put 30000000000 on
	// the wire for thirty seconds. The conversion lives in one method.
	schedule := monitoring.Schedule{IntervalSeconds: 30, JitterSeconds: 3}
	if schedule.Interval().Seconds() != 30 {
		t.Errorf("interval is %s, expected 30s", schedule.Interval())
	}
	if schedule.Jitter().Seconds() != 3 {
		t.Errorf("jitter is %s, expected 3s", schedule.Jitter())
	}
}

func TestADocumentNeverSerialisesADurationAsNanoseconds(t *testing.T) {
	// Asserted against the real encoder, so a field changed to time.Duration
	// fails here rather than on a fleet.
	raw, err := json.Marshal(monitoring.Schedule{Kind: "interval", IntervalSeconds: 30})
	if err != nil {
		t.Fatalf("cannot encode a schedule: %v", err)
	}
	if strings.Contains(string(raw), "30000000000") {
		t.Fatalf("a duration was serialised as nanoseconds: %s", raw)
	}
	if !strings.Contains(string(raw), `"interval_seconds":30`) {
		t.Fatalf("expected integer seconds, got %s", raw)
	}
}

func containsSubstring(values []string, needle string) bool {
	for _, value := range values {
		if strings.Contains(value, needle) {
			return true
		}
	}
	return false
}
