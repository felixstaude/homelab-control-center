// Package scheduler runs collectors on their configured schedules.
//
// One goroutine per enabled module, a bounded worker budget across all of
// them, and counters for every way a collection can fail to happen.
//
// # Each module is scheduled independently
//
// Not one global report cycle. A shared loop would make every module's cadence
// the slowest module's, and one wedged collector would stall the others —
// which is the failure docs/SCHEDULING.md §7.2 forbids. So each enabled module
// has its own ticker and its own goroutine, and a module that cannot read its
// pseudo-file costs only its own series.
//
// # Overrun skips, it never queues
//
// If a collection is still running when the next is due, the due one is
// **skipped and counted**. Queueing would let a slow collector build an
// unbounded backlog of samples whose timestamps are already wrong, and the
// backlog would grow for as long as the collector stayed slow. Skipping keeps
// the schedule honest: the series has a gap and the gap has a counter
// explaining it.
//
// # Drift does not accumulate
//
// A naive loop — collect, then sleep for the interval — makes every
// collection's duration push the next one later. At a 5-second interval with a
// 200 ms collector that is about four minutes of drift a day. time.Ticker
// fires on a fixed cadence regardless of how long the handler takes, so the
// phase is anchored to when the ticker started; the rule is written down here
// so a future refactor does not replace it with a sleep loop.
//
// # Jitter perturbs executions, not the phase
//
// Each execution waits a random amount in [0, jitter) *after* its tick. The
// ticker is untouched, so jitter cannot accumulate into drift. It matters at
// fleet scale and is not cosmetic: twenty agents on a 30-second interval, all
// enrolled the same afternoon, otherwise align and deliver twenty simultaneous
// batches every thirty seconds — turning smooth load into spikes and making
// rate limits fire on honest traffic.
package scheduler

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"sort"
	"sync"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// MaxConcurrentCollections bounds how many collectors may run at once.
//
// Four, across every module. The five collectors in this phase read a
// pseudo-file and return in microseconds, so this is never reached in
// practice — it exists so that a pathological case (five modules at a
// one-second interval on a host where /proc has become slow) is a bounded
// number of goroutines rather than an unbounded one. An execution that cannot
// get a slot is **skipped and counted**, never queued.
const MaxConcurrentCollections = 4

// Collection deadline bounds. A collector that exceeds its deadline is
// cancelled, counted, and the next execution proceeds normally.
//
// The deadline is the module's interval, clamped: a one-second interval gets
// five seconds rather than one, because cancelling a collector that is merely
// slower than its own cadence would produce nothing but timeouts; and nothing
// gets more than a minute, because a /proc read that has taken a minute is not
// going to finish.
const (
	MinCollectionTimeout = 5 * time.Second
	MaxCollectionTimeout = 60 * time.Second
)

// Clock abstracts time so the scheduler is testable.
//
// The alternative is a test suite that sleeps through real intervals, which is
// slow and flaky. Same device, and the same justification, as the agent's
// supervision loop: a scheduler that cannot be tested deterministically is one
// nobody will change with confidence.
type Clock interface {
	Now() time.Time
	// Ticker returns a ticker firing every d. Returned as an interface so a
	// test can fire it on demand rather than waiting.
	Ticker(d time.Duration) Ticker
	// Sleep blocks for d or until ctx is done, reporting whether it completed.
	Sleep(ctx context.Context, d time.Duration) bool
}

// Ticker is the subset of time.Ticker the scheduler uses.
type Ticker interface {
	C() <-chan time.Time
	Stop()
}

// RealClock is the production Clock.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) Ticker(d time.Duration) Ticker { return &realTicker{inner: time.NewTicker(d)} }

func (RealClock) Sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return true
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

type realTicker struct{ inner *time.Ticker }

func (t *realTicker) C() <-chan time.Time { return t.inner.C }
func (t *realTicker) Stop()               { t.inner.Stop() }

// Result is one completed collection, successful or not.
//
// Carries the failure as a field rather than being returned alongside an
// error, because a *failed* collection is still something the controller
// should know about: §26 asks for collector errors to be observable, and a
// module that has been failing for an hour is information an operator needs
// rather than an absence they have to infer.
type Result struct {
	Module string
	// SchemaVersion is the module's declared metric payload version, so the
	// controller can interpret a payload from an agent older than itself.
	SchemaVersion int
	// At is when the collection *started*, which is the instant the samples
	// describe. Not when it finished: for a collector that took 200 ms, the
	// start is the better timestamp and the duration is reported separately.
	At       time.Time
	Duration time.Duration
	Samples  []collect.Sample
	// Err is set when the collection failed. Samples is then empty.
	Err error
}

// Sink receives completed collections.
//
// An interface so the scheduler does not depend on the telemetry queue, which
// does not exist yet — and so a test can assert what was collected without a
// controller. The implementation must not block for long: it is called from
// the collecting goroutine, and a slow sink would turn into overrun skips on
// every module.
type Sink interface {
	Submit(Result)
}

// SinkFunc adapts a function to [Sink].
type SinkFunc func(Result)

func (f SinkFunc) Submit(result Result) { f(result) }

// Counters is why a module's series looks the way it does.
//
// Exposed per module so "why is this graph sparse?" is answerable without
// reading logs — which is the whole point of docs/SCHEDULING.md §7.3. Each
// field is a *different* reason, and collapsing them into one "failures" count
// would lose the distinction an operator needs: a collector that is erroring
// has a different remedy from one that is being skipped for overrun.
type Counters struct {
	// Executions completed, successfully or not.
	Executions int64
	// Samples submitted across every execution.
	Samples int64
	// SkippedOverrun: the previous execution of this module was still running.
	SkippedOverrun int64
	// SkippedBusy: the global concurrency budget was exhausted. A different
	// reason from overrun — this module was ready and something else was
	// running — and conflating them would point at the wrong module.
	SkippedBusy int64
	// Timeouts: cancelled for exceeding the collection deadline.
	Timeouts int64
	// Errors: ran and failed.
	Errors int64
}

// Scheduler runs collectors on their schedules.
type Scheduler struct {
	clock     Clock
	logger    *slog.Logger
	sink      Sink
	collector map[string]collect.Collector

	// semaphore bounds concurrent collections across every module.
	semaphore chan struct{}

	mu       sync.Mutex
	jobs     map[string]*job
	counters map[string]*Counters
	// running is the document in force, kept so Apply can tell a real change
	// from a repeat — an agent that reconnects and is sent the same
	// configuration must not restart every ticker, because restarting a
	// ticker resets its phase and a flapping link would re-randomise every
	// module's timing.
	running monitoring.Document
	started bool
}

// Option customises a Scheduler.
type Option func(*Scheduler)

// WithClock replaces the scheduler's clock. Intended for tests.
func WithClock(clock Clock) Option {
	return func(s *Scheduler) { s.clock = clock }
}

// New builds a Scheduler over a set of collectors.
//
// Collectors are supplied rather than constructed here, so the agent decides
// which /proc root and which options they use — and so a test can substitute
// one that counts its calls.
func New(logger *slog.Logger, sink Sink, collectors []collect.Collector, opts ...Option) *Scheduler {
	scheduler := &Scheduler{
		clock:     RealClock{},
		logger:    logger,
		sink:      sink,
		collector: make(map[string]collect.Collector, len(collectors)),
		semaphore: make(chan struct{}, MaxConcurrentCollections),
		jobs:      make(map[string]*job),
		counters:  make(map[string]*Counters),
	}
	for _, collector := range collectors {
		scheduler.collector[collector.Module()] = collector
	}
	for _, opt := range opts {
		opt(scheduler)
	}
	return scheduler
}

// job is one module's scheduled collection.
type job struct {
	module   string
	schedule monitoring.Schedule
	cancel   context.CancelFunc
	done     chan struct{}
	// ticker is created when the job is registered, not inside its goroutine.
	//
	// Two reasons, and the second is the one that made it a bug. The phase
	// anchor is when the ticker starts, so creating it at registration makes
	// the anchor the moment the operator's change took effect rather than
	// whenever the Go scheduler got round to the goroutine. And a caller that
	// observes the job as registered can rely on its ticker existing —
	// without that, this package's own test suite fired a tick into a module
	// that was listed as active and had no ticker yet, and hung waiting for a
	// collection that could never start.
	ticker Ticker
	// collecting is held for the duration of one collection, so the next tick
	// can tell "still running" from "ready" without blocking on it.
	collecting sync.Mutex
	busy       bool
	busyMu     sync.Mutex
}

// Apply reconfigures the scheduler to match a document.
//
// Starts jobs for modules that are now enabled, stops those that are not, and
// restarts those whose schedule changed. Safe to call repeatedly with the same
// document: an unchanged module's job is left alone, including its ticker
// phase, so an agent reconnecting to a controller that has nothing new does
// not re-randomise every module's timing.
//
// Must be called while [Scheduler.Run] is active; before it, the document is
// recorded and applied when Run starts.
func (s *Scheduler) Apply(ctx context.Context, document monitoring.Document) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.running = document
	if !s.started {
		// Recorded and applied by Run. Starting goroutines now would leak
		// them if Run were never called.
		return
	}
	s.reconcileLocked(ctx, document)
}

// reconcileLocked brings the running jobs into line with a document.
//
// Caller holds s.mu.
func (s *Scheduler) reconcileLocked(ctx context.Context, document monitoring.Document) {
	wanted := make(map[string]monitoring.Schedule, len(document.Modules))
	for id, config := range document.Modules {
		if !config.Enabled {
			continue
		}
		if _, have := s.collector[id]; !have {
			// A module the controller enabled and this build cannot collect.
			// The document's own validation rejects this, so reaching here
			// means a collector was not registered — a wiring mistake rather
			// than a configuration one, and logged as such.
			s.logger.Warn("no collector for an enabled module", "module", id)
			continue
		}
		wanted[id] = config.Schedule
	}

	// Stopped first, so a module whose interval changed releases its slot
	// before its replacement asks for one.
	for id, existing := range s.jobs {
		schedule, keep := wanted[id]
		if keep && sameSchedule(existing.schedule, schedule) {
			continue
		}
		s.stopJobLocked(id, existing)
		if keep {
			s.logger.Info("rescheduling a module", "module", id,
				"interval_seconds", schedule.IntervalSeconds)
		} else {
			s.logger.Info("stopping collection for a module", "module", id)
		}
	}

	for id, schedule := range wanted {
		if _, running := s.jobs[id]; running {
			continue
		}
		s.startJobLocked(ctx, id, schedule)
	}
}

func (s *Scheduler) stopJobLocked(id string, existing *job) {
	existing.cancel()
	// Stopped here as well as in the goroutine's defer, so a replacement
	// ticker at the same interval cannot receive a tick intended for the job
	// being retired — the goroutine's defer runs whenever it next wakes,
	// which may be after the replacement is already registered.
	existing.ticker.Stop()
	delete(s.jobs, id)
	// Not waited for here: the goroutine may be mid-collection, and holding
	// s.mu while it finishes would block every other reconfiguration — and a
	// collector with a 60-second deadline would block them for a minute.
	// Shutdown waits; reconfiguration does not need to.
}

func (s *Scheduler) startJobLocked(ctx context.Context, id string, schedule monitoring.Schedule) {
	interval := schedule.Interval()
	if interval <= 0 {
		// Validation refuses this, so reaching here is a bug rather than a
		// configuration. Logged and not started: a collector running at a
		// cadence nobody configured is worse than one not running, because
		// the first is invisible.
		s.logger.Error("refusing to schedule a module with no interval", "module", id)
		return
	}

	jobCtx, cancel := context.WithCancel(ctx)
	entry := &job{
		module:   id,
		schedule: schedule,
		cancel:   cancel,
		done:     make(chan struct{}),
		ticker:   s.clock.Ticker(interval),
	}
	s.jobs[id] = entry
	if _, have := s.counters[id]; !have {
		s.counters[id] = &Counters{}
	}
	s.logger.Info("collecting a module", "module", id,
		"interval_seconds", schedule.IntervalSeconds,
		"jitter_seconds", schedule.JitterSeconds)
	go s.runJob(jobCtx, entry)
}

// Run drives the scheduler until ctx is cancelled.
//
// Returns when every job has stopped, so a caller that cancels and waits has a
// guarantee there is no collection in flight — which is what makes "no
// goroutine leaks" a checkable property rather than a hope.
func (s *Scheduler) Run(ctx context.Context) {
	s.mu.Lock()
	s.started = true
	s.reconcileLocked(ctx, s.running)
	s.mu.Unlock()

	<-ctx.Done()

	// Collected under the lock and waited for outside it: a job's goroutine
	// takes no lock this function holds, but waiting while holding s.mu would
	// deadlock against anything that logged a counter.
	s.mu.Lock()
	pending := make([]*job, 0, len(s.jobs))
	for id, entry := range s.jobs {
		entry.cancel()
		pending = append(pending, entry)
		delete(s.jobs, id)
	}
	s.started = false
	s.mu.Unlock()

	for _, entry := range pending {
		<-entry.done
	}
	s.logger.Info("collection stopped", "modules", len(pending))
}

// runJob is one module's loop.
func (s *Scheduler) runJob(ctx context.Context, entry *job) {
	defer close(entry.done)
	defer entry.ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-entry.ticker.C():
			s.tick(ctx, entry)
		}
	}
}

// tick handles one due execution.
//
// Returns immediately on a skip, so the ticker's cadence is never delayed by
// the decision not to collect.
func (s *Scheduler) tick(ctx context.Context, entry *job) {
	if entry.markBusy() {
		s.bump(entry.module, func(c *Counters) { c.SkippedOverrun++ })
		s.logger.Warn("skipping a collection: the previous one is still running",
			"module", entry.module)
		return
	}

	select {
	case s.semaphore <- struct{}{}:
	default:
		// The budget is exhausted. Skipped rather than queued, and counted
		// under its own reason: this module was ready and something else was
		// running, which points at a different problem from overrun.
		entry.clearBusy()
		s.bump(entry.module, func(c *Counters) { c.SkippedBusy++ })
		s.logger.Warn("skipping a collection: too many are already running",
			"module", entry.module, "limit", MaxConcurrentCollections)
		return
	}

	go func() {
		defer func() {
			<-s.semaphore
			entry.clearBusy()
		}()
		s.collectOnce(ctx, entry)
	}()
}

// collectOnce runs one collection, with jitter and a deadline.
func (s *Scheduler) collectOnce(ctx context.Context, entry *job) {
	if jitter := entry.schedule.Jitter(); jitter > 0 {
		// A random offset in [0, jitter). Applied here rather than to the
		// ticker, so it perturbs this execution and cannot accumulate into
		// drift.
		delay := time.Duration(rand.Int64N(int64(jitter)))
		if !s.clock.Sleep(ctx, delay) {
			return
		}
	}

	collector, found := s.collector[entry.module]
	if !found {
		return
	}

	timeout := collectionTimeout(entry.schedule.Interval())
	collectCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	startedAt := s.clock.Now()
	samples, err := collector.Collect(collectCtx)
	duration := s.clock.Now().Sub(startedAt)

	switch {
	case err != nil:
		timedOut := collectCtx.Err() != nil && ctx.Err() == nil
		s.bump(entry.module, func(c *Counters) {
			c.Executions++
			c.Errors++
			if timedOut {
				c.Timeouts++
			}
		})
		if ctx.Err() != nil {
			// Shutdown, not a collector fault. Not reported as an error: an
			// agent stopping would otherwise record one failure per enabled
			// module on every restart.
			return
		}
		s.logger.Warn("a collection failed",
			"module", entry.module, "timed_out", timedOut, "error", err)
	default:
		s.bump(entry.module, func(c *Counters) {
			c.Executions++
			c.Samples += int64(len(samples))
		})
	}

	if ctx.Err() != nil {
		return
	}
	s.sink.Submit(Result{
		Module:        entry.module,
		SchemaVersion: schemaVersionOf(entry.module),
		At:            startedAt,
		Duration:      duration,
		Samples:       samples,
		Err:           err,
	})
}

// collectionTimeout is how long a collection may take.
//
// The module's interval, clamped. A one-second interval gets the five-second
// floor rather than one second: cancelling a collector merely because it is
// slower than its own cadence would produce nothing but timeouts, and the
// overrun skip already handles that case without losing the sample.
func collectionTimeout(interval time.Duration) time.Duration {
	switch {
	case interval < MinCollectionTimeout:
		return MinCollectionTimeout
	case interval > MaxCollectionTimeout:
		return MaxCollectionTimeout
	default:
		return interval
	}
}

func schemaVersionOf(module string) int {
	if descriptor, found := monitoring.Lookup(module); found {
		return descriptor.DataSchemaVersion
	}
	return 0
}

// Counters returns a snapshot of every module's counters.
func (s *Scheduler) Counters() map[string]Counters {
	s.mu.Lock()
	defer s.mu.Unlock()
	snapshot := make(map[string]Counters, len(s.counters))
	for id, counters := range s.counters {
		snapshot[id] = *counters
	}
	return snapshot
}

// ActiveModules returns the modules currently being collected, sorted.
func (s *Scheduler) ActiveModules() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	active := make([]string, 0, len(s.jobs))
	for id := range s.jobs {
		active = append(active, id)
	}
	sort.Strings(active)
	return active
}

func (s *Scheduler) bump(module string, apply func(*Counters)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	counters, found := s.counters[module]
	if !found {
		counters = &Counters{}
		s.counters[module] = counters
	}
	apply(counters)
}

// markBusy claims the module, reporting whether it was already claimed.
//
// A flag under its own mutex rather than a TryLock on `collecting`: the
// semantics wanted are "is a collection in flight", which has to be readable
// from the ticker goroutine without blocking, and a mutex that is held for the
// duration of a collection cannot be interrogated that way.
func (j *job) markBusy() (alreadyBusy bool) {
	j.busyMu.Lock()
	defer j.busyMu.Unlock()
	if j.busy {
		return true
	}
	j.busy = true
	return false
}

func (j *job) clearBusy() {
	j.busyMu.Lock()
	j.busy = false
	j.busyMu.Unlock()
}

// sameSchedule reports whether two schedules would behave identically.
//
// Compared field by field rather than with reflect.DeepEqual, because the
// calendar slices are irrelevant for an interval schedule and a nil-versus-
// empty difference in one of them would restart a ticker for no reason — which
// re-randomises the module's phase.
func sameSchedule(left, right monitoring.Schedule) bool {
	if left.Kind != right.Kind {
		return false
	}
	switch left.Kind {
	case monitoring.KindInterval:
		return left.IntervalSeconds == right.IntervalSeconds &&
			left.JitterSeconds == right.JitterSeconds
	case monitoring.KindCalendar:
		return left.Timezone == right.Timezone &&
			equalStrings(left.Times, right.Times) &&
			equalInts(left.DaysOfWeek, right.DaysOfWeek) &&
			equalInts(left.DaysOfMonth, right.DaysOfMonth) &&
			equalInts(left.Months, right.Months) &&
			left.OnMissed == right.OnMissed &&
			left.MissedGraceSeconds == right.MissedGraceSeconds &&
			left.DstNonexistent == right.DstNonexistent &&
			left.DstAmbiguous == right.DstAmbiguous
	default:
		return false
	}
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func equalInts(left, right []int) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
