// Package telemetry buffers collected measurements until the controller has
// stored them.
//
// # Bounded, and the bound is the whole design
//
// A disk filled by a monitoring agent is a self-inflicted outage on a machine
// the operator cares about, so the queue has a hard capacity and **drops the
// oldest entries first** when it is reached
// (docs/AGENT_ARCHITECTURE.md §8). Oldest-first keeps the most recent — and
// most diagnostically useful — data, and every drop is counted and reported on
// the next heartbeat rather than being silent.
//
// # In memory, not on disk, and that is a documented limitation
//
// AGENT_ARCHITECTURE.md §8 says samples are "buffered on disk". They are not,
// in this phase, and the consequence is precise: **an agent restart loses
// whatever is queued.** Deferred rather than done because disk buffering needs
// its own on-disk format, rotation, corruption handling and a size bound that
// interacts with the credential directory — and the loss window is bounded by
// how long the queue holds data, which at the default intervals is minutes.
//
// What makes that survivable is that a controller outage does not need the
// queue to be durable: the agent reconnects and re-sends, and only a restart
// *during* an outage loses anything. See ADR-051.
//
// # Delivery is at-least-once
//
// An entry leaves the queue when the controller acknowledges it, not when it
// is sent. A batch sent and not acknowledged — a dropped connection, a
// controller restart — is still queued when the socket comes back, so it is
// sent again. The controller's storage is keyed on (series, timestamp) and the
// insert is idempotent, so a duplicate changes nothing.
//
// This is deliberately not exactly-once, and the sequence number does not make
// it so: it is monotonic per *process* and resets on restart, so it is for
// gap and duplicate reporting rather than for deduplication.
package telemetry

import (
	"sync"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/scheduler"
)

// Queue capacity bounds.
//
// DefaultCapacity holds roughly ten minutes of the fastest realistic
// configuration: five modules at the five-second preset is one entry a second,
// so 1,024 entries is about seventeen minutes. Ten minutes is the figure worth
// having — it is longer than a controller restart and shorter than anything
// that would make the memory interesting.
//
// MaxCapacity exists because the value is operator-configurable and a
// configuration that could fill memory is one the agent should refuse. Each
// entry holds a module's samples — around thirty for a reference host — so
// 65,536 entries is a few hundred megabytes at worst, which is the point at
// which an agent has stopped being a monitoring agent.
const (
	DefaultCapacity = 1024
	MaxCapacity     = 65536
)

// MaxBatchEntries and MaxBatchSamples bound one outgoing frame.
//
// Mirrors the controller's limits (hcc/agents/protocol.py). Enforced by the
// *sender* as well, because a frame the controller refuses for being too large
// is a frame whose entries stay queued forever — the agent would re-send the
// same oversized batch on every reconnection and never make progress.
const (
	MaxBatchEntries = 32
	MaxBatchSamples = 512
)

// Entry is one module's collection, ready to send.
//
// A copy of the scheduler's Result rather than the Result itself, because a
// queued entry outlives the collection that produced it and must not share a
// slice with anything still being written.
type Entry struct {
	Module        string
	SchemaVersion int
	CollectedAt   time.Time
	DurationMs    int
	// Error is the collector's failure, if it had one. Sent *with* an empty
	// sample list: a module that has been failing for an hour is information
	// an operator needs, and an entry carrying only an absence could not
	// express it.
	Error   string
	Samples []collect.Sample
}

// Stats is what the heartbeat reports and the agent health view shows.
type Stats struct {
	// Queued is entries waiting to be sent.
	Queued int
	// InFlight is entries sent and not yet acknowledged.
	InFlight int
	// Dropped is the total ever discarded for want of capacity. Cumulative
	// rather than since-last-report, so a reader that missed a heartbeat does
	// not under-count — the controller stores the number and can difference it
	// itself.
	Dropped int64
	// Sequence is the last sequence number allocated.
	Sequence int64
	// Capacity is the configured bound, so "queued: 1024" can be read against
	// it rather than requiring the reader to know the default.
	Capacity int
}

// Queue holds entries until the controller has stored them.
//
// Safe for concurrent use: the scheduler enqueues from its collecting
// goroutines and the connection drains from its reader goroutine.
type Queue struct {
	capacity int

	mu sync.Mutex
	// entries is the pending queue, oldest first. A slice rather than a ring
	// buffer: the operations are append, take-from-front and
	// drop-from-front, all of which a slice does well at this size, and a
	// ring buffer would make the oldest-first drop policy harder to read for
	// no measurable gain at a thousand entries.
	entries []Entry
	// inFlight is what the last Drain handed out, kept so a lost
	// acknowledgement can be re-sent rather than lost.
	inFlight []Entry
	// inFlightSeq identifies the batch in flight. Zero means none.
	inFlightSeq int64
	sequence    int64
	dropped     int64

	// signal is closed-and-replaced on every enqueue, so a waiter can select
	// on it. A buffered channel of size one would coalesce too, but a waiter
	// that arrived between the put and its own select would miss the wakeup;
	// the closed-channel idiom has no such window.
	signal chan struct{}
}

// New builds a queue.
//
// A capacity of zero or below uses [DefaultCapacity]; one above [MaxCapacity]
// is clamped. Clamped rather than refused, because this is reached from
// configuration and an agent that would not start because its queue was
// configured too large would be worse than one that logged and continued —
// the caller validates the value before it gets here.
func New(capacity int) *Queue {
	switch {
	case capacity <= 0:
		capacity = DefaultCapacity
	case capacity > MaxCapacity:
		capacity = MaxCapacity
	}
	return &Queue{capacity: capacity, signal: make(chan struct{})}
}

// Enqueue adds a collection's result, dropping the oldest if the queue is full.
//
// Never blocks and never fails. A scheduler that could be blocked by a full
// queue would turn a controller outage into collection overruns, and the
// counters would then describe the wrong problem.
func (q *Queue) Enqueue(result scheduler.Result) {
	entry := Entry{
		Module:        result.Module,
		SchemaVersion: result.SchemaVersion,
		CollectedAt:   result.At,
		DurationMs:    int(result.Duration.Milliseconds()),
		Samples:       result.Samples,
	}
	if result.Err != nil {
		entry.Error = result.Err.Error()
	}

	q.mu.Lock()
	if len(q.entries) >= q.capacity {
		// Oldest first. The most recent data is the most diagnostically
		// useful, and an operator looking at a gap after an outage would
		// rather it were at the start of the outage than at the end.
		dropping := len(q.entries) - q.capacity + 1
		q.entries = q.entries[dropping:]
		q.dropped += int64(dropping)
	}
	q.entries = append(q.entries, entry)
	// Woken after the append, so a waiter that returns immediately finds the
	// entry rather than an empty queue — but only when there is nothing in
	// flight, for the reason [Queue.Notify] gives. An enqueue during an
	// unacknowledged batch is picked up by the wakeup in [Queue.Ack].
	if len(q.inFlight) == 0 {
		q.wakeLocked()
	}
	q.mu.Unlock()
}

// Notify returns a channel closed when there is something to send *now*.
//
// Re-read on every wait: the channel is replaced after each wakeup, so a
// caller that held the old one would never be woken again.
//
// # An in-flight batch is not "something to send"
//
// This is the subtlety, and getting it wrong is a busy loop. One batch is in
// flight at a time, and it is re-sent on the *next connection* — a session's
// exit calls [Queue.Requeue], which puts it back among the pending entries.
// While it is in flight there is nothing for this connection to do until the
// controller acknowledges it.
//
// Reporting an in-flight batch as work made the agent's session loop fire its
// send case on every iteration, re-encode the same batch, and send it again as
// fast as the socket allowed — flooding the controller, tripping its sample
// rate limiter, and starving the heartbeat timer so the connection then died
// of an idle timeout. Found by a test that wanted a heartbeat to report a
// growing queue and never got one.
func (q *Queue) Notify() <-chan struct{} {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.entries) > 0 && len(q.inFlight) == 0 {
		// Already something to do. Returned closed, so a select fires at once
		// rather than waiting for the next enqueue — which is what makes a
		// reconnecting agent drain its backlog immediately.
		closed := make(chan struct{})
		close(closed)
		return closed
	}
	return q.signal
}

func (q *Queue) wakeLocked() {
	close(q.signal)
	q.signal = make(chan struct{})
}

// Drain takes the next batch to send, marking it in flight.
//
// Returns the batch's sequence number and its entries. An empty slice means
// there is nothing to send.
//
// **Re-returns the in-flight batch if one is outstanding**, rather than taking
// a new one. That is what makes delivery at-least-once across a reconnection:
// a batch sent and not acknowledged is sent again, with its original sequence
// number, so the controller's acknowledgement still matches. Sending a *new*
// batch while one was unacknowledged would also need a window and an
// out-of-order acknowledgement path, for no benefit at this scale — one batch
// in flight at a time is enough to saturate a socket carrying thirty samples a
// second.
func (q *Queue) Drain() (int64, []Entry) {
	q.mu.Lock()
	defer q.mu.Unlock()

	if len(q.inFlight) > 0 {
		batch := make([]Entry, len(q.inFlight))
		copy(batch, q.inFlight)
		return q.inFlightSeq, batch
	}
	if len(q.entries) == 0 {
		return 0, nil
	}

	take := len(q.entries)
	if take > MaxBatchEntries {
		take = MaxBatchEntries
	}
	// Also bounded by samples, because thirty-two entries of a per-core CPU
	// series on a large host would exceed the controller's sample limit and be
	// refused — and a refused batch stays queued forever, re-sent identically
	// on every reconnection.
	samples := 0
	limit := 0
	for limit < take {
		next := samples + len(q.entries[limit].Samples)
		if limit > 0 && next > MaxBatchSamples {
			break
		}
		samples = next
		limit++
	}
	if limit == 0 {
		// One entry on its own exceeds the sample limit. Taken anyway: the
		// controller would refuse it, but holding it back means it is re-tried
		// forever and blocks everything behind it. Sending it gets a rejection
		// the operator can see, and the acknowledgement clears it.
		limit = 1
	}

	q.sequence++
	q.inFlightSeq = q.sequence
	q.inFlight = make([]Entry, limit)
	copy(q.inFlight, q.entries[:limit])
	q.entries = q.entries[limit:]

	batch := make([]Entry, limit)
	copy(batch, q.inFlight)
	return q.inFlightSeq, batch
}

// Ack discards the in-flight batch, if the sequence number matches.
//
// The match matters: an acknowledgement for a sequence this agent is not
// waiting on is a stale frame from a previous connection, and acting on it
// would discard a batch the controller has not stored.
func (q *Queue) Ack(seq int64) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.inFlightSeq == 0 || seq != q.inFlightSeq {
		return false
	}
	q.inFlight = nil
	q.inFlightSeq = 0
	if len(q.entries) > 0 {
		// More to send, so a waiter is woken rather than waiting for the next
		// collection — which is what drains a backlog promptly.
		q.wakeLocked()
	}
	return true
}

// Requeue returns the in-flight batch to the front of the queue.
//
// Called when a connection drops. The batch goes to the *front* rather than the
// back, so order is preserved — a chart drawn from samples that arrived out of
// order is still correct, since every sample carries its own timestamp, but a
// queue that reordered would make the drop policy discard the wrong entries.
//
// Entries that no longer fit are dropped from the oldest end, exactly as an
// enqueue would, and counted.
func (q *Queue) Requeue() {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.inFlight) == 0 {
		return
	}
	combined := make([]Entry, 0, len(q.inFlight)+len(q.entries))
	combined = append(combined, q.inFlight...)
	combined = append(combined, q.entries...)
	if len(combined) > q.capacity {
		dropping := len(combined) - q.capacity
		combined = combined[dropping:]
		q.dropped += int64(dropping)
	}
	q.entries = combined
	q.inFlight = nil
	q.inFlightSeq = 0
	if len(q.entries) > 0 {
		q.wakeLocked()
	}
}

// Stats reports the queue's state.
func (q *Queue) Stats() Stats {
	q.mu.Lock()
	defer q.mu.Unlock()
	return Stats{
		Queued:   len(q.entries),
		InFlight: len(q.inFlight),
		Dropped:  q.dropped,
		Sequence: q.sequence,
		Capacity: q.capacity,
	}
}

// Submit implements [scheduler.Sink].
//
// A method rather than a wrapper function, so the queue can be handed to the
// scheduler directly — and so there is no adapter whose job is to forget a
// field.
func (q *Queue) Submit(result scheduler.Result) { q.Enqueue(result) }
