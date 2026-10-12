// Package collect reads real measurements from the Linux kernel.
//
// Five collectors, one per module: CPU, memory, swap, load average and system
// uptime. Each reads a pseudo-file under /proc and nothing else — no
// subprocess, no parsing of human-readable command output, no dependency.
// That is what makes a one-second interval affordable and it is why these five
// are the ones this phase implements.
//
// # Nothing here invents a value
//
// The rule the whole package is shaped around: a measurement the kernel did
// not provide is **absent**, never substituted. A collector that cannot read
// its source returns an error; a field the kernel did not report produces no
// sample rather than a zero. An operator looking at a gap has to be able to
// tell "nothing was collected" from "the value was zero", and the only way to
// keep that true is to never manufacture the second
// (docs/PRODUCT_VISION.md principle 2).
//
// Concretely, and each of these was a decision rather than an omission:
//
//   - A swapless host produces **no swap samples**, not 0% of 0 bytes.
//   - CPU utilisation needs two samples; the first collection after a start
//     produces **nothing**, not 0%.
//   - A counter that went backwards — a reboot, a wrapped register — produces
//     **nothing** for that interval, not a spike of several thousand percent.
//
// # Availability is per host and is reported, not assumed
//
// [Collector.Available] answers "can this host collect this", which is a
// different question from "does this agent have this module". A swapless host
// supports the swap module and cannot produce a figure from it, and the
// interface needs to say which — otherwise an operator enables swap monitoring
// and watches an empty chart forever
// (docs/AGENT_ARCHITECTURE.md §7).
//
// # Every collector is tested against recorded fixtures
//
// The /proc root is a field, not a constant, so the tests read files committed
// in testdata rather than whatever the build machine happens to be doing.
// A collector tested against the live /proc asserts almost nothing: the values
// change between runs, so the test can only check that parsing did not crash.
package collect

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strings"
)

// DefaultProcRoot is where the kernel's process pseudo-filesystem is mounted.
const DefaultProcRoot = "/proc"

// Sample is one measurement.
//
// Value is a float64 because these are measurements: a utilisation percentage
// is genuinely fractional, and forcing it to an integer would either lose a
// tenth of a percent or require a scaling convention every reader would have
// to know. That is the opposite of the rule for *configuration*, where
// integers are mandatory (ADR-019) precisely because a configuration document
// is checksummed and two languages' float formatting need not agree. Nothing
// here is checksummed.
//
// NaN and infinity are rejected at the boundary by [Valid], because they are
// what a division by a zero total produces and a chart cannot render them.
type Sample struct {
	// Metric is the identifier within the module — "utilisation", "total".
	// Matched against the module's declared metrics, so a typo is a sample the
	// controller refuses rather than a new series nobody expected.
	Metric string
	// Labels are the declared dimensions: {"core": "3"}, {"window": "1m"}.
	// Nil for a metric with one series per host.
	Labels map[string]string
	Value  float64
}

// Valid reports whether a sample can be stored and rendered.
//
// A separate check rather than an error from each collector, because the
// failure it catches is arithmetic rather than parsing: 0/0 on a host with no
// swap, or a ratio taken across a zero-length interval. Catching it once here
// means no collector has to remember, and a collector that does produce one is
// a bug caught by [Collect] rather than a NaN in a chart.
func (s Sample) Valid() bool {
	return s.Metric != "" && !math.IsNaN(s.Value) && !math.IsInf(s.Value, 0)
}

// Availability is whether a host can run a collector, and why not.
type Availability struct {
	Available bool
	// Reason is operator-facing and is set only when Available is false.
	// Phrased as what is missing rather than as an error, because it is
	// displayed at the point of configuration: "this host has no swap
	// configured" is actionable, "open /proc/swaps: no such file" is not.
	Reason string
}

// Collector reads one module's measurements.
//
// Deliberately small. A wider interface — start, stop, configure, describe —
// would make every collector carry lifecycle code that the scheduler already
// owns, and the five in this phase are stateless apart from the CPU
// collector's previous sample.
type Collector interface {
	// Module is the registry identifier this collector implements.
	Module() string
	// Available reports whether this host can produce these measurements.
	// Cheap enough to call on every configuration change; not called per
	// collection.
	Available() Availability
	// Collect returns this module's samples.
	//
	// An error means the collection failed and nothing should be recorded. An
	// empty slice with no error is legitimate and means "nothing to report
	// *this* time" — the CPU collector's first run, or an interval across a
	// counter reset. The two are different and the scheduler reports them
	// differently.
	Collect(ctx context.Context) ([]Sample, error)
}

// ErrNoBaseline reports a collection that needs a previous sample it does not
// have.
//
// Not an error returned to the scheduler: a collector in this state returns an
// empty slice, because "not yet" is not a failure and recording it as one
// would make every agent start with a collector error. The sentinel exists for
// the collectors' own internal use and for tests to assert against.
var ErrNoBaseline = fmt.Errorf("no previous sample to compute a rate from")

// percent converts a ratio to a 0-100 percentage, clamped.
//
// Clamped because the kernel's own counters can produce a ratio slightly above
// one across a short interval — /proc/stat's fields are updated by different
// code paths and are not sampled atomically — and a CPU utilisation of 100.4%
// is a number an operator will not believe, from a system that is otherwise
// telling the truth. Clamping is honest here in a way it would not be for a
// configured value: this is rounding a measurement to its own defined range,
// not quietly changing a setting.
//
// A zero denominator returns NaN rather than zero, so [Sample.Valid] rejects
// it and no sample is produced. Returning zero would assert that nothing was
// in use, which is not what "we could not measure" means.
func percent(part, total float64) float64 {
	if total <= 0 {
		return math.NaN()
	}
	ratio := part / total * 100
	switch {
	case ratio < 0:
		return 0
	case ratio > 100:
		return 100
	default:
		return ratio
	}
}

// keepValid drops samples that cannot be stored, reporting how many went.
//
// Returning the count rather than silently filtering: a collector producing
// invalid samples is a bug, and the scheduler logs the count so it is visible
// rather than appearing as a quietly short series.
func keepValid(samples []Sample) (kept []Sample, dropped int) {
	kept = make([]Sample, 0, len(samples))
	for _, sample := range samples {
		if sample.Valid() {
			kept = append(kept, sample)
			continue
		}
		dropped++
	}
	return kept, dropped
}

// sortSamples orders samples by metric and then by label, so a series arrives
// in the same order every time.
//
// Not required for correctness — the controller stores by series, not by
// position — but it makes a test assertion a list comparison rather than a set
// comparison, and makes a log line of samples readable.
func sortSamples(samples []Sample) {
	sort.SliceStable(samples, func(left, right int) bool {
		if samples[left].Metric != samples[right].Metric {
			return samples[left].Metric < samples[right].Metric
		}
		return labelKey(samples[left].Labels) < labelKey(samples[right].Labels)
	})
}

func labelKey(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for key := range labels {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, key+"="+labels[key])
	}
	return strings.Join(parts, ",")
}
