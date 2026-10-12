package telemetry_test

import (
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/scheduler"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/telemetry"
)

func result(module string, samples int, at time.Time) scheduler.Result {
	collected := make([]collect.Sample, 0, samples)
	for index := range samples {
		collected = append(collected, collect.Sample{
			Metric: "utilisation",
			Value:  float64(index),
		})
	}
	return scheduler.Result{
		Module:        module,
		SchemaVersion: 1,
		At:            at,
		Duration:      3 * time.Millisecond,
		Samples:       collected,
	}
}

func fill(queue *telemetry.Queue, count int) {
	base := time.Now()
	for index := range count {
		queue.Enqueue(result("cpu", 1, base.Add(time.Duration(index)*time.Second)))
	}
}

// ---------------------------------------------------------------------------
// Capacity and the drop policy
// ---------------------------------------------------------------------------

func TestANewQueueIsEmpty(t *testing.T) {
	stats := telemetry.New(0).Stats()
	if stats.Queued != 0 || stats.InFlight != 0 || stats.Dropped != 0 {
		t.Errorf("a new queue is not empty: %+v", stats)
	}
	if stats.Capacity != telemetry.DefaultCapacity {
		t.Errorf("capacity is %d, expected the default %d", stats.Capacity, telemetry.DefaultCapacity)
	}
}

func TestAnAbsurdCapacityIsClamped(t *testing.T) {
	// Clamped rather than refused: this is reached from configuration, and an
	// agent that would not start because its queue was configured too large
	// is worse than one that logs and continues. The caller validates first.
	if got := telemetry.New(telemetry.MaxCapacity * 10).Stats().Capacity; got != telemetry.MaxCapacity {
		t.Errorf("capacity is %d, expected it clamped to %d", got, telemetry.MaxCapacity)
	}
	if got := telemetry.New(-5).Stats().Capacity; got != telemetry.DefaultCapacity {
		t.Errorf("capacity is %d, expected the default", got)
	}
}

func TestAFullQueueDropsTheOldestFirst(t *testing.T) {
	// AGENT_ARCHITECTURE.md §8. The most recent data is the most
	// diagnostically useful, and an operator looking at a gap after an outage
	// would rather it were at the start of the outage than at the end.
	queue := telemetry.New(4)
	base := time.Now()
	for index := range 7 {
		queue.Enqueue(result("cpu", 1, base.Add(time.Duration(index)*time.Second)))
	}

	stats := queue.Stats()
	if stats.Queued != 4 {
		t.Fatalf("queued %d, expected the capacity of 4", stats.Queued)
	}
	if stats.Dropped != 3 {
		t.Errorf("dropped %d, expected 3", stats.Dropped)
	}

	_, batch := queue.Drain()
	// Entries 0, 1 and 2 were dropped, so the oldest remaining is 3.
	if !batch[0].CollectedAt.Equal(base.Add(3 * time.Second)) {
		t.Errorf("the oldest surviving entry is %s, expected %s",
			batch[0].CollectedAt, base.Add(3*time.Second))
	}
	if !batch[len(batch)-1].CollectedAt.Equal(base.Add(6 * time.Second)) {
		t.Error("the newest entry was dropped, which is the wrong end")
	}
}

func TestDropsAreCountedCumulatively(t *testing.T) {
	// Cumulative rather than since-last-report: a reader that missed a
	// heartbeat would otherwise under-count, and the controller can
	// difference two readings itself.
	queue := telemetry.New(2)
	fill(queue, 5)
	first := queue.Stats().Dropped
	fill(queue, 5)
	second := queue.Stats().Dropped

	if second <= first {
		t.Errorf("the drop count did not grow: %d then %d", first, second)
	}
}

func TestEnqueueNeverBlocks(t *testing.T) {
	// A scheduler that could be blocked by a full queue would turn a
	// controller outage into collection overruns, and the counters would then
	// describe the wrong problem.
	queue := telemetry.New(1)
	done := make(chan struct{})
	go func() {
		fill(queue, 10_000)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Enqueue blocked")
	}
}

// ---------------------------------------------------------------------------
// Draining and acknowledging
// ---------------------------------------------------------------------------

func TestDrainingAnEmptyQueueYieldsNothing(t *testing.T) {
	seq, batch := telemetry.New(0).Drain()
	if seq != 0 || len(batch) != 0 {
		t.Errorf("an empty queue produced seq=%d, %d entries", seq, len(batch))
	}
}

func TestDrainingMarksTheBatchInFlightRatherThanRemovingIt(t *testing.T) {
	// The property at-least-once delivery rests on: an entry leaves the queue
	// when the controller acknowledges it, not when it is sent.
	queue := telemetry.New(0)
	fill(queue, 3)

	seq, batch := queue.Drain()
	stats := queue.Stats()

	if seq != 1 {
		t.Errorf("the first batch has sequence %d, expected 1", seq)
	}
	if len(batch) != 3 {
		t.Errorf("drained %d entries, expected 3", len(batch))
	}
	if stats.Queued != 0 {
		t.Errorf("queued %d, expected 0", stats.Queued)
	}
	if stats.InFlight != 3 {
		t.Errorf("in flight %d, expected 3", stats.InFlight)
	}
}

func TestDrainingAgainReturnsTheSameBatchUntilItIsAcknowledged(t *testing.T) {
	// What makes a dropped connection survivable: the batch is sent again,
	// with its original sequence number, so the acknowledgement still matches.
	queue := telemetry.New(0)
	fill(queue, 2)

	firstSeq, first := queue.Drain()
	secondSeq, second := queue.Drain()

	if firstSeq != secondSeq {
		t.Errorf("the sequence changed: %d then %d", firstSeq, secondSeq)
	}
	if len(first) != len(second) {
		t.Errorf("the batch changed: %d then %d entries", len(first), len(second))
	}
}

func TestAnAcknowledgementDiscardsTheBatch(t *testing.T) {
	queue := telemetry.New(0)
	fill(queue, 2)
	seq, _ := queue.Drain()

	if !queue.Ack(seq) {
		t.Fatal("the acknowledgement was not accepted")
	}
	stats := queue.Stats()
	if stats.InFlight != 0 || stats.Queued != 0 {
		t.Errorf("the batch survived its acknowledgement: %+v", stats)
	}
}

func TestAnAcknowledgementForTheWrongSequenceIsIgnored(t *testing.T) {
	// A stale frame from a previous connection. Acting on it would discard a
	// batch the controller has not stored.
	queue := telemetry.New(0)
	fill(queue, 2)
	seq, _ := queue.Drain()

	if queue.Ack(seq + 99) {
		t.Fatal("an acknowledgement for an unknown batch was accepted")
	}
	if queue.Stats().InFlight != 2 {
		t.Error("the in-flight batch was discarded by a stale acknowledgement")
	}
}

func TestAnAcknowledgementWithNothingInFlightIsIgnored(t *testing.T) {
	if telemetry.New(0).Ack(1) {
		t.Fatal("an acknowledgement was accepted with nothing in flight")
	}
}

func TestSequenceNumbersAreMonotonic(t *testing.T) {
	queue := telemetry.New(0)
	var previous int64
	for round := range 5 {
		fill(queue, 1)
		seq, _ := queue.Drain()
		if seq <= previous {
			t.Fatalf("round %d: sequence %d did not advance past %d", round, seq, previous)
		}
		previous = seq
		queue.Ack(seq)
	}
}

// ---------------------------------------------------------------------------
// Requeueing
// ---------------------------------------------------------------------------

func TestRequeueingPutsTheBatchBackAtTheFront(t *testing.T) {
	// Order is preserved, so the drop policy discards the right entries. A
	// queue that reordered would drop the newest on the next overflow.
	queue := telemetry.New(0)
	base := time.Now()
	for index := range 4 {
		queue.Enqueue(result("cpu", 1, base.Add(time.Duration(index)*time.Second)))
	}

	// Drain everything, then enqueue one more before requeueing.
	seq, batch := queue.Drain()
	if len(batch) != 4 {
		t.Fatalf("drained %d, expected 4", len(batch))
	}
	queue.Enqueue(result("cpu", 1, base.Add(10*time.Second)))
	queue.Requeue()

	_, after := queue.Drain()
	if len(after) != 5 {
		t.Fatalf("after requeueing there are %d entries, expected 5", len(after))
	}
	if !after[0].CollectedAt.Equal(base) {
		t.Errorf("the requeued batch is not at the front: first is %s", after[0].CollectedAt)
	}
	if !after[4].CollectedAt.Equal(base.Add(10 * time.Second)) {
		t.Error("the entry enqueued during the outage is not last")
	}
	// And the sequence advanced, because this is a new batch.
	if newSeq, _ := queue.Drain(); newSeq == seq {
		t.Error("the requeued batch kept its old sequence number")
	}
}

func TestRequeueingWithNothingInFlightDoesNothing(t *testing.T) {
	queue := telemetry.New(0)
	fill(queue, 2)
	queue.Requeue()
	if queue.Stats().Queued != 2 {
		t.Error("Requeue disturbed the pending queue")
	}
}

func TestRequeueingBeyondCapacityDropsTheOldest(t *testing.T) {
	queue := telemetry.New(3)
	base := time.Now()
	for index := range 3 {
		queue.Enqueue(result("cpu", 1, base.Add(time.Duration(index)*time.Second)))
	}
	_, _ = queue.Drain()
	// The queue is now empty with three in flight. Fill it again, so
	// requeueing cannot fit.
	for index := range 3 {
		queue.Enqueue(result("cpu", 1, base.Add(time.Duration(100+index)*time.Second)))
	}
	queue.Requeue()

	stats := queue.Stats()
	if stats.Queued != 3 {
		t.Errorf("queued %d, expected the capacity of 3", stats.Queued)
	}
	if stats.Dropped != 3 {
		t.Errorf("dropped %d, expected the three requeued entries", stats.Dropped)
	}
	// What survived is the newest, which is the drop policy applied
	// consistently rather than a special case for requeueing.
	_, batch := queue.Drain()
	if !batch[0].CollectedAt.Equal(base.Add(100 * time.Second)) {
		t.Errorf("the surviving entries are the wrong ones: first is %s", batch[0].CollectedAt)
	}
}

// ---------------------------------------------------------------------------
// Batch bounds
// ---------------------------------------------------------------------------

func TestABatchIsBoundedByEntryCount(t *testing.T) {
	queue := telemetry.New(0)
	fill(queue, telemetry.MaxBatchEntries*3)

	_, batch := queue.Drain()
	if len(batch) != telemetry.MaxBatchEntries {
		t.Errorf("drained %d entries, expected the limit of %d",
			len(batch), telemetry.MaxBatchEntries)
	}
}

func TestABatchIsBoundedBySampleCount(t *testing.T) {
	// Thirty-two entries of a per-core series on a large host would exceed
	// the controller's sample limit and be refused — and a refused batch stays
	// queued forever, re-sent identically on every reconnection.
	queue := telemetry.New(0)
	base := time.Now()
	for index := range 10 {
		queue.Enqueue(result("cpu", 200, base.Add(time.Duration(index)*time.Second)))
	}

	_, batch := queue.Drain()
	samples := 0
	for _, entry := range batch {
		samples += len(entry.Samples)
	}
	if samples > telemetry.MaxBatchSamples {
		t.Errorf("drained %d samples, over the limit of %d", samples, telemetry.MaxBatchSamples)
	}
	if len(batch) == 0 {
		t.Fatal("the sample bound produced an empty batch")
	}
}

func TestAnOversizedSingleEntryIsStillSent(t *testing.T) {
	// Holding it back means it is re-tried forever and blocks everything
	// behind it. Sending it gets a rejection the operator can see, and the
	// acknowledgement clears it.
	queue := telemetry.New(0)
	queue.Enqueue(result("cpu", telemetry.MaxBatchSamples*2, time.Now()))
	queue.Enqueue(result("memory", 1, time.Now()))

	_, batch := queue.Drain()
	if len(batch) != 1 {
		t.Fatalf("drained %d entries, expected the one oversized entry alone", len(batch))
	}
	if batch[0].Module != "cpu" {
		t.Errorf("drained %q, expected the oversized entry", batch[0].Module)
	}
}

// ---------------------------------------------------------------------------
// Notification
// ---------------------------------------------------------------------------

func TestNotifyFiresOnAnEnqueue(t *testing.T) {
	queue := telemetry.New(0)
	pending := queue.Notify()

	select {
	case <-pending:
		t.Fatal("an empty queue signalled that there was work")
	default:
	}

	fill(queue, 1)
	select {
	case <-pending:
	case <-time.After(time.Second):
		t.Fatal("Notify did not fire on an enqueue")
	}
}

func TestNotifyIsAlreadyClosedWhenThereIsWork(t *testing.T) {
	// What makes a reconnecting agent drain its backlog at once rather than
	// waiting for the next collection.
	queue := telemetry.New(0)
	fill(queue, 1)

	select {
	case <-queue.Notify():
	case <-time.After(time.Second):
		t.Fatal("Notify did not fire for an already-populated queue")
	}
}

func TestNotifyDoesNotFireWhileABatchIsInFlight(t *testing.T) {
	// The subtlety, and getting it wrong is a busy loop.
	//
	// This test asserted the opposite until the agent's own integration test
	// caught it: reporting an in-flight batch as work made the session loop
	// fire its send case on every iteration, re-encode the same batch and send
	// it again as fast as the socket allowed — flooding the controller,
	// tripping its sample rate limiter, and starving the heartbeat timer until
	// the connection died of an idle timeout.
	//
	// One batch is in flight at a time, and it is re-sent on the *next*
	// connection via Requeue. While it is in flight there is nothing for this
	// connection to do until the controller acknowledges.
	queue := telemetry.New(0)
	fill(queue, 1)
	_, _ = queue.Drain()

	select {
	case <-queue.Notify():
		t.Fatal("Notify fired with a batch in flight, which is a busy loop")
	case <-time.After(50 * time.Millisecond):
	}
}

func TestAnEnqueueDuringAnUnacknowledgedBatchDoesNotWake(t *testing.T) {
	// Same reason. The entry is picked up by the wakeup in Ack.
	queue := telemetry.New(0)
	fill(queue, 1)
	_, _ = queue.Drain()

	pending := queue.Notify()
	fill(queue, 1)
	select {
	case <-pending:
		t.Fatal("an enqueue woke a waiter while a batch was in flight")
	case <-time.After(50 * time.Millisecond):
	}
}

func TestAcknowledgingWakesAWaiterWhenMoreRemains(t *testing.T) {
	// What drains a backlog promptly: the agent does not wait for the next
	// collection to send the next batch.
	queue := telemetry.New(0)
	fill(queue, telemetry.MaxBatchEntries+5)
	seq, _ := queue.Drain()

	pending := queue.Notify()
	queue.Ack(seq)
	select {
	case <-pending:
	case <-time.After(time.Second):
		t.Fatal("acknowledging did not wake a waiter with more to send")
	}
}

func TestRequeueingWakesAWaiter(t *testing.T) {
	// The next connection has work immediately, rather than waiting for the
	// next collection to notice.
	queue := telemetry.New(0)
	fill(queue, 1)
	_, _ = queue.Drain()

	pending := queue.Notify()
	queue.Requeue()
	select {
	case <-pending:
	case <-time.After(time.Second):
		t.Fatal("Requeue did not wake a waiter")
	}
}

func TestANotificationChannelIsReplacedAfterEachWakeup(t *testing.T) {
	// The reason callers must re-read it: holding the old one would mean never
	// being woken again.
	queue := telemetry.New(0)
	first := queue.Notify()
	fill(queue, 1)
	<-first

	seq, _ := queue.Drain()
	queue.Ack(seq)

	second := queue.Notify()
	if first == second {
		t.Fatal("the notification channel was not replaced")
	}
	select {
	case <-second:
		t.Fatal("the fresh channel was already closed with an empty queue")
	default:
	}
}

// ---------------------------------------------------------------------------
// Entries
// ---------------------------------------------------------------------------

func TestAFailedCollectionIsQueuedWithItsError(t *testing.T) {
	// §26: collector errors are observable. An entry carrying an error and no
	// samples is a legitimate report, and dropping it would make a module
	// that has been failing for an hour indistinguishable from one nobody
	// enabled.
	queue := telemetry.New(0)
	failed := result("cpu", 0, time.Now())
	failed.Err = errors.New("cannot read /proc/stat")
	queue.Enqueue(failed)

	_, batch := queue.Drain()
	if len(batch) != 1 {
		t.Fatalf("drained %d entries, expected 1", len(batch))
	}
	if !strings.Contains(batch[0].Error, "/proc/stat") {
		t.Errorf("the error was lost: %q", batch[0].Error)
	}
	if len(batch[0].Samples) != 0 {
		t.Error("a failed collection carried samples")
	}
}

func TestTheDurationIsRecordedInMilliseconds(t *testing.T) {
	queue := telemetry.New(0)
	entry := result("cpu", 1, time.Now())
	entry.Duration = 250 * time.Millisecond
	queue.Enqueue(entry)

	_, batch := queue.Drain()
	if batch[0].DurationMs != 250 {
		t.Errorf("duration is %dms, expected 250", batch[0].DurationMs)
	}
}

func TestTheQueueIsASchedulerSink(t *testing.T) {
	// A method rather than an adapter, so there is no wrapper whose job is to
	// forget a field.
	var sink scheduler.Sink = telemetry.New(0)
	sink.Submit(result("cpu", 1, time.Now()))
	queue, ok := sink.(*telemetry.Queue)
	if !ok {
		t.Fatal("the queue is not usable as a sink")
	}
	if queue.Stats().Queued != 1 {
		t.Error("Submit did not enqueue")
	}
}

// ---------------------------------------------------------------------------
// Concurrency
// ---------------------------------------------------------------------------

func TestConcurrentEnqueuesAndDrainsDoNotLoseOrDuplicate(t *testing.T) {
	// The scheduler enqueues from its collecting goroutines and the
	// connection drains from its reader goroutine, so this is the real
	// access pattern. Run under `-race`, where an unguarded field shows.
	queue := telemetry.New(10_000)
	const producers = 8
	const perProducer = 200

	var wait sync.WaitGroup
	wait.Add(producers)
	for range producers {
		go func() {
			defer wait.Done()
			for index := range perProducer {
				queue.Enqueue(result("cpu", 1, time.Now().Add(time.Duration(index)*time.Millisecond)))
			}
		}()
	}

	drained := 0
	done := make(chan struct{})
	go func() {
		defer close(done)
		for drained < producers*perProducer {
			seq, batch := queue.Drain()
			if len(batch) == 0 {
				time.Sleep(time.Millisecond)
				continue
			}
			drained += len(batch)
			queue.Ack(seq)
		}
	}()

	wait.Wait()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatalf("drained %d of %d", drained, producers*perProducer)
	}

	if drained != producers*perProducer {
		t.Errorf("drained %d, expected %d", drained, producers*perProducer)
	}
	if dropped := queue.Stats().Dropped; dropped != 0 {
		t.Errorf("dropped %d entries from a queue with room for all of them", dropped)
	}
}
