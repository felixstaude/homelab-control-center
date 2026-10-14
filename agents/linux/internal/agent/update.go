package agent

import (
	"context"
	"errors"
	"log/slog"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// declined is an error that represents an instruction this agent refused
// rather than one that failed.
//
// Defined here, in the consumer, rather than imported from internal/updater —
// which would point the dependency the wrong way: the session loop must not
// be able to reach the installer except through [UpdateApplier].
// internal/updater's refusal type satisfies this structurally, and the
// coupling is a one-method interface rather than a package import.
type declined interface {
	Declined() bool
}

// isRefusal reports whether err is a declined instruction.
//
// The distinction matters for the log level and nothing else, which is why it
// is this small: an agent refusing an instruction its policy forbids is
// working correctly, and logging it as an error would make a
// correctly-configured fleet look broken in whatever somebody is grepping.
func isRefusal(err error) bool {
	var refusal declined
	return errors.As(err, &refusal) && refusal.Declined()
}

// reconcileUpdate asks for an instruction the controller has mentioned.
//
// The notification arrives on the heartbeat acknowledgement, and the agent
// answers with a request rather than acting on it — a notification is not
// evidence of anything, and only an instruction whose signature verifies says
// anything about what to install. Exactly the shape configuration
// synchronisation already uses, and for the same reason.
//
// Nothing is asked for when no applier is configured: an agent whose operator
// has not enabled update management should not be fetching instructions it
// would then decline, and the silence keeps a controller from recording
// refusals nobody asked for.
func (a *Agent) reconcileUpdate(
	ctx context.Context, connection Connection, ack *protocol.HeartbeatAck,
) error {
	if ack == nil || ack.UpdateInstructionID == "" {
		return nil
	}
	if a.applyUpdate == nil {
		a.logger.Debug(
			"the controller has an update instruction and this agent has no updater configured",
			"instruction", ack.UpdateInstructionID,
		)
		return nil
	}
	if a.updating.Load() {
		// Already working on one. Asking again would get the same instruction
		// and start a second download over the first.
		return nil
	}
	a.logger.Info("the controller has an update instruction",
		"instruction", ack.UpdateInstructionID,
	)
	return connection.Send(ctx, protocol.NewUpdateRequest())
}

// startUpdate applies an instruction in its own goroutine.
//
// # Why a goroutine
//
// An update takes minutes: a download, an extraction, a service restart.
// Doing it inline would stop the heartbeats for that whole time, so the
// controller would time the connection out mid-install — and an agent that
// loses its connection while replacing its own binary is the worst moment for
// it to happen. So the loop keeps heartbeating and the update reports through
// [Agent.outbound].
//
// # Why this process does not report success
//
// The install ends with `systemctl restart`, which terminates **this
// process**. So the states this agent can report stop at `restarting`; the
// controller completes the job when the replacement connects and reports the
// new version. That reconciliation is the controller's, and it has to be,
// because no process can report its own successful replacement.
func (a *Agent) startUpdate(ctx context.Context, instruction *protocol.UpdateInstruction) {
	if instruction == nil {
		return
	}
	logger := a.logger.With("instruction", instruction.InstructionID)
	if a.applyUpdate == nil {
		logger.Warn("an update instruction arrived and this agent has no updater configured")
		return
	}
	if !a.updating.CompareAndSwap(false, true) {
		// The agent's half of "one update per host". The controller enforces
		// it in its schema; this stops a replayed or duplicated frame from
		// starting a second download over the first.
		logger.Warn("an update is already running; ignoring the instruction")
		return
	}

	// A context of its own, so a cancel frame can stop it — and derived from
	// the session's, so a lost connection does not leave an orphan
	// downloading forever.
	updateCtx, cancel := context.WithCancel(ctx)
	a.cancelUpdate.Store(&cancel)

	copied := *instruction
	go func() {
		defer func() {
			cancel()
			a.cancelUpdate.Store(nil)
			a.updating.Store(false)
		}()
		reporter := &queuedReporter{agent: a, instructionID: copied.InstructionID, logger: logger}
		if err := a.applyUpdate.Apply(updateCtx, copied, reporter); err != nil {
			// Logged at Info for a refusal: an agent declining an instruction
			// its policy forbids is working correctly, and logging it as an
			// error would make a correctly-configured fleet look broken.
			if isRefusal(err) {
				logger.Info("the update instruction was declined", "reason", err.Error())
			} else {
				logger.Error("the update failed", "error", err)
			}
			return
		}
		// Reached only when the install did not restart this process, which
		// in practice means a dry run or an installer that declined to swap.
		logger.Info("the update finished without replacing this process")
	}()
}

// cancelRunningUpdate stops the update in progress, if the identifier matches.
//
// Matched on the identifier so a stale cancel — for an instruction that
// already finished and was followed by another — cannot stop the wrong
// update. Advisory once the binary is being replaced: by then the installer's
// own rollback is the recovery path, and a cancellation that left a
// half-installed service behind would be worse than none.
func (a *Agent) cancelRunningUpdate(frame *protocol.UpdateCancel) {
	if frame == nil {
		return
	}
	cancel := a.cancelUpdate.Load()
	if cancel == nil {
		a.logger.Info("the controller cancelled an update that is not running",
			"instruction", frame.InstructionID,
		)
		return
	}
	a.logger.Info("the controller cancelled the running update",
		"instruction", frame.InstructionID, "reason", frame.Reason,
	)
	(*cancel)()
}

// queuedReporter sends progress through the session loop.
//
// Never writes to the socket itself: the loop is the only writer, which is
// what makes concurrent reporting safe without a lock around the connection.
type queuedReporter struct {
	agent         *Agent
	instructionID string
	logger        *slog.Logger
}

func (r *queuedReporter) Progress(_ context.Context, state, detail string) error {
	return r.enqueue(protocol.NewUpdateProgress(r.instructionID, state, detail))
}

func (r *queuedReporter) Result(_ context.Context, state, resultingVersion, failure string) error {
	return r.enqueue(protocol.NewUpdateResult(r.instructionID, state, resultingVersion, failure))
}

// enqueue hands a frame to the loop, dropping it rather than blocking.
//
// A progress report lost costs the interface a stale bar for one heartbeat.
// Blocking the updater on a busy socket could stall an install, which is a
// much worse outcome — so the queue is small and a full one is a drop with a
// log line rather than back-pressure onto the installer.
func (r *queuedReporter) enqueue(frame any) error {
	select {
	case r.agent.outbound <- frame:
		return nil
	default:
		r.logger.Warn("dropped an update report; the outbound queue is full")
		return nil
	}
}
