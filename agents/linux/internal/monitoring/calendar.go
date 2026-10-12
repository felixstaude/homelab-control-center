package monitoring

import (
	"fmt"
	"sort"
	"time"
)

// SearchHorizonDays is how far [NextRun] looks before giving up.
//
// Four years, in days. Not a round number: a schedule restricted to 29
// February fires once every four years and the search has to find it rather
// than report a legal schedule as impossible. February 29th is also why this
// is a day-by-day walk rather than arithmetic.
const SearchHorizonDays = 366 * 4

// Occurrence is one resolved execution instant.
//
// DstAdjustment is why this is not just a time.Time. An occurrence that was
// shifted out of a nonexistent hour, or chosen from two ambiguous candidates,
// is a thing the agent should be able to log — "running at 03:00, because
// 02:30 does not exist tonight" is the difference between a scheduler an
// operator trusts and one they suspect.
type Occurrence struct {
	// At is the instant, in UTC. UTC rather than the schedule's zone because
	// this is what gets compared and sorted, and a local-time value invites a
	// comparison across two zones.
	At time.Time
	// LocalTime is the wall-clock time that produced it, "HH:MM".
	LocalTime string
	// DstAdjustment is "", "shifted_forward", "ambiguous_first" or
	// "ambiguous_second".
	DstAdjustment string
}

// Daylight-saving adjustment labels, matching the controller's.
const (
	AdjustmentNone        = ""
	AdjustmentShifted     = "shifted_forward"
	AdjustmentAmbigFirst  = "ambiguous_first"
	AdjustmentAmbigSecond = "ambiguous_second"
)

// NextRun returns the first occurrence strictly after `after`, or false.
//
// # Why this exists in the agent at all
//
// The agent evaluates its own calendar schedules, because it must keep working
// while the controller is unreachable (AGENT_ARCHITECTURE.md §8). So this is
// the *authoritative* evaluator and the controller's Python one is for
// validation and for previewing a schedule in the interface.
//
// Two implementations of one rule is a drift risk, managed rather than
// accepted: contracts/calendar-cases.json is generated from the controller's
// evaluator and TestCalendarMatchesTheContract asserts this one reproduces it.
//
// # Why a day-by-day walk
//
// The cases that break arithmetic are the ones that matter: 29 February exists
// in one year of four, "the 31st" in seven months of twelve, and a
// daylight-saving transition moves a local time without moving the date.
// Walking days and asking the time zone database what each local time means on
// that day gets all three right for at most SearchHorizonDays iterations.
func NextRun(schedule Schedule, after time.Time) (Occurrence, bool) {
	zone, err := time.LoadLocation(schedule.Timezone)
	if err != nil {
		// A schedule with an unknown zone cannot be evaluated. Validation
		// refuses it before it reaches here, so this is defensive — and it
		// returns false rather than falling back to UTC, because running a
		// maintenance window in the wrong zone is worse than not running it.
		return Occurrence{}, false
	}

	boundary := after.In(zone)
	// Start on the boundary's own local date: an occurrence later today is
	// still in the future, and starting tomorrow would skip it.
	cursor := time.Date(boundary.Year(), boundary.Month(), boundary.Day(), 0, 0, 0, 0, zone)

	// Sorted, so the first match found on a matching day is the earliest.
	times := make([]string, len(schedule.Times))
	copy(times, schedule.Times)
	sort.Strings(times)

	for day := 0; day < SearchHorizonDays; day++ {
		if dayMatches(schedule, cursor) {
			var candidates []Occurrence
			for _, wall := range times {
				candidates = append(candidates, resolveLocalTime(zone, cursor, wall, schedule)...)
			}
			// Resolution can reorder: a time shifted forward out of a
			// nonexistent hour can land after a later listed time, and an
			// ambiguous time resolved to its second occurrence certainly can.
			sort.SliceStable(candidates, func(left, right int) bool {
				return candidates[left].At.Before(candidates[right].At)
			})
			for _, candidate := range candidates {
				if candidate.At.After(after) {
					return candidate, true
				}
			}
		}
		cursor = cursor.AddDate(0, 0, 1)
	}
	return Occurrence{}, false
}

// UpcomingRuns returns the next count occurrences.
//
// Returns fewer — possibly none — when the schedule runs out within the search
// horizon, which a February-29th schedule legitimately does.
func UpcomingRuns(schedule Schedule, after time.Time, count int) []Occurrence {
	occurrences := make([]Occurrence, 0, count)
	cursor := after
	for range count {
		occurrence, found := NextRun(schedule, cursor)
		if !found {
			break
		}
		occurrences = append(occurrences, occurrence)
		cursor = occurrence.At
	}
	return occurrences
}

// dayMatches reports whether a date satisfies every day restriction.
//
// ANDed, unlike cron — where their interaction is an OR that surprises nearly
// everyone and silently runs jobs on unintended days. A nil slice means "every
// value", which is how the wire format spells it.
func dayMatches(schedule Schedule, date time.Time) bool {
	if schedule.Months != nil && !containsInt(schedule.Months, int(date.Month())) {
		return false
	}
	if schedule.DaysOfMonth != nil && !containsInt(schedule.DaysOfMonth, date.Day()) {
		return false
	}
	// Go's time.Weekday is already 0 = Sunday, which matches the wire format.
	// Stated because Python's is 0 = Monday and the controller converts; a
	// reader checking one against the other needs to know which is which.
	return schedule.DaysOfWeek == nil || containsInt(schedule.DaysOfWeek, int(date.Weekday()))
}

func containsInt(values []int, needle int) bool {
	for _, value := range values {
		if value == needle {
			return true
		}
	}
	return false
}

// resolveLocalTime turns one wall-clock time on one date into zero, one or two
// instants.
//
// Where the daylight-saving policy is applied. Three cases:
//
//   - **Nonexistent** (spring forward), detected by a UTC round trip.
//     time.Date on a nonexistent local time returns an instant whose own local
//     time is different — asking for 02:30 in a 02:00→03:00 gap yields 03:30 —
//     so comparing the resolved fields against the requested ones is the test.
//     Under shift_forward the instant used is the **first local time that
//     exists**, found by walking minutes forward, which for a 02:30 request is
//     03:00 and not 03:30. The two differ by the size of the gap, and
//     SCHEDULING.md §5.1 specifies the former.
//   - **Ambiguous** (fall back), detected by comparing the offsets an hour
//     either side: if the same local time resolves to two different instants,
//     it occurs twice. The policy picks one, or both.
//   - **Neither**, which is every other day of the year.
//
// The order is load-bearing: existence is checked first. Go, like Python,
// gives a nonexistent time a resolvable answer, so an ambiguity test that ran
// first would misclassify every spring-forward time.
func resolveLocalTime(
	zone *time.Location, date time.Time, wall string, schedule Schedule,
) []Occurrence {
	hour, minute, ok := parseWallClock(wall)
	if !ok {
		return nil
	}

	requested := time.Date(date.Year(), date.Month(), date.Day(), hour, minute, 0, 0, zone)

	if requested.Hour() != hour || requested.Minute() != minute {
		// Nonexistent: the resolved instant's own local time is not the one
		// asked for.
		if schedule.DstNonexistent == DstSkip {
			return nil
		}
		shifted, found := firstExistingAtOrAfter(zone, date, hour, minute)
		if !found {
			return nil
		}
		return []Occurrence{{At: shifted.UTC(), LocalTime: wall, DstAdjustment: AdjustmentShifted}}
	}

	// Ambiguous: the same local time an hour earlier in UTC also renders as
	// this local time, which can only happen when the clock went back.
	earlier := requested.Add(-time.Hour)
	if earlier.Hour() == hour && earlier.Minute() == minute {
		switch schedule.DstAmbiguous {
		case DstSecond:
			return []Occurrence{
				{At: requested.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigSecond},
			}
		case DstBoth:
			return []Occurrence{
				{At: earlier.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigFirst},
				{At: requested.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigSecond},
			}
		default:
			// DstFirst, and the zero value: run once, as early as asked.
			return []Occurrence{
				{At: earlier.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigFirst},
			}
		}
	}

	// The reverse check: this local time is the *first* of two. time.Date
	// resolves an ambiguous local time to the first occurrence, so the later
	// one is an hour ahead and renders identically.
	later := requested.Add(time.Hour)
	if later.Hour() == hour && later.Minute() == minute {
		switch schedule.DstAmbiguous {
		case DstSecond:
			return []Occurrence{
				{At: later.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigSecond},
			}
		case DstBoth:
			return []Occurrence{
				{At: requested.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigFirst},
				{At: later.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigSecond},
			}
		default:
			return []Occurrence{
				{At: requested.UTC(), LocalTime: wall, DstAdjustment: AdjustmentAmbigFirst},
			}
		}
	}

	return []Occurrence{{At: requested.UTC(), LocalTime: wall, DstAdjustment: AdjustmentNone}}
}

// firstExistingAtOrAfter walks minutes forward to the first local time that
// exists.
//
// Minute granularity because every transition in the tz database is a whole
// number of minutes, and 120 iterations covers more than the largest gap ever
// recorded — the common case is 60 minutes, Lord Howe Island's is 30. A
// hardcoded "+1 hour" would be half an hour wrong there every spring.
func firstExistingAtOrAfter(
	zone *time.Location, date time.Time, hour, minute int,
) (time.Time, bool) {
	base := time.Date(date.Year(), date.Month(), date.Day(), hour, minute, 0, 0, time.UTC)
	for offset := 1; offset <= 120; offset++ {
		probe := base.Add(time.Duration(offset) * time.Minute)
		candidate := time.Date(
			probe.Year(), probe.Month(), probe.Day(), probe.Hour(), probe.Minute(), 0, 0, zone,
		)
		if candidate.Hour() == probe.Hour() && candidate.Minute() == probe.Minute() {
			return candidate, true
		}
	}
	return time.Time{}, false
}

func parseWallClock(value string) (hour, minute int, ok bool) {
	if !validWallClock(value) {
		return 0, 0, false
	}
	if _, err := fmt.Sscanf(value, "%02d:%02d", &hour, &minute); err != nil {
		return 0, 0, false
	}
	return hour, minute, true
}
