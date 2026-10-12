package monitoring_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

const calendarContractPath = "../../../../contracts/calendar-cases.json"

type calendarContract struct {
	Cases []struct {
		Name     string `json:"name"`
		Schedule struct {
			Kind               string   `json:"kind"`
			Timezone           string   `json:"timezone"`
			Times              []string `json:"times"`
			DaysOfWeek         []int    `json:"days_of_week"`
			DaysOfMonth        []int    `json:"days_of_month"`
			Months             []int    `json:"months"`
			OnMissed           string   `json:"on_missed"`
			MissedGraceSeconds int      `json:"missed_grace_seconds"`
			DstNonexistent     string   `json:"dst_nonexistent"`
			DstAmbiguous       string   `json:"dst_ambiguous"`
		} `json:"schedule"`
		After    string `json:"after"`
		Expected *struct {
			At            string `json:"at"`
			LocalTime     string `json:"local_time"`
			DstAdjustment string `json:"dst_adjustment"`
		} `json:"expected"`
	} `json:"cases"`
}

// TestCalendarMatchesTheContract is what makes two evaluators acceptable.
//
// The agent's is authoritative — it has to evaluate schedules while the
// controller is unreachable — and the controller's exists for validation and
// for previewing a schedule in the interface. Two implementations of one rule
// would drift, so the cases are generated from the controller's evaluator,
// committed, and reproduced here.
//
// What this does and does not prove: it proves the two **agree**, not that
// either matches the specification. The specification is prose
// (docs/SCHEDULING.md §5) and is tested directly on the Python side against
// real transition dates. Agreement is the property the fleet actually needs.
func TestCalendarMatchesTheContract(t *testing.T) {
	raw, err := os.ReadFile(filepath.Clean(calendarContractPath))
	if err != nil {
		t.Fatalf(
			"cannot read the calendar contract at %s: %v\n"+
				"Generate it with ./scripts/contracts.sh from the repository root.",
			calendarContractPath, err,
		)
	}
	var contract calendarContract
	if err := json.Unmarshal(raw, &contract); err != nil {
		t.Fatalf("the calendar contract is not valid JSON: %v", err)
	}
	if len(contract.Cases) == 0 {
		t.Fatal("the calendar contract has no cases")
	}

	for _, testCase := range contract.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			after, err := time.Parse(time.RFC3339, testCase.After)
			if err != nil {
				t.Fatalf("cannot parse `after`: %v", err)
			}
			schedule := monitoring.Schedule{
				Kind:               testCase.Schedule.Kind,
				Timezone:           testCase.Schedule.Timezone,
				Times:              testCase.Schedule.Times,
				DaysOfWeek:         testCase.Schedule.DaysOfWeek,
				DaysOfMonth:        testCase.Schedule.DaysOfMonth,
				Months:             testCase.Schedule.Months,
				OnMissed:           testCase.Schedule.OnMissed,
				MissedGraceSeconds: testCase.Schedule.MissedGraceSeconds,
				DstNonexistent:     testCase.Schedule.DstNonexistent,
				DstAmbiguous:       testCase.Schedule.DstAmbiguous,
			}

			occurrence, found := monitoring.NextRun(schedule, after)

			if testCase.Expected == nil {
				if found {
					t.Fatalf("expected no occurrence, got %s", occurrence.At)
				}
				return
			}
			if !found {
				t.Fatalf("expected %s, got no occurrence", testCase.Expected.At)
			}

			expectedAt, err := time.Parse(time.RFC3339, testCase.Expected.At)
			if err != nil {
				t.Fatalf("cannot parse the expected instant: %v", err)
			}
			if !occurrence.At.Equal(expectedAt) {
				t.Errorf("instant: got %s, the controller computes %s",
					occurrence.At.Format(time.RFC3339), expectedAt.Format(time.RFC3339))
			}
			if occurrence.LocalTime != testCase.Expected.LocalTime {
				t.Errorf("local time: got %q, expected %q",
					occurrence.LocalTime, testCase.Expected.LocalTime)
			}
			if occurrence.DstAdjustment != testCase.Expected.DstAdjustment {
				t.Errorf("dst adjustment: got %q, expected %q",
					occurrence.DstAdjustment, testCase.Expected.DstAdjustment)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Properties the contract does not cover
// ---------------------------------------------------------------------------

func TestAnUnknownZoneYieldsNoOccurrence(t *testing.T) {
	// Rather than falling back to UTC: running a maintenance window in the
	// wrong zone is worse than not running it.
	schedule := monitoring.Schedule{
		Kind: monitoring.KindCalendar, Timezone: "Not/AZone", Times: []string{"03:00"},
	}
	if _, found := monitoring.NextRun(schedule, time.Now()); found {
		t.Fatal("an unknown zone produced an occurrence")
	}
}

func TestEmbeddedTzdataWorksWithoutASystemDatabase(t *testing.T) {
	// time/tzdata is imported for this: on a minimal host or a scratch
	// container with no /usr/share/zoneinfo, LoadLocation would otherwise
	// fail and every calendar schedule would silently not run.
	//
	// Asserted by loading a zone whose rules are not UTC and checking the
	// offset, which proves real rule data was found rather than a fallback.
	schedule := monitoring.Schedule{
		Kind: monitoring.KindCalendar, Timezone: "Europe/Berlin", Times: []string{"12:00"},
	}
	occurrence, found := monitoring.NextRun(
		schedule, time.Date(2024, 7, 1, 0, 0, 0, 0, time.UTC),
	)
	if !found {
		t.Fatal("no occurrence; the time zone database may be missing")
	}
	// July in Berlin is UTC+2, so 12:00 local is 10:00 UTC.
	if occurrence.At.UTC().Hour() != 10 {
		t.Errorf("12:00 Berlin resolved to %s UTC; the zone rules were not applied",
			occurrence.At.UTC().Format("15:04"))
	}
}

func TestUpcomingRunsIsStrictlyIncreasing(t *testing.T) {
	schedule := monitoring.Schedule{
		Kind:     monitoring.KindCalendar,
		Timezone: "Europe/Berlin",
		Times:    []string{"00:00", "12:00"},
	}
	runs := monitoring.UpcomingRuns(schedule, time.Date(2024, 6, 1, 0, 0, 0, 0, time.UTC), 6)
	if len(runs) != 6 {
		t.Fatalf("got %d runs, expected 6", len(runs))
	}
	for index := 1; index < len(runs); index++ {
		if !runs[index].At.After(runs[index-1].At) {
			t.Fatalf("run %d (%s) is not after run %d (%s)",
				index, runs[index].At, index-1, runs[index-1].At)
		}
	}
}

func TestUpcomingRunsDoesNotRepeatABothPolicysTwoInstants(t *testing.T) {
	// `both` yields two instants for one wall-clock time, and the walk
	// advances by instant, so neither is returned twice.
	schedule := monitoring.Schedule{
		Kind:         monitoring.KindCalendar,
		Timezone:     "Europe/Berlin",
		Times:        []string{"02:30"},
		DstAmbiguous: monitoring.DstBoth,
	}
	runs := monitoring.UpcomingRuns(schedule, time.Date(2024, 10, 26, 12, 0, 0, 0, time.UTC), 4)
	seen := make(map[int64]bool, len(runs))
	for _, run := range runs {
		if seen[run.At.UnixNano()] {
			t.Fatalf("instant %s was returned twice", run.At)
		}
		seen[run.At.UnixNano()] = true
	}
}

func TestAnUnreachableScheduleWithinTheHorizonYieldsNothing(t *testing.T) {
	// 30 February. The controller refuses this at save time, so the agent
	// should never see one — but if it does, not running is the right answer.
	schedule := monitoring.Schedule{
		Kind:        monitoring.KindCalendar,
		Timezone:    "UTC",
		Times:       []string{"00:00"},
		Months:      []int{2},
		DaysOfMonth: []int{30},
	}
	if _, found := monitoring.NextRun(schedule, time.Now()); found {
		t.Fatal("30 February produced an occurrence")
	}
}

func TestSundayIsZeroNotSeven(t *testing.T) {
	// Go's time.Weekday is already 0 = Sunday and Python's is 0 = Monday, so
	// this is where a conversion error between the two would show.
	schedule := monitoring.Schedule{
		Kind: monitoring.KindCalendar, Timezone: "UTC",
		Times: []string{"00:00"}, DaysOfWeek: []int{0},
	}
	occurrence, found := monitoring.NextRun(
		schedule, time.Date(2024, 6, 1, 0, 0, 0, 0, time.UTC),
	)
	if !found {
		t.Fatal("no occurrence")
	}
	// 2 June 2024 is a Sunday.
	if occurrence.At.Weekday() != time.Sunday {
		t.Errorf("fired on a %s, expected Sunday", occurrence.At.Weekday())
	}
}

func TestAnOccurrenceLaterTodayIsFoundRatherThanSkipped(t *testing.T) {
	berlin, err := time.LoadLocation("Europe/Berlin")
	if err != nil {
		t.Fatalf("cannot load the zone: %v", err)
	}
	schedule := monitoring.Schedule{
		Kind: monitoring.KindCalendar, Timezone: "Europe/Berlin", Times: []string{"23:00"},
	}
	occurrence, found := monitoring.NextRun(
		schedule, time.Date(2024, 6, 1, 8, 0, 0, 0, berlin),
	)
	if !found {
		t.Fatal("no occurrence")
	}
	if got := occurrence.At.In(berlin).Format("2006-01-02 15:04"); got != "2024-06-01 23:00" {
		t.Errorf("got %s, expected 2024-06-01 23:00", got)
	}
}
