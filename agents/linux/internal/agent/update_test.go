package agent

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// These tests are about the *wiring*, not the update logic — that lives in
// internal/updater and is tested there against every gate. What is only
// observable here is the part that involves the session loop:
//
//   - a notification produces a request, and only when an updater exists
//   - two instructions do not start two downloads
//   - reports reach the socket through the loop rather than from the update
//     goroutine, which is what keeps one writer
//   - a cancel frame stops the running update, and a stale one does not

type recordingApplier struct {
	mu        sync.Mutex
	started   int
	release   chan struct{}
	err       error
	reportVia func(UpdateReporter)
	sawCancel chan struct{}
}

func (r *recordingApplier) Apply(
	ctx context.Context, _ protocol.UpdateInstruction, reporter UpdateReporter,
) error {
	r.mu.Lock()
	r.started++
	r.mu.Unlock()

	if r.reportVia != nil {
		r.reportVia(reporter)
	}
	if r.release != nil {
		select {
		case <-r.release:
		case <-ctx.Done():
			if r.sawCancel != nil {
				close(r.sawCancel)
			}
			return ctx.Err()
		case <-time.After(5 * time.Second):
			return errors.New("the test did not release the applier")
		}
	}
	return r.err
}

func (r *recordingApplier) starts() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.started
}

type captureConnection struct {
	mu   sync.Mutex
	sent []any
}

func (c *captureConnection) Send(_ context.Context, frame any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sent = append(c.sent, frame)
	return nil
}

func (c *captureConnection) Read(context.Context) (protocol.ServerFrame, error) {
	return protocol.ServerFrame{}, errors.New("not used")
}

func (c *captureConnection) Close() {}

func (c *captureConnection) frames() []any {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]any(nil), c.sent...)
}

func testAgent(applier UpdateApplier) *Agent {
	return &Agent{
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
		applyUpdate: applier,
		outbound:    make(chan any, 8),
	}
}

func instruction() *protocol.UpdateInstruction {
	return &protocol.UpdateInstruction{
		V:             protocol.Version,
		Type:          protocol.TypeUpdateInstruction,
		InstructionID: "11111111-2222-3333-4444-555555555555",
		ToVersion:     "0.1.1",
	}
}

func TestANotificationProducesARequest(t *testing.T) {
	// The agent asks rather than acting on the identifier: a notification is
	// not evidence of anything, and only an instruction whose signature
	// verifies says anything about what to install.
	agent := testAgent(&recordingApplier{})
	connection := &captureConnection{}
	ack := &protocol.HeartbeatAck{UpdateInstructionID: "abc"}

	if err := agent.reconcileUpdate(context.Background(), connection, ack); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	frames := connection.frames()
	if len(frames) != 1 {
		t.Fatalf("sent %d frames, want 1", len(frames))
	}
	request, ok := frames[0].(protocol.UpdateRequest)
	if !ok {
		t.Fatalf("sent %T, want an UpdateRequest", frames[0])
	}
	if request.Type != protocol.TypeUpdateRequest || request.V != protocol.Version {
		t.Errorf("the request frame is %+v", request)
	}
}

func TestNoNotificationProducesNothing(t *testing.T) {
	agent := testAgent(&recordingApplier{})
	connection := &captureConnection{}

	for _, ack := range []*protocol.HeartbeatAck{nil, {}, {UpdateInstructionID: ""}} {
		if err := agent.reconcileUpdate(context.Background(), connection, ack); err != nil {
			t.Fatalf("reconcile: %v", err)
		}
	}

	if frames := connection.frames(); len(frames) != 0 {
		t.Errorf("sent %d frames for no notification", len(frames))
	}
}

func TestAnAgentWithNoUpdaterDeclinesWithoutFetching(t *testing.T) {
	// The default, and a behaviour that changed once the end-to-end scenario
	// ran. It used to be silence, which left an administrator who pressed
	// "install" watching a job sit in `queued` until the stale sweeper took
	// it. The controller only mentions an instruction when a job exists, and a
	// job only exists because somebody authorized one — so the refusal is
	// owed to them.
	//
	// Not fetched first: the reason does not depend on what is in the
	// instruction, because this host will decline whatever it is.
	agent := testAgent(nil)
	connection := &captureConnection{}

	if err := agent.reconcileUpdate(
		context.Background(), connection, &protocol.HeartbeatAck{UpdateInstructionID: "abc"},
	); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	frames := connection.frames()
	if len(frames) != 1 {
		t.Fatalf("sent %d frames, want one refusal: %v", len(frames), frames)
	}
	result, ok := frames[0].(protocol.UpdateResult)
	if !ok {
		t.Fatalf("sent %T, want an UpdateResult", frames[0])
	}
	if result.State != protocol.StateFailed {
		t.Errorf("state = %q, want %q", result.State, protocol.StateFailed)
	}
	if result.InstructionID != "abc" {
		t.Errorf("instruction = %q, want %q", result.InstructionID, "abc")
	}
	// The remedy, verbatim. It is rendered in the history an administrator
	// reads, and the variable name is the useful half.
	if !strings.Contains(result.Error, "HCC_AGENT_AUTO_UPDATE") {
		t.Errorf("the reason does not name the setting: %q", result.Error)
	}
	// No request for the instruction itself.
	for _, frame := range frames {
		if _, asked := frame.(protocol.UpdateRequest); asked {
			t.Error("the agent fetched an instruction it was always going to decline")
		}
	}
}

func TestARefusalIsReportedOncePerInstruction(t *testing.T) {
	// The job moves to `failed` on the first report and the controller stops
	// mentioning it, so this is belt and braces — but a controller that kept
	// mentioning it would otherwise get a refusal every thirty seconds for
	// ever.
	agent := testAgent(nil)
	connection := &captureConnection{}
	ack := &protocol.HeartbeatAck{UpdateInstructionID: "abc"}

	for range 3 {
		if err := agent.reconcileUpdate(context.Background(), connection, ack); err != nil {
			t.Fatalf("reconcile: %v", err)
		}
	}
	if frames := connection.frames(); len(frames) != 1 {
		t.Fatalf("sent %d refusals for one instruction, want one", len(frames))
	}

	// A different instruction is a different decision, and is reported.
	if err := agent.reconcileUpdate(
		context.Background(), connection, &protocol.HeartbeatAck{UpdateInstructionID: "def"},
	); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if frames := connection.frames(); len(frames) != 2 {
		t.Fatalf("sent %d refusals for two instructions, want two", len(frames))
	}
}

func TestAnAgentWithNoUpdaterIgnoresAnInstruction(t *testing.T) {
	// And if one arrives anyway — a controller that asked without being
	// asked — nothing runs.
	agent := testAgent(nil)

	agent.startUpdate(context.Background(), instruction())

	if agent.updating.Load() {
		t.Error("an agent with no updater started an update")
	}
}

func TestANotificationIsIgnoredWhileAnUpdateRuns(t *testing.T) {
	// Asking again would get the same instruction and start a second
	// download over the first.
	applier := &recordingApplier{release: make(chan struct{})}
	agent := testAgent(applier)
	connection := &captureConnection{}

	agent.startUpdate(context.Background(), instruction())
	waitFor(t, func() bool { return applier.starts() == 1 })

	if err := agent.reconcileUpdate(
		context.Background(), connection, &protocol.HeartbeatAck{UpdateInstructionID: "abc"},
	); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if frames := connection.frames(); len(frames) != 0 {
		t.Errorf("asked for an instruction while one was running: %v", frames)
	}

	close(applier.release)
}

func TestTwoInstructionsStartOneUpdate(t *testing.T) {
	// The agent's half of "one update per host". The controller enforces it
	// in its schema; this stops a replayed frame from starting a second
	// download over the first.
	applier := &recordingApplier{release: make(chan struct{})}
	agent := testAgent(applier)

	agent.startUpdate(context.Background(), instruction())
	waitFor(t, func() bool { return applier.starts() == 1 })
	agent.startUpdate(context.Background(), instruction())

	if starts := applier.starts(); starts != 1 {
		t.Errorf("the applier ran %d times, want 1", starts)
	}

	close(applier.release)
	waitFor(t, func() bool { return !agent.updating.Load() })
}

func TestTheFlagIsClearedAfterwards(t *testing.T) {
	// A flag left set would mean the agent never takes another instruction,
	// which is a fleet that silently stops updating.
	for _, outcome := range []error{nil, errors.New("it failed")} {
		applier := &recordingApplier{err: outcome}
		agent := testAgent(applier)

		agent.startUpdate(context.Background(), instruction())
		waitFor(t, func() bool { return !agent.updating.Load() })

		if agent.cancelUpdate.Load() != nil {
			t.Error("the cancel function was left behind")
		}
	}
}

func TestReportsReachTheLoopRatherThanTheSocket(t *testing.T) {
	// The property that keeps the socket single-writer: the update goroutine
	// hands frames to the loop, which writes them. An updater that wrote
	// directly would race the heartbeat.
	applier := &recordingApplier{
		reportVia: func(reporter UpdateReporter) {
			_ = reporter.Progress(context.Background(), protocol.StateDownloading, "3 MB")
			_ = reporter.Result(context.Background(), protocol.StateSucceeded, "0.1.1", "")
		},
	}
	agent := testAgent(applier)

	agent.startUpdate(context.Background(), instruction())
	waitFor(t, func() bool { return len(agent.outbound) == 2 })

	first := <-agent.outbound
	progress, ok := first.(protocol.UpdateProgress)
	if !ok {
		t.Fatalf("first frame is %T", first)
	}
	if progress.State != protocol.StateDownloading || progress.Detail != "3 MB" {
		t.Errorf("progress frame is %+v", progress)
	}
	if progress.InstructionID != instruction().InstructionID {
		t.Errorf("the progress frame names instruction %q", progress.InstructionID)
	}

	second := <-agent.outbound
	result, ok := second.(protocol.UpdateResult)
	if !ok {
		t.Fatalf("second frame is %T", second)
	}
	if result.State != protocol.StateSucceeded || result.ResultingVersion != "0.1.1" {
		t.Errorf("result frame is %+v", result)
	}
}

func TestAFullQueueDropsReportsRatherThanBlocking(t *testing.T) {
	// A progress report lost costs the interface a stale bar for one
	// heartbeat. Blocking the updater on a busy socket could stall an
	// install, which is much worse — so a full queue drops.
	applier := &recordingApplier{
		reportVia: func(reporter UpdateReporter) {
			for index := 0; index < 50; index++ {
				if err := reporter.Progress(context.Background(), protocol.StateDownloading, "x"); err != nil {
					panic("a dropped report must not be an error: " + err.Error())
				}
			}
		},
	}
	agent := testAgent(applier)

	agent.startUpdate(context.Background(), instruction())

	// Completes rather than deadlocking, which is the assertion.
	waitFor(t, func() bool { return !agent.updating.Load() })
	if len(agent.outbound) != cap(agent.outbound) {
		t.Errorf("the queue holds %d of %d", len(agent.outbound), cap(agent.outbound))
	}
}

func TestACancelStopsTheRunningUpdate(t *testing.T) {
	applier := &recordingApplier{
		release:   make(chan struct{}),
		sawCancel: make(chan struct{}),
	}
	agent := testAgent(applier)

	agent.startUpdate(context.Background(), instruction())
	waitFor(t, func() bool { return applier.starts() == 1 })

	agent.cancelRunningUpdate(&protocol.UpdateCancel{
		InstructionID: instruction().InstructionID,
		Reason:        "an administrator changed their mind",
	})

	select {
	case <-applier.sawCancel:
	case <-time.After(2 * time.Second):
		t.Fatal("the update was not cancelled")
	}
	waitFor(t, func() bool { return !agent.updating.Load() })
}

func TestACancelWithNothingRunningIsHarmless(t *testing.T) {
	// A stale cancel — for an instruction that already finished — must not
	// panic and must not stop anything.
	agent := testAgent(&recordingApplier{})

	agent.cancelRunningUpdate(&protocol.UpdateCancel{InstructionID: "whatever"})
	agent.cancelRunningUpdate(nil)
}

func TestALostConnectionStopsTheUpdate(t *testing.T) {
	// The update's context derives from the session's, so a dropped
	// connection does not leave an orphan downloading forever.
	applier := &recordingApplier{
		release:   make(chan struct{}),
		sawCancel: make(chan struct{}),
	}
	agent := testAgent(applier)
	ctx, cancel := context.WithCancel(context.Background())

	agent.startUpdate(ctx, instruction())
	waitFor(t, func() bool { return applier.starts() == 1 })
	cancel()

	select {
	case <-applier.sawCancel:
	case <-time.After(2 * time.Second):
		t.Fatal("the update outlived its session")
	}
}

func TestANilInstructionIsIgnored(t *testing.T) {
	agent := testAgent(&recordingApplier{})

	agent.startUpdate(context.Background(), nil)

	if agent.updating.Load() {
		t.Error("a nil instruction started an update")
	}
}

func TestIsRefusalRecognisesTheUpdatersType(t *testing.T) {
	// The one-method interface this package defines, satisfied structurally
	// by internal/updater's refusal — so the session loop can tell a declined
	// instruction from a broken one without importing the installer.
	if isRefusal(errors.New("an ordinary failure")) {
		t.Error("an ordinary error was treated as a refusal")
	}
	if isRefusal(nil) {
		t.Error("nil was treated as a refusal")
	}
	if !isRefusal(&testRefusal{declining: true}) {
		t.Error("a declining error was not recognised")
	}
	// An error that implements the interface and reports false is not a
	// refusal. Worth asserting separately: `errors.As` succeeding is not the
	// same as the error declining, and treating the two as equivalent would
	// log real failures at Info.
	if isRefusal(&testRefusal{declining: false}) {
		t.Error("an error reporting Declined()=false was treated as a refusal")
	}
	// And wrapped, because the session loop sees whatever the updater
	// returned after its own context has been added.
	if !isRefusal(fmt.Errorf("context: %w", &testRefusal{declining: true})) {
		t.Error("a wrapped refusal was not recognised")
	}
}

type testRefusal struct{ declining bool }

func (t *testRefusal) Error() string  { return "declined" }
func (t *testRefusal) Declined() bool { return t.declining }

// waitFor polls until condition holds, failing the test on a timeout.
//
// Polling rather than sleeping a fixed interval: a fixed sleep is either flaky
// on a loaded machine or slow on an idle one, and this suite already pays for
// a real scheduler elsewhere.
func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("the condition did not hold within 3s")
}
