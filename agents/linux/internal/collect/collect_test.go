package collect_test

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
)

// Every test reads a recorded fixture under testdata rather than the live
// /proc. A collector tested against the running kernel can assert almost
// nothing: the values change between runs, so the test can only check that
// parsing did not crash — which is the part least likely to be wrong.
//
// The fixtures are a plausible 4-core host:
//
//	baseline  — 1000/100/500/8000/200/10/20 jiffies, 16 GiB RAM, 2 GiB swap
//	busy      — 1000 jiffies later: 600 busy, 400 idle, so 60% utilisation
//	rebooted  — counters *lower* than the baseline
//	oldkernel — seven /proc/stat fields, i.e. no steal/guest/guest_nice
//	noswap    — no swap, and no MemAvailable either (pre-3.14)
const (
	baseline  = "testdata/baseline"
	busy      = "testdata/busy"
	rebooted  = "testdata/rebooted"
	oldkernel = "testdata/oldkernel"
	noswap    = "testdata/noswap"
)

const gibibyte = 1024 * 1024 * 1024

func valueOf(t *testing.T, samples []collect.Sample, metric string) float64 {
	t.Helper()
	for _, sample := range samples {
		if sample.Metric == metric && len(sample.Labels) == 0 {
			return sample.Value
		}
	}
	t.Fatalf("no %q sample in %v", metric, names(samples))
	return 0
}

func labelled(samples []collect.Sample, metric, key, value string) (float64, bool) {
	for _, sample := range samples {
		if sample.Metric == metric && sample.Labels[key] == value {
			return sample.Value, true
		}
	}
	return 0, false
}

func names(samples []collect.Sample) []string {
	out := make([]string, 0, len(samples))
	for _, sample := range samples {
		out = append(out, sample.Metric)
	}
	return out
}

func countOf(samples []collect.Sample, metric string) int {
	count := 0
	for _, sample := range samples {
		if sample.Metric == metric {
			count++
		}
	}
	return count
}

func closeTo(t *testing.T, got, want, tolerance float64, label string) {
	t.Helper()
	if math.Abs(got-want) > tolerance {
		t.Errorf("%s is %.4f, expected %.4f (±%g)", label, got, want, tolerance)
	}
}

// ---------------------------------------------------------------------------
// CPU
// ---------------------------------------------------------------------------

func TestCPUProducesNoUtilisationFromOneSnapshot(t *testing.T) {
	// The claim the mission's §12 asks to be refused: utilisation cannot be
	// computed from a single instantaneous counter snapshot. /proc/stat
	// reports jiffies since boot, so one reading is a lifetime average and
	// not what anybody means by "CPU usage".
	cpu := collect.NewCPU(baseline, false)
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if countOf(samples, "utilisation") != 0 {
		t.Errorf("a utilisation was reported from one snapshot: %v", samples)
	}
	// And specifically **not** zero, which would render as an idle host.
	for _, sample := range samples {
		if sample.Metric == "utilisation" {
			t.Errorf("utilisation was reported as %v", sample.Value)
		}
	}
}

func TestCPUReportsTheCoreCountImmediately(t *testing.T) {
	// A fact about the host rather than a rate, so it needs no baseline.
	cpu := collect.NewCPU(baseline, false)
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := valueOf(t, samples, "logical_cpus"); got != 4 {
		t.Errorf("logical_cpus is %v, expected 4", got)
	}
}

func TestCPUComputesUtilisationFromTheDelta(t *testing.T) {
	// baseline -> busy is 1000 total jiffies, of which 400 idle (350 idle +
	// 50 iowait), so 600 busy = 60%.
	cpu := collect.NewCPU(baseline, false)
	if _, err := cpu.Collect(context.Background()); err != nil {
		t.Fatalf("baseline: %v", err)
	}
	cpu.ProcRoot = busy
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	closeTo(t, valueOf(t, samples, "utilisation"), 60, 0.001, "utilisation")
}

func TestCPUCountsIowaitAsIdle(t *testing.T) {
	// A host waiting on a disk is not CPU-bound, and counting iowait as busy
	// makes it look like one — the opposite of the diagnosis an operator
	// needs. baseline -> busy moves iowait by 50 of the 1000 jiffies; if
	// iowait were counted as busy the answer would be 65% rather than 60%.
	cpu := collect.NewCPU(baseline, false)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy
	samples, _ := cpu.Collect(context.Background())
	got := valueOf(t, samples, "utilisation")
	if math.Abs(got-65) < 0.001 {
		t.Fatalf("utilisation is %.2f%%, which is iowait counted as busy", got)
	}
	closeTo(t, got, 60, 0.001, "utilisation")
}

func TestCPUProducesNothingAcrossACounterReset(t *testing.T) {
	// A reboot. The difference is meaningless, and treating a negative delta
	// as a large positive one renders as several thousand percent at exactly
	// the moment somebody is working out what happened.
	cpu := collect.NewCPU(busy, false)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = rebooted
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("a counter reset must not be an error: %v", err)
	}
	if countOf(samples, "utilisation") != 0 {
		t.Errorf("a utilisation was reported across a reset: %v", samples)
	}
}

func TestCPURecoversAfterAReset(t *testing.T) {
	// The reset replaced the baseline, so the *next* interval works. Without
	// that, a reboot would silence the CPU series until the agent restarted.
	cpu := collect.NewCPU(busy, false)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = rebooted
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = baseline
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if countOf(samples, "utilisation") != 1 {
		t.Errorf("the collector did not recover after a reset: %v", samples)
	}
}

func TestCPUProducesNothingForAZeroLengthInterval(t *testing.T) {
	// Two collections inside one jiffy. Observed for real: the live smoke
	// check called Collect twice in succession and correctly reported no
	// utilisation.
	cpu := collect.NewCPU(baseline, false)
	_, _ = cpu.Collect(context.Background())
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if countOf(samples, "utilisation") != 0 {
		t.Errorf("a utilisation was reported for a zero-length interval: %v", samples)
	}
}

func TestCPUPerCoreIsOffByDefault(t *testing.T) {
	// It multiplies this module's cardinality by the core count, which is a
	// cost the operator should opt into rather than discover.
	cpu := collect.NewCPU(baseline, false)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy
	samples, _ := cpu.Collect(context.Background())
	if count := countOf(samples, "core_utilisation"); count != 0 {
		t.Errorf("per-core produced %d series with the option off", count)
	}
}

func TestCPUPerCoreAgreesWithTheAggregate(t *testing.T) {
	// The arithmetic cross-check: on a fixture where every core moved
	// identically, each core's utilisation equals the aggregate's. Verified
	// against the live kernel too — 20 cores, mean 11.59%, aggregate 11.59%.
	cpu := collect.NewCPU(baseline, true)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if count := countOf(samples, "core_utilisation"); count != 4 {
		t.Fatalf("got %d per-core series, expected 4", count)
	}

	aggregate := valueOf(t, samples, "utilisation")
	var sum float64
	for _, sample := range samples {
		if sample.Metric == "core_utilisation" {
			sum += sample.Value
		}
	}
	closeTo(t, sum/4, aggregate, 0.2, "mean of the per-core series")
}

func TestCPUPerCoreIsLabelledByTheKernelsOwnName(t *testing.T) {
	// By label rather than by position: a container's cpuset can expose a
	// non-contiguous set, and indexing by position would mislabel them.
	cpu := collect.NewCPU(baseline, true)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy
	samples, _ := cpu.Collect(context.Background())
	for _, core := range []string{"0", "1", "2", "3"} {
		if _, found := labelled(samples, "core_utilisation", "core", core); !found {
			t.Errorf("no series for core %s", core)
		}
	}
}

func TestCPUToleratesAKernelWithFewerFields(t *testing.T) {
	// steal arrived in 2.6.11, guest in 2.6.24, guest_nice in 2.6.33. A
	// kernel older than this agent produces fewer fields, and refusing that
	// would make the agent unusable on a host it could otherwise monitor.
	cpu := collect.NewCPU(oldkernel, false)
	samples, err := cpu.Collect(context.Background())
	if err != nil {
		t.Fatalf("a short /proc/stat line must be tolerated: %v", err)
	}
	if valueOf(t, samples, "logical_cpus") != 1 {
		t.Errorf("expected one core, got %v", samples)
	}
}

func TestCPURefusesAMalformedStat(t *testing.T) {
	// Tolerant of a *short* line and strict about a malformed one: a field
	// that is present and not a number means the file is not what it claims.
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "stat"), []byte("cpu  a b c d e\n"), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	if _, err := collect.NewCPU(root, false).Collect(context.Background()); err == nil {
		t.Fatal("expected a malformed /proc/stat to be refused")
	}
}

func TestCPURefusesAFileWithNoAggregateLine(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "stat"), []byte("intr 1 2 3\nctxt 4\n"), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	_, err := collect.NewCPU(root, false).Collect(context.Background())
	if err == nil || !strings.Contains(err.Error(), "no aggregate cpu line") {
		t.Fatalf("expected a refusal naming the missing line, got %v", err)
	}
}

func TestCPUReportsUnavailableWithoutProcStat(t *testing.T) {
	availability := collect.NewCPU(t.TempDir(), false).Available()
	if availability.Available {
		t.Fatal("reported available with no /proc/stat")
	}
	if availability.Reason == "" {
		t.Error("an unavailable collector must say why")
	}
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

func TestMemoryUsesMemAvailableNotMemFree(t *testing.T) {
	// The single most common mistake in memory monitoring. MemFree is near
	// zero on a healthy Linux host because the kernel uses everything spare
	// for page cache; reporting it as "available" tells every operator their
	// server is about to run out of memory when it is working as designed.
	//
	// The fixture has MemFree=0.5 GiB and MemAvailable=8 GiB, so the two
	// answers differ by a factor of sixteen.
	samples, err := collect.NewMemory(baseline).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	closeTo(t, valueOf(t, samples, "available"), 8*gibibyte, 1, "available")
	if math.Abs(valueOf(t, samples, "available")-0.5*gibibyte) < 1 {
		t.Fatal("available was reported as MemFree")
	}
}

func TestMemoryConvertsKibibytesToBytes(t *testing.T) {
	// /proc/meminfo reports in kibibytes with the unit on each line. Taking
	// the number without reading the unit is how a 16 GiB host reports 16 MB.
	samples, _ := collect.NewMemory(baseline).Collect(context.Background())
	closeTo(t, valueOf(t, samples, "total"), 16*gibibyte, 1, "total")
}

func TestMemoryUsedAndAvailableSumToTheTotal(t *testing.T) {
	// Deriving used from MemFree instead would make the two figures disagree
	// by the size of the page cache, which on a busy host is most of the
	// machine.
	samples, _ := collect.NewMemory(baseline).Collect(context.Background())
	total := valueOf(t, samples, "total")
	closeTo(t, valueOf(t, samples, "used")+valueOf(t, samples, "available"), total, 1, "used + available")
}

func TestMemoryUtilisationIsAPercentageOfTheTotal(t *testing.T) {
	// 16 GiB total, 8 GiB available, so 8 GiB used = 50%.
	samples, _ := collect.NewMemory(baseline).Collect(context.Background())
	closeTo(t, valueOf(t, samples, "utilisation"), 50, 0.001, "utilisation")
}

func TestMemoryFallsBackWhenMemAvailableIsAbsent(t *testing.T) {
	// A kernel older than 3.14. The approximation is
	// MemFree + Buffers + Cached + SReclaimable = 0.5 + 0.25 + 6 + 0.5 GiB.
	samples, err := collect.NewMemory(noswap).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	closeTo(t, valueOf(t, samples, "available"), 7.25*gibibyte, 1, "available")
}

func TestMemoryRefusesAFileWithNoTotal(t *testing.T) {
	// A host with no total memory does not exist, so this is a parsing or
	// mounting failure — and every percentage below it would be of nothing.
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "meminfo"), []byte("MemFree: 1024 kB\n"), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	if _, err := collect.NewMemory(root).Collect(context.Background()); err == nil {
		t.Fatal("expected a missing MemTotal to be refused")
	}
}

func TestMemoryIgnoresAnUnrecognisedUnit(t *testing.T) {
	// Guessing would produce a figure off by three orders of magnitude, which
	// is worse than an absent one.
	root := t.TempDir()
	content := "MemTotal: 16777216 kB\nMemAvailable: 100 furlongs\n"
	if err := os.WriteFile(filepath.Join(root, "meminfo"), []byte(content), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	samples, err := collect.NewMemory(root).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// MemAvailable was skipped, so the fallback applied and produced zero —
	// which is the honest answer for a file that reports nothing usable.
	closeTo(t, valueOf(t, samples, "available"), 0, 1, "available")
}

func TestNoMemorySampleIsNaNOrInfinite(t *testing.T) {
	samples, _ := collect.NewMemory(baseline).Collect(context.Background())
	for _, sample := range samples {
		if !sample.Valid() {
			t.Errorf("%s is not a renderable value: %v", sample.Metric, sample.Value)
		}
	}
}

// ---------------------------------------------------------------------------
// Swap
// ---------------------------------------------------------------------------

func TestSwapReportsUsageWhenSwapExists(t *testing.T) {
	// 2 GiB total, 1 GiB free, so 1 GiB used = 50%.
	samples, err := collect.NewSwap(baseline).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	closeTo(t, valueOf(t, samples, "total"), 2*gibibyte, 1, "total")
	closeTo(t, valueOf(t, samples, "used"), 1*gibibyte, 1, "used")
	closeTo(t, valueOf(t, samples, "utilisation"), 50, 0.001, "utilisation")
}

func TestASwaplessHostProducesNoSwapSamples(t *testing.T) {
	// Not 0% of 0 bytes. No samples renders as "no data"; a zero renders as a
	// line at the bottom, which asserts that swap exists and is unused.
	samples, err := collect.NewSwap(noswap).Collect(context.Background())
	if err != nil {
		t.Fatalf("a swapless host must not be an error: %v", err)
	}
	if len(samples) != 0 {
		t.Errorf("a swapless host produced %v", samples)
	}
}

func TestASwaplessHostReportsUnavailableWithAReason(t *testing.T) {
	// Containers and many cloud instances have no swap, so this is the common
	// case — and the interface needs to say so where somebody is about to
	// enable the module rather than show an empty chart forever.
	availability := collect.NewSwap(noswap).Available()
	if availability.Available {
		t.Fatal("a swapless host reported swap as available")
	}
	if !strings.Contains(availability.Reason, "no swap configured") {
		t.Errorf("the reason should be actionable, got %q", availability.Reason)
	}
}

func TestSwapIsAvailableWhenSwapExists(t *testing.T) {
	if !collect.NewSwap(baseline).Available().Available {
		t.Fatal("a host with swap reported it as unavailable")
	}
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

func TestLoadReportsThreeWindows(t *testing.T) {
	samples, err := collect.NewLoad(baseline).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(samples) != 3 {
		t.Fatalf("got %d samples, expected 3", len(samples))
	}
	for window, want := range map[string]float64{"1m": 2.64, "5m": 3.07, "15m": 2.98} {
		got, found := labelled(samples, "average", "window", window)
		if !found {
			t.Errorf("no series for the %s window", window)
			continue
		}
		closeTo(t, got, want, 0.001, window+" load")
	}
}

func TestLoadIsOneMetricLabelledByWindow(t *testing.T) {
	// One series per window rather than three metrics, because the interface
	// draws them on one chart and they are the same measurement.
	samples, _ := collect.NewLoad(baseline).Collect(context.Background())
	for _, sample := range samples {
		if sample.Metric != "average" {
			t.Errorf("unexpected metric %q", sample.Metric)
		}
		if sample.Labels["window"] == "" {
			t.Error("a load sample has no window label")
		}
	}
}

func TestLoadIsNotExpressedAsAPercentage(t *testing.T) {
	// §14: load average must not be labelled as CPU utilisation. It has no
	// upper bound, so a value above 100 has to survive — a collector that
	// clamped or scaled it would be presenting it as a percentage.
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "loadavg"), []byte("128.5 64.2 32.1 1/2 3\n"), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	samples, err := collect.NewLoad(root).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	got, _ := labelled(samples, "average", "window", "1m")
	closeTo(t, got, 128.5, 0.001, "a load above 100")
}

func TestLoadRefusesAMalformedFile(t *testing.T) {
	root := t.TempDir()
	for name, content := range map[string]string{
		"too few fields": "1.0 2.0\n",
		"not a number":   "a b c 1/2 3\n",
		"negative":       "-1.0 2.0 3.0 1/2 3\n",
	} {
		if err := os.WriteFile(filepath.Join(root, "loadavg"), []byte(content), 0o600); err != nil {
			t.Fatalf("cannot write the fixture: %v", err)
		}
		if _, err := collect.NewLoad(root).Collect(context.Background()); err == nil {
			t.Errorf("%s: expected a refusal", name)
		}
	}
}

// ---------------------------------------------------------------------------
// Uptime
// ---------------------------------------------------------------------------

func TestUptimeReportsTheFirstFieldOnly(t *testing.T) {
	// The second field is idle time summed across all CPUs, which is not
	// uptime and on a multi-core host exceeds it.
	samples, err := collect.NewUptime(baseline).Collect(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(samples) != 1 {
		t.Fatalf("got %d samples, expected 1", len(samples))
	}
	closeTo(t, valueOf(t, samples, "seconds"), 125203.33, 0.001, "uptime")
}

func TestUptimeIsTheHostsNotTheAgents(t *testing.T) {
	// Asserted by value: the fixture's 125203 seconds is about 34 hours, and
	// a collector reporting the agent's own uptime would return a number
	// close to zero in a test that has just started.
	samples, _ := collect.NewUptime(baseline).Collect(context.Background())
	if got := valueOf(t, samples, "seconds"); got < 1000 {
		t.Errorf("uptime is %.0f seconds, which looks like the agent's own", got)
	}
}

func TestUptimeRefusesAMalformedFile(t *testing.T) {
	root := t.TempDir()
	for name, content := range map[string]string{
		"empty":        "",
		"not a number": "soon\n",
		"negative":     "-1.0 2.0\n",
	} {
		if err := os.WriteFile(filepath.Join(root, "uptime"), []byte(content), 0o600); err != nil {
			t.Fatalf("cannot write the fixture: %v", err)
		}
		if _, err := collect.NewUptime(root).Collect(context.Background()); err == nil {
			t.Errorf("%s: expected a refusal", name)
		}
	}
}

// ---------------------------------------------------------------------------
// Cross-cutting
// ---------------------------------------------------------------------------

func TestEveryCollectorReportsItsModule(t *testing.T) {
	// Matched against the registry, so a collector whose module identifier
	// does not exist is caught here rather than by the controller refusing
	// its samples.
	expected := map[string]bool{"cpu": true, "memory": true, "swap": true, "load": true, "uptime": true}
	collectors := []collect.Collector{
		collect.NewCPU(baseline, false),
		collect.NewMemory(baseline),
		collect.NewSwap(baseline),
		collect.NewLoad(baseline),
		collect.NewUptime(baseline),
	}
	for _, collector := range collectors {
		if !expected[collector.Module()] {
			t.Errorf("collector reports an unknown module %q", collector.Module())
		}
		delete(expected, collector.Module())
	}
	if len(expected) != 0 {
		t.Errorf("no collector for %v", expected)
	}
}

func TestEveryCollectorReportsUnavailableWithAReasonOnAnEmptyRoot(t *testing.T) {
	root := t.TempDir()
	collectors := []collect.Collector{
		collect.NewCPU(root, false),
		collect.NewMemory(root),
		collect.NewSwap(root),
		collect.NewLoad(root),
		collect.NewUptime(root),
	}
	for _, collector := range collectors {
		availability := collector.Available()
		if availability.Available {
			t.Errorf("%s reported available with no /proc", collector.Module())
		}
		if availability.Reason == "" {
			t.Errorf("%s is unavailable and does not say why", collector.Module())
		}
	}
}

func TestNoCollectorEverProducesAnInvalidSample(t *testing.T) {
	// The boundary [collect.Sample.Valid] exists for: 0/0 on a host with no
	// swap, or a ratio across a zero-length interval. A NaN reaching a chart
	// is a rendering failure an operator cannot diagnose.
	cpu := collect.NewCPU(baseline, true)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy

	for _, root := range []string{baseline, busy, noswap, oldkernel} {
		collectors := []collect.Collector{
			collect.NewMemory(root),
			collect.NewSwap(root),
			collect.NewLoad(root),
			collect.NewUptime(root),
		}
		for _, collector := range collectors {
			samples, err := collector.Collect(context.Background())
			if err != nil {
				continue
			}
			for _, sample := range samples {
				if !sample.Valid() {
					t.Errorf("%s/%s produced %v", root, collector.Module(), sample)
				}
			}
		}
	}

	samples, _ := cpu.Collect(context.Background())
	for _, sample := range samples {
		if !sample.Valid() {
			t.Errorf("cpu produced %v", sample)
		}
	}
}

func TestSamplesArriveInAStableOrder(t *testing.T) {
	// Go map iteration is randomised, so without sorting a per-core series
	// would arrive in a different order every collection — which makes a test
	// assertion a set comparison and a log line unreadable.
	cpu := collect.NewCPU(baseline, true)
	_, _ = cpu.Collect(context.Background())
	cpu.ProcRoot = busy

	first, _ := cpu.Collect(context.Background())
	firstOrder := strings.Join(describe(first), "|")
	for attempt := 0; attempt < 10; attempt++ {
		cpu.ProcRoot = baseline
		_, _ = cpu.Collect(context.Background())
		cpu.ProcRoot = busy
		again, _ := cpu.Collect(context.Background())
		if strings.Join(describe(again), "|") != firstOrder {
			t.Fatalf("sample order differed between collections:\n%v\n%v", first, again)
		}
	}
}

func describe(samples []collect.Sample) []string {
	out := make([]string, 0, len(samples))
	for _, sample := range samples {
		out = append(out, sample.Metric+"/"+sample.Labels["core"]+sample.Labels["window"])
	}
	return out
}
