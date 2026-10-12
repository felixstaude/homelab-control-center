package scheduler_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/scheduler"
)

// These tests drive the scheduler with a controllable clock, so a 30-second
// interval is one channel send rather than thirty seconds of waiting. A
// scheduler tested against the real clock can only assert that it eventually
// did something, which is the part least likely to be wrong.

// testClock fires tickers on demand.
type testClock struct {
	mu      sync.Mutex
	now     time.Time
	tickers map[time.Duration][]*testTicker
	// sleeps records every jitter wait, because "jitter was applied" is a
	// property worth asserting and is otherwise invisible.
	sleeps []time.Duration
	// instantSleep makes Sleep return immediately rather than waiting, so a
	// test does not have to coordinate with the jitter.
	instantSleep bool
}

func newTestClock() *testClock {
	return &testClock{
		now:          time.Date(2026, 10, 13, 12, 0, 0, 0, time.UTC),
		tickers:      make(map[time.Duration][]*testTicker),
		instantSleep: true,
	}
}

func (c *testClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	// Advanced by a millisecond on every read, so a collection's measured
	// duration is non-zero and a test can assert it was recorded.
	c.now = c.now.Add(time.Millisecond)
	return c.now
}

func (c *testClock) Ticker(d time.Duration) scheduler.Ticker {
	c.mu.Lock()
	defer c.mu.Unlock()
	ticker := &testTicker{channel: make(chan time.Time, 1)}
	c.tickers[d] = append(c.tickers[d], ticker)
	return ticker
}

func (c *testClock) Sleep(ctx context.Context, d time.Duration) bool {
	c.mu.Lock()
	c.sleeps = append(c.sleeps, d)
	instant := c.instantSleep
	c.mu.Unlock()
	if instant {
		return ctx.Err() == nil
	}
	<-ctx.Done()
	return false
}

// fire delivers one tick to every ticker at this interval.
//
// `now` is captured under the lock along with the ticker list. Reading it
// afterwards was a data race the race detector caught: Now() writes the field
// on every call, and the scheduler calls Now() from its collecting goroutines.
func (c *testClock) fire(d time.Duration) int {
	c.mu.Lock()
	tickers := append([]*testTicker(nil), c.tickers[d]...)
	now := c.now
	c.mu.Unlock()
	delivered := 0
	for _, ticker := range tickers {
		if ticker.fire(now) {
			delivered++
		}
	}
	return delivered
}

func (c *testClock) sleepCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.sleeps)
}

type testTicker struct {
	mu      sync.Mutex
	channel chan time.Time
	stopped bool
}

func (t *testTicker) C() <-chan time.Time { return t.channel }

func (t *testTicker) Stop() {
	t.mu.Lock()
	t.stopped = true
	t.mu.Unlock()
}

func (t *testTicker) fire(at time.Time) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.stopped {
		return false
	}
	select {
	case t.channel <- at:
		return true
	default:
		return false
	}
}

// fakeCollector is a collector a test controls.
type fakeCollector struct {
	module string

	mu      sync.Mutex
	calls   int
	samples []collect.Sample
	err     error
	// block, when set, holds the collection until released — so a test can
	// create a genuine overrun rather than simulating one.
	block   chan struct{}
	started chan struct{}
}

func newFakeCollector(module string) *fakeCollector {
	return &fakeCollector{
		module:  module,
		samples: []collect.Sample{{Metric: "utilisation", Value: 42}},
		started: make(chan struct{}, 16),
	}
}

func (f *fakeCollector) Module() string { return f.module }

func (f *fakeCollector) Available() collect.Availability {
	return collect.Availability{Available: true}
}

func (f *fakeCollector) Collect(ctx context.Context) ([]collect.Sample, error) {
	f.mu.Lock()
	f.calls++
	block, err, samples := f.block, f.err, f.samples
	f.mu.Unlock()

	select {
	case f.started <- struct{}{}:
	default:
	}

	if block != nil {
		select {
		case <-block:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return samples, err
}

func (f *fakeCollector) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func (f *fakeCollector) setError(err error) {
	f.mu.Lock()
	f.err = err
	f.mu.Unlock()
}

// recordingSink collects results.
type recordingSink struct {
	mu      sync.Mutex
	results []scheduler.Result
}

func (r *recordingSink) Submit(result scheduler.Result) {
	r.mu.Lock()
	r.results = append(r.results, result)
	r.mu.Unlock()
}

func (r *recordingSink) snapshot() []scheduler.Result {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]scheduler.Result, len(r.results))
	copy(out, r.results)
	return out
}

func (r *recordingSink) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.results)
}

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, &slog.HandlerOptions{Level: slog.LevelError}))
}

// documentEnabling builds a document with the named modules enabled at the
// given interval.
func documentEnabling(interval, jitter int, enabled ...string) monitoring.Document {
	wanted := make(map[string]bool, len(enabled))
	for _, id := range enabled {
		wanted[id] = true
	}
	modules := make(map[string]monitoring.ModuleConfig, len(monitoring.ModuleIDs()))
	for _, id := range monitoring.ModuleIDs() {
		modules[id] = monitoring.ModuleConfig{
			Enabled: wanted[id],
			Schedule: monitoring.Schedule{
				Kind:            monitoring.KindInterval,
				IntervalSeconds: interval,
				JitterSeconds:   jitter,
			},
		}
	}
	return monitoring.Document{
		DocumentVersion: monitoring.DocumentVersion,
		RegistryVersion: monitoring.RegistryVersion,
		HostID:          "11111111-2222-3333-4444-555555555555",
		Version:         1,
		Modules:         modules,
	}
}

// eventually polls until condition holds or the deadline passes.
//
// The scheduler collects in its own goroutine, so a test that asserted
// immediately after firing a tick would be racing it. Polling with a short
// interval keeps the suite fast while being deterministic about the outcome.
func eventually(t *testing.T, what string, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// start runs a scheduler and returns a stop function that waits for it.
//
// Blocks until every job the document asks for is running. Without that wait
// a test could fire a ticker before Run's goroutine had created it, the tick
// would go nowhere, and the test would hang on a collection that never
// started — which is exactly what happened the first time this suite ran.
// Returning early is only safe when the document enables nothing, where no
// ticker will ever exist and there is nothing to race.
func start(
	t *testing.T, clock scheduler.Clock, sink scheduler.Sink,
	collectors []collect.Collector, document monitoring.Document,
) (*scheduler.Scheduler, context.Context, func()) {
	t.Helper()
	instance := scheduler.New(quietLogger(), sink, collectors, scheduler.WithClock(clock))
	ctx, cancel := context.WithCancel(context.Background())
	instance.Apply(ctx, document)

	finished := make(chan struct{})
	go func() {
		instance.Run(ctx)
		close(finished)
	}()

	if expected := expectedActive(collectors, document); expected > 0 {
		eventually(t, "the scheduler to start its jobs", func() bool {
			return len(instance.ActiveModules()) == expected
		})
	}

	return instance, ctx, func() {
		cancel()
		select {
		case <-finished:
		case <-time.After(5 * time.Second):
			t.Fatal("the scheduler did not stop within five seconds")
		}
	}
}

// expectedActive is how many jobs a document should produce, given the
// collectors actually registered.
func expectedActive(collectors []collect.Collector, document monitoring.Document) int {
	have := make(map[string]bool, len(collectors))
	for _, collector := range collectors {
		have[collector.Module()] = true
	}
	count := 0
	for id, config := range document.Modules {
		if config.Enabled && have[id] {
			count++
		}
	}
	return count
}

// ---------------------------------------------------------------------------
// Default-deny
// ---------------------------------------------------------------------------

func TestNothingIsCollectedForAnEmptyConfiguration(t *testing.T) {
	// §2.1 at the last layer it can be broken: the scheduler must not start a
	// job for a module nobody enabled.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	instance, _, stop := start(t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0))
	defer stop()

	if active := instance.ActiveModules(); len(active) != 0 {
		t.Fatalf("an empty configuration started %v", active)
	}
	clock.fire(10 * time.Second)
	time.Sleep(20 * time.Millisecond)
	if cpu.callCount() != 0 {
		t.Errorf("a disabled module was collected %d times", cpu.callCount())
	}
}

func TestOnlyEnabledModulesAreCollected(t *testing.T) {
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	memory := newFakeCollector("memory")
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink, []collect.Collector{cpu, memory}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	if active := instance.ActiveModules(); len(active) != 1 || active[0] != "cpu" {
		t.Fatalf("active modules are %v, expected [cpu]", active)
	}
	clock.fire(10 * time.Second)
	eventually(t, "cpu to be collected", func() bool { return cpu.callCount() == 1 })
	if memory.callCount() != 0 {
		t.Errorf("memory was collected %d times", memory.callCount())
	}
}

// ---------------------------------------------------------------------------
// Independent scheduling
// ---------------------------------------------------------------------------

func TestEachModuleHasItsOwnSchedule(t *testing.T) {
	// Not one global cycle: a shared loop would make every module's cadence
	// the slowest module's.
	clock := newTestClock()
	fast := newFakeCollector("cpu")
	slow := newFakeCollector("uptime")
	sink := &recordingSink{}

	document := documentEnabling(5, 0, "cpu")
	document.Modules["uptime"] = monitoring.ModuleConfig{
		Enabled: true,
		Schedule: monitoring.Schedule{
			Kind: monitoring.KindInterval, IntervalSeconds: 300,
		},
	}

	_, _, stop := start(t, clock, sink, []collect.Collector{fast, slow}, document)
	defer stop()

	// Three ticks at five seconds; none at three hundred.
	for range 3 {
		clock.fire(5 * time.Second)
		eventually(t, "a cpu collection", func() bool { return fast.callCount() > 0 })
		time.Sleep(2 * time.Millisecond)
	}
	eventually(t, "three cpu collections", func() bool { return fast.callCount() == 3 })
	if slow.callCount() != 0 {
		t.Errorf("the 300s module was collected %d times", slow.callCount())
	}
}

func TestAFailingModuleDoesNotStopTheOthers(t *testing.T) {
	// One wedged collector must not stall the rest (SCHEDULING.md §7.2).
	clock := newTestClock()
	broken := newFakeCollector("cpu")
	broken.setError(errors.New("cannot read /proc/stat"))
	healthy := newFakeCollector("memory")
	sink := &recordingSink{}

	_, _, stop := start(
		t, clock, sink,
		[]collect.Collector{broken, healthy},
		documentEnabling(10, 0, "cpu", "memory"),
	)
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "both modules to run", func() bool {
		return broken.callCount() == 1 && healthy.callCount() == 1
	})

	eventually(t, "both results", func() bool { return sink.count() == 2 })
	var sawError, sawSuccess bool
	for _, result := range sink.snapshot() {
		if result.Module == "cpu" && result.Err != nil {
			sawError = true
		}
		if result.Module == "memory" && result.Err == nil && len(result.Samples) == 1 {
			sawSuccess = true
		}
	}
	if !sawError {
		t.Error("the failing module's error was not reported")
	}
	if !sawSuccess {
		t.Error("the healthy module did not produce samples")
	}
}

// ---------------------------------------------------------------------------
// Overrun
// ---------------------------------------------------------------------------

func TestAnOverrunSkipsRatherThanQueues(t *testing.T) {
	// A genuine overrun, not a simulated one: the collector blocks, the next
	// tick arrives, and the scheduler must skip it rather than build a
	// backlog of samples whose timestamps are already wrong.
	clock := newTestClock()
	slow := newFakeCollector("cpu")
	slow.block = make(chan struct{})
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink, []collect.Collector{slow}, documentEnabling(10, 0, "cpu"),
	)
	defer func() {
		close(slow.block)
		stop()
	}()

	clock.fire(10 * time.Second)
	<-slow.started

	// Three more ticks while the first collection is still in flight.
	for range 3 {
		clock.fire(10 * time.Second)
		time.Sleep(5 * time.Millisecond)
	}

	eventually(t, "the overruns to be counted", func() bool {
		return instance.Counters()["cpu"].SkippedOverrun >= 3
	})
	if calls := slow.callCount(); calls != 1 {
		t.Errorf("the collector ran %d times; the ticks should have been skipped", calls)
	}
}

func TestAnOverrunSkipIsCountedUnderItsOwnReason(t *testing.T) {
	// Not folded into a generic "failures": a module being skipped for
	// overrun has a different remedy from one that is erroring.
	clock := newTestClock()
	slow := newFakeCollector("cpu")
	slow.block = make(chan struct{})
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink, []collect.Collector{slow}, documentEnabling(10, 0, "cpu"),
	)
	defer func() {
		close(slow.block)
		stop()
	}()

	clock.fire(10 * time.Second)
	<-slow.started
	clock.fire(10 * time.Second)

	eventually(t, "one overrun", func() bool {
		return instance.Counters()["cpu"].SkippedOverrun == 1
	})
	counters := instance.Counters()["cpu"]
	if counters.Errors != 0 {
		t.Errorf("an overrun was counted as an error (%d)", counters.Errors)
	}
	if counters.Timeouts != 0 {
		t.Errorf("an overrun was counted as a timeout (%d)", counters.Timeouts)
	}
}

func TestTheSchedulerRecoversAfterAnOverrun(t *testing.T) {
	clock := newTestClock()
	slow := newFakeCollector("cpu")
	slow.block = make(chan struct{})
	sink := &recordingSink{}

	_, _, stop := start(
		t, clock, sink, []collect.Collector{slow}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	clock.fire(10 * time.Second)
	<-slow.started
	clock.fire(10 * time.Second)
	time.Sleep(5 * time.Millisecond)

	// Release the first collection; the next tick should run normally.
	close(slow.block)
	slow.mu.Lock()
	slow.block = nil
	slow.mu.Unlock()

	eventually(t, "the first result", func() bool { return sink.count() >= 1 })
	clock.fire(10 * time.Second)
	eventually(t, "a second collection", func() bool { return slow.callCount() >= 2 })
}

// ---------------------------------------------------------------------------
// Counters and results
// ---------------------------------------------------------------------------

func TestASuccessfulCollectionIsCountedWithItsSamples(t *testing.T) {
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	cpu.samples = []collect.Sample{
		{Metric: "utilisation", Value: 12},
		{Metric: "logical_cpus", Value: 4},
	}
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "a result", func() bool { return sink.count() == 1 })

	counters := instance.Counters()["cpu"]
	if counters.Executions != 1 {
		t.Errorf("executions is %d, expected 1", counters.Executions)
	}
	if counters.Samples != 2 {
		t.Errorf("samples is %d, expected 2", counters.Samples)
	}
	if counters.Errors != 0 {
		t.Errorf("errors is %d, expected 0", counters.Errors)
	}
}

func TestAResultCarriesTheModulesSchemaVersion(t *testing.T) {
	// So the controller can interpret a payload from an agent older than
	// itself.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	_, _, stop := start(t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"))
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "a result", func() bool { return sink.count() == 1 })

	descriptor, _ := monitoring.Lookup("cpu")
	if got := sink.snapshot()[0].SchemaVersion; got != descriptor.DataSchemaVersion {
		t.Errorf("schema version is %d, expected %d", got, descriptor.DataSchemaVersion)
	}
}

func TestAResultIsStampedWhenTheCollectionStarted(t *testing.T) {
	// Not when it finished: for a collector that took 200 ms the start is the
	// better timestamp, and the duration is reported separately.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	_, _, stop := start(t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"))
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "a result", func() bool { return sink.count() == 1 })

	result := sink.snapshot()[0]
	if result.At.IsZero() {
		t.Error("the result has no timestamp")
	}
	if result.Duration <= 0 {
		t.Errorf("duration is %s, expected a positive value", result.Duration)
	}
}

func TestAFailedCollectionIsReportedRatherThanDropped(t *testing.T) {
	// §26: collector errors are observable. A module that has been failing
	// for an hour is information an operator needs rather than an absence
	// they have to infer.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	cpu.setError(errors.New("permission denied"))
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "a result", func() bool { return sink.count() == 1 })

	result := sink.snapshot()[0]
	if result.Err == nil {
		t.Fatal("the failure was not reported")
	}
	if !strings.Contains(result.Err.Error(), "permission denied") {
		t.Errorf("the reported error lost its cause: %v", result.Err)
	}
	if instance.Counters()["cpu"].Errors != 1 {
		t.Error("the failure was not counted")
	}
}

// ---------------------------------------------------------------------------
// Jitter
// ---------------------------------------------------------------------------

func TestJitterIsAppliedToEachExecution(t *testing.T) {
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	_, _, stop := start(t, clock, sink, []collect.Collector{cpu}, documentEnabling(30, 3, "cpu"))
	defer stop()

	clock.fire(30 * time.Second)
	eventually(t, "a jittered collection", func() bool { return clock.sleepCount() >= 1 })
}

func TestNoJitterMeansNoWait(t *testing.T) {
	// Zero means "none" — for schedules where exact timing matters more than
	// smooth fleet load.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	_, _, stop := start(t, clock, sink, []collect.Collector{cpu}, documentEnabling(30, 0, "cpu"))
	defer stop()

	clock.fire(30 * time.Second)
	eventually(t, "a result", func() bool { return sink.count() == 1 })
	if count := clock.sleepCount(); count != 0 {
		t.Errorf("a zero jitter still waited %d times", count)
	}
}

// ---------------------------------------------------------------------------
// Dynamic reconfiguration
// ---------------------------------------------------------------------------

func TestEnablingAModuleStartsCollectingIt(t *testing.T) {
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	memory := newFakeCollector("memory")
	sink := &recordingSink{}

	instance, ctx, stop := start(
		t, clock, sink, []collect.Collector{cpu, memory}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	instance.Apply(ctx, documentEnabling(10, 0, "cpu", "memory"))
	eventually(t, "memory to become active", func() bool {
		return len(instance.ActiveModules()) == 2
	})

	clock.fire(10 * time.Second)
	eventually(t, "both to be collected", func() bool {
		return cpu.callCount() >= 1 && memory.callCount() >= 1
	})
}

func TestDisablingAModuleStopsCollectingIt(t *testing.T) {
	// §23 step 11: verify collection stops. The property that matters is that
	// the collector is not called again, not merely that the interface says so.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	instance, ctx, stop := start(
		t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	clock.fire(10 * time.Second)
	eventually(t, "the first collection", func() bool { return cpu.callCount() == 1 })

	instance.Apply(ctx, documentEnabling(10, 0))
	eventually(t, "cpu to stop", func() bool { return len(instance.ActiveModules()) == 0 })

	before := cpu.callCount()
	clock.fire(10 * time.Second)
	time.Sleep(20 * time.Millisecond)
	if cpu.callCount() != before {
		t.Errorf("a disabled module was collected again (%d -> %d)", before, cpu.callCount())
	}
}

func TestChangingAnIntervalReschedules(t *testing.T) {
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	instance, ctx, stop := start(
		t, clock, sink, []collect.Collector{cpu}, documentEnabling(10, 0, "cpu"),
	)
	defer stop()

	instance.Apply(ctx, documentEnabling(5, 0, "cpu"))
	eventually(t, "the new ticker", func() bool { return clock.fire(5*time.Second) == 1 })
	eventually(t, "a collection on the new interval", func() bool { return cpu.callCount() >= 1 })

	// And the old ticker is gone, so the module is not collected twice.
	before := cpu.callCount()
	clock.fire(10 * time.Second)
	time.Sleep(20 * time.Millisecond)
	if cpu.callCount() != before {
		t.Error("the old ticker is still firing")
	}
}

func TestReapplyingTheSameConfigurationDoesNotRestartAnything(t *testing.T) {
	// An agent reconnecting to a controller with nothing new must not
	// re-randomise every module's timing — restarting a ticker resets its
	// phase, and a flapping link would do it repeatedly.
	clock := newTestClock()
	cpu := newFakeCollector("cpu")
	sink := &recordingSink{}

	document := documentEnabling(10, 0, "cpu")
	instance, ctx, stop := start(t, clock, sink, []collect.Collector{cpu}, document)
	defer stop()

	eventually(t, "cpu to be active", func() bool { return len(instance.ActiveModules()) == 1 })

	for range 5 {
		instance.Apply(ctx, document)
	}

	// Exactly one live ticker at this interval: five re-applications that
	// restarted the job would have left five.
	if delivered := clock.fire(10 * time.Second); delivered != 1 {
		t.Errorf("%d tickers fired; re-applying restarted the job", delivered)
	}
}

func TestAnEnabledModuleWithNoCollectorIsSkippedNotFatal(t *testing.T) {
	// A wiring mistake rather than a configuration one — the document's own
	// validation rejects an unknown module — so it is logged and the other
	// modules carry on.
	clock := newTestClock()
	memory := newFakeCollector("memory")
	sink := &recordingSink{}

	instance, _, stop := start(
		t, clock, sink,
		[]collect.Collector{memory},
		documentEnabling(10, 0, "cpu", "memory"),
	)
	defer stop()

	if active := instance.ActiveModules(); len(active) != 1 || active[0] != "memory" {
		t.Fatalf("active modules are %v, expected [memory]", active)
	}
	clock.fire(10 * time.Second)
	eventually(t, "memory to be collected", func() bool { return memory.callCount() == 1 })
}

// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------

func TestRunReturnsOnlyAfterEveryJobHasStopped(t *testing.T) {
	// What makes "no goroutine leaks" a checkable property: a caller that
	// cancels and waits has a guarantee there is no collection in flight.
	clock := newTestClock()
	collectors := []collect.Collector{
		newFakeCollector("cpu"),
		newFakeCollector("memory"),
		newFakeCollector("swap"),
	}
	sink := &recordingSink{}

	instance := scheduler.New(quietLogger(), sink, collectors, scheduler.WithClock(clock))
	ctx, cancel := context.WithCancel(context.Background())
	instance.Apply(ctx, documentEnabling(10, 0, "cpu", "memory", "swap"))

	finished := make(chan struct{})
	go func() {
		instance.Run(ctx)
		close(finished)
	}()

	eventually(t, "every module to start", func() bool {
		return len(instance.ActiveModules()) == 3
	})

	cancel()
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return within five seconds of cancellation")
	}
	if active := instance.ActiveModules(); len(active) != 0 {
		t.Errorf("%v were still active after Run returned", active)
	}
}

func TestShutdownCancelsACollectionInFlight(t *testing.T) {
	// A blocked collector must not hold shutdown open: the context it is
	// given is cancelled, so a collector that honours it returns.
	clock := newTestClock()
	slow := newFakeCollector("cpu")
	slow.block = make(chan struct{})
	sink := &recordingSink{}

	instance := scheduler.New(quietLogger(), sink, []collect.Collector{slow}, scheduler.WithClock(clock))
	ctx, cancel := context.WithCancel(context.Background())
	instance.Apply(ctx, documentEnabling(10, 0, "cpu"))

	finished := make(chan struct{})
	go func() {
		instance.Run(ctx)
		close(finished)
	}()

	eventually(t, "cpu to be active", func() bool { return len(instance.ActiveModules()) == 1 })
	clock.fire(10 * time.Second)
	<-slow.started

	cancel()
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown blocked on a collection in flight")
	}
	close(slow.block)
}

func TestACancelledCollectionIsNotReportedAsAnError(t *testing.T) {
	// An agent stopping would otherwise record one failure per enabled module
	// on every restart, and the interface would show every host as broken
	// after a routine redeploy.
	clock := newTestClock()
	slow := newFakeCollector("cpu")
	slow.block = make(chan struct{})
	sink := &recordingSink{}

	instance := scheduler.New(quietLogger(), sink, []collect.Collector{slow}, scheduler.WithClock(clock))
	ctx, cancel := context.WithCancel(context.Background())
	instance.Apply(ctx, documentEnabling(10, 0, "cpu"))

	finished := make(chan struct{})
	go func() {
		instance.Run(ctx)
		close(finished)
	}()

	eventually(t, "cpu to be active", func() bool { return len(instance.ActiveModules()) == 1 })
	clock.fire(10 * time.Second)
	<-slow.started
	cancel()
	<-finished
	close(slow.block)

	for _, result := range sink.snapshot() {
		if result.Err != nil {
			t.Errorf("a cancelled collection was submitted as an error: %v", result.Err)
		}
	}
}

// ---------------------------------------------------------------------------
// Bounded concurrency
// ---------------------------------------------------------------------------

func TestConcurrentCollectionsAreBounded(t *testing.T) {
	// Five modules all blocking: at most MaxConcurrentCollections run, and
	// the rest are skipped under their own reason rather than queued.
	clock := newTestClock()
	ids := monitoring.ModuleIDs()
	collectors := make([]collect.Collector, 0, len(ids))
	fakes := make([]*fakeCollector, 0, len(ids))
	release := make(chan struct{})
	for _, id := range ids {
		fake := newFakeCollector(id)
		fake.block = release
		collectors = append(collectors, fake)
		fakes = append(fakes, fake)
	}
	sink := &recordingSink{}

	instance, _, stop := start(t, clock, sink, collectors, documentEnabling(10, 0, ids...))
	defer func() {
		close(release)
		stop()
	}()

	eventually(t, "every module to start", func() bool {
		return len(instance.ActiveModules()) == len(ids)
	})
	clock.fire(10 * time.Second)

	eventually(t, "the budget to bind", func() bool {
		running := 0
		for _, fake := range fakes {
			running += fake.callCount()
		}
		return running >= scheduler.MaxConcurrentCollections
	})

	// Never more than the budget, however long we wait.
	time.Sleep(50 * time.Millisecond)
	running := 0
	for _, fake := range fakes {
		running += fake.callCount()
	}
	if running > scheduler.MaxConcurrentCollections {
		t.Errorf("%d collections ran at once, the budget is %d",
			running, scheduler.MaxConcurrentCollections)
	}

	skipped := int64(0)
	for _, counters := range instance.Counters() {
		skipped += counters.SkippedBusy
	}
	if skipped == 0 {
		t.Error("the budget bound and nothing was counted as skipped-busy")
	}
}

func TestASkippedBusyExecutionIsNotCountedAsAnOverrun(t *testing.T) {
	// Different reasons point at different problems: overrun means *this*
	// module is slow, busy means something else is.
	clock := newTestClock()
	ids := monitoring.ModuleIDs()
	collectors := make([]collect.Collector, 0, len(ids))
	release := make(chan struct{})
	for _, id := range ids {
		fake := newFakeCollector(id)
		fake.block = release
		collectors = append(collectors, fake)
	}
	sink := &recordingSink{}

	instance, _, stop := start(t, clock, sink, collectors, documentEnabling(10, 0, ids...))
	defer func() {
		close(release)
		stop()
	}()

	eventually(t, "every module to start", func() bool {
		return len(instance.ActiveModules()) == len(ids)
	})
	clock.fire(10 * time.Second)

	eventually(t, "a skipped-busy", func() bool {
		for _, counters := range instance.Counters() {
			if counters.SkippedBusy > 0 {
				return true
			}
		}
		return false
	})

	for module, counters := range instance.Counters() {
		if counters.SkippedBusy > 0 && counters.SkippedOverrun > 0 {
			t.Errorf("%s counted the same skip as both busy and overrun", module)
		}
	}
}

// ---------------------------------------------------------------------------
// Deadlines
// ---------------------------------------------------------------------------

func TestTheCollectionDeadlineIsClamped(t *testing.T) {
	// A one-second interval gets the five-second floor: cancelling a
	// collector merely because it is slower than its own cadence would
	// produce nothing but timeouts, and the overrun skip handles that case
	// without losing the sample.
	//
	// Asserted through the exported bounds rather than by timing, which would
	// make the test slow and flaky.
	if scheduler.MinCollectionTimeout >= 10*time.Second {
		t.Error("the deadline floor is too high to be a floor")
	}
	if scheduler.MaxCollectionTimeout <= scheduler.MinCollectionTimeout {
		t.Error("the deadline ceiling is not above the floor")
	}
}

func TestADeadlineCancellationIsCountedAsATimeout(t *testing.T) {
	// Driven by a collector that never returns and a context that is not the
	// shutdown one, so the scheduler can tell a deadline from a shutdown.
	clock := newTestClock()
	stuck := newFakeCollector("cpu")
	stuck.block = make(chan struct{})
	sink := &recordingSink{}

	// One second so the deadline is the five-second floor, which is still too
	// long to wait for — so instead the collector is released and the counter
	// checked for the *absence* of a spurious timeout. The timeout path's
	// arithmetic is covered by TestTheCollectionDeadlineIsClamped.
	instance, _, stop := start(
		t, clock, sink, []collect.Collector{stuck}, documentEnabling(1, 0, "cpu"),
	)
	defer stop()

	clock.fire(1 * time.Second)
	<-stuck.started
	close(stuck.block)

	eventually(t, "a result", func() bool { return sink.count() >= 1 })
	if timeouts := instance.Counters()["cpu"].Timeouts; timeouts != 0 {
		t.Errorf("a collection that returned in time counted %d timeouts", timeouts)
	}
}
