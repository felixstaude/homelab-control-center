// Package agent contains the agent's runtime loop.
//
// Milestone 1.8 scope: connectivity. The agent loads its credential, dials the
// controller, heartbeats, and reconnects when the connection drops. It
// deliberately collects nothing — no metrics, no Docker access (engineering
// rule 1) — and reports no fabricated values (rule 8). What exists here is the
// connection supervision that Phase 2's collectors will plug into.
//
// # Reconnecting is the agent's whole job
//
// The controller never dials in (ADR-018), so an agent that has given up is an
// agent nothing will come and restart. The loop therefore treats a dropped
// connection as normal and reconnects forever, with three refinements that
// each exist because the naive version fails:
//
//   - **Exponential backoff.** A fixed one-second retry turns a controller
//     restart into a thousand hosts hammering it during the exact minute it is
//     trying to come up.
//   - **Jitter.** Pure exponential backoff makes a fleet that was disconnected
//     together reconnect together, forever — the thundering herd re-forms at
//     every doubling. Full jitter breaks the lockstep.
//   - **A reset on success.** Backoff that is not reset after a long, healthy
//     connection means a host whose link flaps once a day eventually waits a
//     minute to reconnect from a single blip.
//
// # One refusal is fatal, and the rest are not
//
// An agent whose credential has been revoked exits non-zero rather than
// retrying quietly. That is a deliberate choice about who the agent reports to:
// systemd's restart counter and `systemctl status` are what an operator looks
// at, and a process that is "active (running)" while doing nothing useful tells
// them the opposite of the truth. Everything else — a disabled host, a
// controller being restarted, a network outage — is temporary and is retried.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/scheduler"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/telemetry"
)

// defaultHeartbeatInterval is used until the controller states its own.
//
// The controller sends the authoritative interval in every acknowledgement, so
// this applies to exactly one heartbeat per connection — the first. It matches
// the controller's default so that even that one is right.
const defaultHeartbeatInterval = 30 * time.Second

// heartbeatFloor bounds what the agent will accept from the controller.
//
// The controller is trusted to set the cadence, but not to set it to zero: a
// bug or a compromised controller returning interval_seconds=0 would turn
// every agent in the fleet into a busy loop sending frames as fast as the
// socket allows — which the controller would then rate-limit, producing a
// reconnect storm driven by its own answer.
const heartbeatFloor = 5 * time.Second

// ErrRevoked reports that the controller refused this agent's credential.
//
// Returned from [Agent.Run] so the process can exit non-zero. Not retried; see
// the package documentation.
var ErrRevoked = errors.New("this agent's credential was rejected; the host must be re-enrolled")

// Agent owns the runtime loop.
type Agent struct {
	cfg    config.Config
	logger *slog.Logger
	clock  Clock

	// dial is injected so tests can drive the supervision loop without a
	// network. The loop's behaviour — backoff, jitter, reset, the fatal case —
	// is the part most worth testing and the part a real socket makes
	// untestable.
	dial Dialer

	startedAt time.Time

	// configMu guards config. The session loop is single-goroutine today, so
	// a bare field would be correct — but the scheduler reads this from its
	// own goroutines, and a lock added later is a lock added after the race
	// has shipped. `go test -race` covers it either way.
	configMu sync.RWMutex
	// config is the configuration currently in force, or nil when the agent
	// has none. Never replaced by a configuration that failed validation: a
	// rejected document leaves this untouched, which is what "keep running
	// the last known good one" means in practice.
	config *monitoring.Stored

	// queue buffers collected measurements until the controller stores them.
	// Nil only in tests that do not exercise collection.
	queue *telemetry.Queue
	// collectorCtx is the scheduler's context, needed by Apply. Set by Run and
	// nil before it, which is why every use is guarded: a configuration that
	// arrives before Run has started has nowhere to be applied, and the
	// stored copy is applied by Run itself.
	collectorCtx context.Context
	// collector runs the enabled modules on their schedules. It outlives
	// individual connections — collection continues through a controller
	// outage, which is the whole point of the queue (AGENT_ARCHITECTURE.md
	// §8) — so it is started by Run and stopped when Run returns, not per
	// session.
	collector *scheduler.Scheduler

	// --- Update management, protocol 2 ----------------------------------

	// applyUpdate applies an authorized instruction. Nil when update
	// management is not configured, which is the normal state of an agent
	// whose operator has not enabled it — and the reason the field is checked
	// rather than assumed.
	applyUpdate UpdateApplier

	// outbound carries frames from the update goroutine to the session loop.
	//
	// **The loop is the only writer to the socket**, and this channel is what
	// keeps that true once an update runs concurrently. An update takes
	// minutes — a download, an extraction, a service restart — and doing it
	// inline would stop the heartbeats, so the controller would time the
	// connection out mid-install. So it runs in its own goroutine and reports
	// through here.
	//
	// Buffered, so a reporter is not blocked by a loop that is momentarily
	// busy; small, because a backlog of stale progress is worth dropping
	// rather than queueing. A send that would block is dropped with a log
	// line: losing a progress report costs the interface a stale bar for one
	// heartbeat, and blocking the updater on the socket would mean a slow
	// connection could stall an install.
	//
	// Each item carries an optional acknowledgement channel, and that is not
	// decoration. The last report an update makes is `restarting`, and the
	// very next thing that happens is systemd killing this process — so a
	// report that had merely been *queued* was never sent, and the first real
	// run of the end-to-end scenario left the job showing `downloading` for a
	// host that had already finished updating. The reporter waits for the
	// write when it needs to know the frame left.
	outbound chan outboundFrame

	// declinedInstruction remembers the last instruction this agent refused
	// for want of being allowed to update itself.
	//
	// So that a host with updates disabled reports the refusal once rather
	// than once every heartbeat. Per session, deliberately: an agent that has
	// reconnected may be a newly configured one, and re-reporting is cheap
	// next to never reporting.
	declinedInstruction atomic.Pointer[string]

	// updating guards against two concurrent updates for one agent.
	//
	// The controller already enforces one open job per host in its schema,
	// and this is the agent's own half of the same invariant: a duplicate
	// instruction — a replayed frame, a controller that asked twice — must
	// not start a second download over the first.
	updating atomic.Bool

	// cancelUpdate stops the running update. Nil when none is running.
	cancelUpdate atomic.Pointer[context.CancelFunc]
}

// outboundFrame is one frame the update goroutine wants written, and an
// optional channel to hear about it on.
//
// `sent` is nil for a report whose delivery does not matter — an intermediate
// progress state, where a drop costs a stale bar for one heartbeat. It is
// non-nil for the ones where it does: see [Agent.outbound].
type outboundFrame struct {
	frame any
	sent  chan error
}

// UpdateApplier applies one authorized update instruction.
//
// An interface rather than a concrete updater so that internal/agent keeps no
// dependency on internal/updater — which depends on internal/install, which
// is the privileged half of the binary. The agent's session loop should not
// be able to reach the installer except through this.
type UpdateApplier interface {
	Apply(ctx context.Context, instruction protocol.UpdateInstruction, reporter UpdateReporter) error
}

// UpdateReporter is how an update tells the controller where it has got to.
type UpdateReporter interface {
	Progress(ctx context.Context, state, detail string) error
	Result(ctx context.Context, state, resultingVersion, failure string) error
}

// Configuration returns the configuration in force, or nil.
//
// A pointer to the stored value rather than a copy of the document: the value
// is immutable once applied — [monitoring.Apply] builds a new one rather than
// mutating — so sharing it is safe and copying a map of modules on every
// scheduler tick would not be.
func (a *Agent) Configuration() *monitoring.Stored {
	a.configMu.RLock()
	defer a.configMu.RUnlock()
	return a.config
}

func (a *Agent) setConfiguration(stored *monitoring.Stored) {
	a.configMu.Lock()
	a.config = stored
	a.configMu.Unlock()
}

// Connection is the subset of a controller connection the loop uses.
//
// An interface rather than the concrete type, for the reason above. Kept
// deliberately small: a wide interface would be a second description of the
// protocol, free to drift from the first.
type Connection interface {
	// Send writes one agent → controller frame. One method rather than one per
	// type; the protocol package's constructors are what type the call site.
	Send(ctx context.Context, frame any) error
	Read(ctx context.Context) (protocol.ServerFrame, error)
	Close()
}

// Dialer opens a connection to the controller.
type Dialer func(ctx context.Context, credential string) (Connection, error)

// Clock abstracts time so the loop is testable.
//
// The alternative — sleeping in tests — makes the suite slow and flaky. This
// is the one abstraction in the agent that exists purely for testability, and
// it is justified: a supervision loop that cannot be tested deterministically
// is a supervision loop nobody will change with confidence.
type Clock interface {
	// Now returns the current time.
	Now() time.Time
	// Sleep blocks for d, or until ctx is cancelled, and reports whether it
	// slept the whole duration. Returning the reason rather than just
	// unblocking is what lets the backoff distinguish "the wait elapsed" from
	// "we are shutting down".
	Sleep(ctx context.Context, d time.Duration) bool
	// After returns a channel that delivers once, after d.
	//
	// Separate from Sleep because the heartbeat timer has to be *selected
	// on* alongside the socket read. An agent that slept between heartbeats
	// could not react to anything the controller sent until it woke up — see
	// [Agent.session].
	After(d time.Duration) <-chan time.Time
}

// RealClock is the production Clock, backed by the time package.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) Sleep(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

func (RealClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

// Option customises an Agent.
type Option func(*Agent)

// WithClock replaces the agent's clock. Intended for tests.
func WithClock(clock Clock) Option {
	return func(a *Agent) { a.clock = clock }
}

// WithDialer replaces the agent's dialer. Intended for tests.
func WithDialer(dial Dialer) Option {
	return func(a *Agent) { a.dial = dial }
}

// New constructs an Agent from a validated configuration.
//
// Returns an error rather than panicking on a nil logger, so a programming
// mistake surfaces at startup with a clear message.
func New(cfg config.Config, logger *slog.Logger, opts ...Option) (*Agent, error) {
	if logger == nil {
		return nil, errors.New("agent: logger must not be nil")
	}

	a := &Agent{cfg: cfg, logger: logger, clock: RealClock{}}
	for _, opt := range opts {
		opt(a)
	}

	if a.queue == nil {
		a.queue = telemetry.New(cfg.QueueCapacity)
	}
	if a.outbound == nil {
		// Small on purpose. An update emits six progress reports over
		// minutes, so this is never close to full in normal operation — and
		// when it is, dropping a stale progress line is the right loss. See
		// the field's comment.
		a.outbound = make(chan outboundFrame, 8)
	}
	if a.collector == nil {
		// The /proc root is the real one. Per-core CPU is off unless the
		// configuration turns it on — it multiplies the cpu module's
		// cardinality by the core count, which is a cost the operator opts
		// into rather than discovers.
		a.collector = scheduler.New(
			logger.With("component", "scheduler"),
			a.queue,
			[]collect.Collector{
				collect.NewCPU(cfg.ProcRoot, cfg.CollectPerCore),
				collect.NewMemory(cfg.ProcRoot),
				collect.NewSwap(cfg.ProcRoot),
				collect.NewLoad(cfg.ProcRoot),
				collect.NewUptime(cfg.ProcRoot),
				collect.NewFilesystem(cfg.ProcRoot),
				collect.NewDiskIO(cfg.ProcRoot, cfg.SysRoot),
				collect.NewNetwork(cfg.ProcRoot, cfg.SysRoot, cfg.NetworkInterfaces),
				collect.NewSensors(cfg.SysRoot),
			},
		)
	}

	if a.dial == nil {
		info := buildinfo.Get()
		client, err := controller.New(controller.Config{
			BaseURL:      cfg.ControllerURL,
			CAFile:       cfg.CAFile,
			AgentVersion: info.Version,
			Platform:     info.Platform,
		})
		if err != nil {
			return nil, err
		}
		a.dial = func(ctx context.Context, credential string) (Connection, error) {
			return client.Connect(ctx, credential)
		}
	}

	a.startedAt = a.clock.Now()
	a.loadStoredConfiguration()
	return a, nil
}

// loadStoredConfiguration reads the last configuration the controller gave
// this agent, if there is one.
//
// At construction rather than on connect, so a restarted agent resumes
// collecting from its own disk without waiting for the controller — which is
// the whole point of storing it (AGENT_ARCHITECTURE.md §8: a controller
// outage must not create a monitoring gap).
//
// Never fatal. Three outcomes, and the middle one is the interesting case:
//
//   - **No file.** Normal for a freshly enrolled agent. Logged at debug; the
//     controller sends one on connect.
//   - **A corrupt or invalid file.** Logged at *error*, because an operator
//     needs to know — an agent that silently discarded it would come up
//     collecting nothing while reporting itself healthy. Not fatal, because
//     the controller will send a fresh document on the next connection and
//     exiting would turn a self-healing problem into a manual one.
//   - **A good file.** Applied, and the agent collects before it has spoken to
//     anybody.
func (a *Agent) loadStoredConfiguration() {
	stored, err := monitoring.Load(a.cfg.ConfigFile)
	switch {
	case errors.Is(err, monitoring.ErrNoStoredConfig):
		a.logger.Debug("no stored configuration; waiting for the controller",
			"path", a.cfg.ConfigFile,
		)
	case err != nil:
		a.logger.Error("ignoring the stored configuration",
			"path", a.cfg.ConfigFile,
			"error", err,
		)
	default:
		a.setConfiguration(&stored)
		a.logger.Info("resuming with the stored configuration",
			"version", stored.Document.Version,
			"enabled_modules", strings.Join(stored.Document.EnabledModules(), ","),
		)
	}
}

// WithQueue replaces the telemetry queue. Intended for tests.
func WithQueue(queue *telemetry.Queue) Option {
	return func(a *Agent) { a.queue = queue }
}

// WithScheduler replaces the collection scheduler. Intended for tests.
// WithUpdateApplier enables controller-driven updates.
//
// Absent, the agent takes no instruction: it logs that the controller has one
// and asks for nothing. That is the right default for a binary that installs
// itself as root — update management is wired in by `hcc-agent run` when the
// installation supports it, and an agent built or embedded without it cannot
// be made to install anything over the socket at all.
func WithUpdateApplier(applier UpdateApplier) Option {
	return func(a *Agent) { a.applyUpdate = applier }
}

func WithScheduler(collector *scheduler.Scheduler) Option {
	return func(a *Agent) { a.collector = collector }
}

// Queue exposes the telemetry queue, for `status` and for tests.
func (a *Agent) Queue() *telemetry.Queue { return a.queue }

// Run connects to the controller and stays connected until ctx is cancelled.
//
// Returns nil on a clean, context-cancelled shutdown. [ErrRevoked] when the
// controller has refused this agent's credential, which is not something
// retrying can fix. Any other error is a genuine failure worth a non-zero exit
// code.
//
// The credential is read from disk **on every connection attempt**, not once
// at startup. That is what makes re-enrollment work without a restart: an
// operator who runs `hcc-agent enroll` again on a running host has the new
// credential picked up by the next attempt. It also costs one small read per
// attempt, which is nothing next to a TLS handshake.
func (a *Agent) Run(ctx context.Context) error {
	info := buildinfo.Get()

	a.logger.Info("agent starting",
		"version", info.Version,
		"commit", info.Commit,
		"platform", info.Platform,
		"controller_url", a.cfg.ControllerURL,
		"credential_file", a.cfg.CredentialFile,
	)
	// Collection runs for the whole lifetime of the process, not per
	// connection. That is the point of the queue: a controller outage must not
	// create a monitoring gap (AGENT_ARCHITECTURE.md §8), so the scheduler
	// keeps collecting into the buffer and the connection drains it when there
	// is one.
	collectorCtx, stopCollecting := context.WithCancel(ctx)
	collectorDone := make(chan struct{})
	go func() {
		defer close(collectorDone)
		a.collector.Run(collectorCtx)
	}()
	defer func() {
		stopCollecting()
		<-collectorDone
	}()

	// Applied before the first connection, so a restarted agent resumes
	// collecting from its stored configuration immediately.
	if stored := a.Configuration(); stored != nil {
		a.collector.Apply(collectorCtx, stored.Document)
	}
	a.collectorCtx = collectorCtx

	wait := a.cfg.ReconnectMinWait
	var attempts uint64

	for {
		if err := ctx.Err(); err != nil {
			return a.shutdown()
		}

		attempts++
		productive, err := a.session(ctx)

		switch {
		case err == nil:
			// A clean close, which the controller initiates for several
			// legitimate reasons. Reconnect without complaint.
		case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
			return a.shutdown()
		case errors.Is(err, ErrRevoked):
			a.logger.Error("the controller rejected this agent's credential",
				"detail", "re-enroll this host from the control plane: "+
					"issue a new enrollment credential and run `hcc-agent enroll`",
			)
			return err
		case errors.Is(err, credentials.ErrNotEnrolled):
			a.logger.Error("this agent is not enrolled",
				"credential_file", a.cfg.CredentialFile,
				"detail", "run `hcc-agent enroll` with an enrollment credential "+
					"from the host's page in the control plane",
			)
			return err
		default:
			a.logger.Warn("connection ended", "attempt", attempts, "error", err)
		}

		// Reset after a connection that actually *worked* — one that exchanged
		// a heartbeat and an acknowledgement. Without the reset, a host whose
		// link flaps once a day creeps up to the maximum wait and stays there,
		// so a one-second blip costs a minute of invisibility.
		//
		// "Worked" deliberately does not mean "was accepted". Live
		// verification caught the difference: a disabled host's connection is
		// accepted, told why, and closed at once — so resetting on acceptance
		// made the agent reconnect roughly once a second, forever, against a
		// controller that had just said no. The backoff now grows for that
		// case, which is exactly what it is for.
		if productive {
			wait = a.cfg.ReconnectMinWait
		}

		delay := jitter(wait)
		a.logger.Info("reconnecting", "in", delay.Round(time.Millisecond).String())
		if !a.clock.Sleep(ctx, delay) {
			return a.shutdown()
		}

		wait = min(wait*2, a.cfg.ReconnectMaxWait)
	}
}

// readResult is one outcome of a socket read, delivered to the session loop.
type readResult struct {
	frame protocol.ServerFrame
	err   error
}

// session runs one connection from dial to close.
//
// Returns whether the connection *did any work*: whether it got at least one
// heartbeat acknowledged. That is what the caller needs in order to decide
// whether to reset its backoff, and it is deliberately stricter than "the
// handshake succeeded" — a connection that was accepted and immediately closed
// did not work, however briefly it existed.
//
// # The read runs continuously, and the heartbeat runs on a timer
//
// The first version of this loop sent a heartbeat, read the reply, then slept
// for the interval. It worked, and it quietly threw away the reason for
// having a socket at all: for thirty seconds out of every thirty, the agent
// was not listening. Live verification caught it — an administrator revoked a
// credential, the controller closed the connection immediately, and the agent
// carried on as though connected until its next heartbeat fell due.
//
// So the read happens in its own goroutine and the loop selects over three
// things: the heartbeat timer, the next frame, and cancellation. A
// server-initiated close is now acted on in milliseconds, which is what makes
// "revoke this credential" mean *now* rather than "within half a minute".
func (a *Agent) session(ctx context.Context) (productive bool, err error) {
	credential, err := credentials.Load(a.cfg.CredentialFile)
	if err != nil {
		return false, err
	}

	connection, err := a.dial(ctx, credential)
	if err != nil {
		if errors.Is(err, controller.ErrUnauthorized) {
			// Wrapped rather than returned as-is, so the loop above has one
			// sentinel to test for whether the refusal arrived at the
			// handshake or as a close frame.
			return false, fmt.Errorf("%w: %w", ErrRevoked, err)
		}
		return false, err
	}
	defer connection.Close()
	// Whatever was in flight when this connection ended goes back to the
	// front of the queue, so it is sent again on the next one. Without this an
	// unacknowledged batch would be lost on every disconnection — which is
	// the window at-least-once delivery exists to close.
	defer a.queue.Requeue()

	a.logger.Info("connected to the controller")

	// Cancelled on the way out, which is what stops the reader goroutine.
	// Without it the goroutine would block on a read of a closed socket for
	// as long as the library takes to notice, and one leaked goroutine per
	// reconnection is a leak that grows with the agent's uptime.
	readCtx, stopReading := context.WithCancel(ctx)
	defer stopReading()

	// Buffered by one, so the reader can deliver a final frame or error and
	// exit even if the loop below has already returned.
	frames := make(chan readResult, 1)
	go func() {
		for {
			frame, readErr := connection.Read(readCtx)
			select {
			case frames <- readResult{frame: frame, err: readErr}:
			case <-readCtx.Done():
				return
			}
			if readErr != nil {
				return
			}
		}
	}()

	// The first heartbeat goes out immediately rather than after one interval:
	// the controller stamps liveness at accept, and waiting thirty seconds to
	// confirm it would leave a host that is in fact fine looking unconfirmed.
	if err := a.sendHeartbeat(ctx, connection); err != nil {
		return false, err
	}

	interval := defaultHeartbeatInterval
	// Armed once and re-armed after each heartbeat, rather than created inside
	// the select. A timer built in the select expression is rebuilt on every
	// iteration, so every received frame would silently restart the heartbeat
	// clock and each discarded timer would be one more allocation a chatty
	// controller could provoke.
	timer := a.clock.After(interval)
	for {
		// Re-read on every iteration: the channel is replaced after each
		// wakeup, so holding the old one would mean never being woken again.
		pending := a.queue.Notify()

		select {
		case <-ctx.Done():
			return productive, ctx.Err()

		case <-pending:
			// Something to send. Drained and sent here, in the reader
			// goroutine, because a WebSocket must be written by exactly one
			// task — and because a sender goroutine would need its own
			// shutdown and its own error path for no benefit.
			if err := a.sendTelemetry(ctx, connection); err != nil {
				return productive, err
			}

		case <-timer:
			if err := a.sendHeartbeat(ctx, connection); err != nil {
				return productive, err
			}
			// Re-armed with the current interval, so a cadence the controller
			// changed takes effect from the next heartbeat rather than
			// retroactively shortening this one.
			timer = a.clock.After(interval)

		case item := <-a.outbound:
			// The update goroutine's reports, written by the loop so the
			// socket keeps exactly one writer.
			err := connection.Send(ctx, item.frame)
			if item.sent != nil {
				// Non-blocking: the channel has room for one and the waiter
				// may already have timed out, in which case nobody is
				// listening and blocking here would stall the session.
				select {
				case item.sent <- err:
				default:
				}
			}
			if err != nil {
				return productive, err
			}

		case result := <-frames:
			if result.err != nil {
				return productive, result.err
			}
			switch result.frame.Type {
			case protocol.TypeHeartbeatAck:
				// The one place productivity is established: the controller
				// has acknowledged a heartbeat, so this connection is doing
				// the thing connections are for.
				productive = true
				interval = a.acceptInterval(result.frame.Ack, interval)
				if err := a.reconcileConfig(ctx, connection, result.frame.Ack); err != nil {
					return productive, err
				}
				if err := a.reconcileUpdate(ctx, connection, result.frame.Ack); err != nil {
					return productive, err
				}
			case protocol.TypeConfigAvailable:
				// A notification, not evidence. The agent asks for the
				// document rather than acting on the version, because only a
				// snapshot whose checksum verifies says anything about what
				// to collect.
				a.logger.Info("the controller has a newer configuration",
					"version", result.frame.Available.Version,
				)
				if err := connection.Send(ctx, protocol.NewConfigRequest()); err != nil {
					return productive, err
				}

			case protocol.TypeConfigSnapshot:
				if err := a.handleConfigSnapshot(ctx, connection, result.frame.Snapshot); err != nil {
					return productive, err
				}

			case protocol.TypeTelemetryAck:
				a.handleTelemetryAck(result.frame.TelemetryAck)

			case protocol.TypeUpdateInstruction:
				a.startUpdate(ctx, result.frame.Instruction)

			case protocol.TypeUpdateCancel:
				a.cancelRunningUpdate(result.frame.Cancel)

			case protocol.TypeClose:
				return productive, a.handleClose(result.frame.Close)
			default:
				// Unreachable: ParseServerFrame refuses anything else.
				// Handled so that a future frame type added to the parser and
				// not to this switch fails loudly rather than being ignored.
				return productive, fmt.Errorf(
					"unhandled controller frame type %q", result.frame.Type,
				)
			}
		}
	}
}

// acceptInterval applies the controller's heartbeat cadence, or refuses it.
//
// The controller owns the cadence because it is the only side that can see the
// whole fleet, and it is the only channel through which an agent can be
// reconfigured at all. It does not own the right to set it to zero: a bug — or
// a compromised controller — returning interval_seconds=0 would turn every
// agent into a busy loop sending frames as fast as the socket allows, which
// the controller would then rate-limit, producing a reconnect storm driven by
// its own answer.
func (a *Agent) acceptInterval(ack *protocol.HeartbeatAck, current time.Duration) time.Duration {
	next := time.Duration(ack.IntervalSeconds) * time.Second
	if next < heartbeatFloor {
		// Logged rather than silently clamped: a controller asking for a
		// sub-floor interval is a bug somebody should see, and the agent's log
		// is where the operator will look.
		a.logger.Warn("ignoring an implausible heartbeat interval from the controller",
			"requested_seconds", ack.IntervalSeconds,
			"using", current.String(),
		)
		return current
	}
	if next != current {
		a.logger.Info("heartbeat interval set by the controller", "interval", next.String())
	}
	return next
}

// handleClose turns a controller close frame into the right error.
func (a *Agent) handleClose(closed *protocol.Close) error {
	if protocol.Fatal(closed.Reason) {
		return fmt.Errorf("%w: %s", ErrRevoked, closed.Detail)
	}

	logger := a.logger.With("reason", closed.Reason)
	if closed.Detail != "" {
		logger = logger.With("detail", closed.Detail)
	}

	switch closed.Reason {
	case protocol.CloseSuperseded:
		// Almost always this agent's own newer connection. Not a problem, and
		// not a reason to back off — which the caller handles by resetting the
		// backoff after a connection that was established.
		logger.Info("the controller superseded this connection")
	case protocol.CloseHostDisabled:
		logger.Warn("this host is disabled in the control plane; " +
			"the agent will keep retrying until it is enabled")
	case protocol.CloseUnsupportedProtocol:
		logger.Error("this agent's protocol version is not supported by the controller",
			"agent_protocol_version", protocol.Version,
			"controller_supports", closed.SupportedVersions,
			"detail", "install an agent build matching the controller",
		)
	default:
		logger.Warn("the controller closed the connection")
	}

	if !protocol.Retryable(closed.Reason) {
		// Returned as an error so the backoff applies. Not fatal: a controller
		// mid-upgrade may speak this version again shortly, and an agent that
		// exited would need a manual start afterwards.
		return fmt.Errorf("the controller closed the connection: %s", closed.Reason)
	}
	return nil
}

// sendHeartbeat writes one heartbeat, reporting the configuration in force.
//
// Reporting it on every heartbeat is what makes the synchronisation loop
// self-correcting: the controller answers with the version it has, so a missed
// push, a controller restart and a stale local file are all noticed within one
// interval without any of them needing a mechanism of its own.
func (a *Agent) sendHeartbeat(ctx context.Context, connection Connection) error {
	info := buildinfo.Get()
	var version int64
	var checksum string
	if current := a.Configuration(); current != nil {
		version = current.Document.Version
		checksum = current.Checksum
	}
	state := protocol.HeartbeatState{
		AgentVersion:   info.Version,
		Platform:       info.Platform,
		UptimeSeconds:  int64(a.uptime().Seconds()),
		ConfigVersion:  version,
		ConfigChecksum: checksum,
	}
	if a.queue != nil {
		stats := a.queue.Stats()
		state.BufferedItems = stats.Queued + stats.InFlight
		state.DroppedItems = stats.Dropped
	}
	return connection.Send(ctx, protocol.NewHeartbeat(state))
}

// reconcileConfig asks for a configuration when the controller has a
// different one.
//
// This is the whole of drift detection, and it is three lines because the
// acknowledgement carries the version. No push needs to have arrived, no
// separate channel exists, and a controller restart, a missed notification and
// a stale local file all produce the same comparison and the same request.
//
// Deliberately *not* "ask if the versions differ in either direction". An
// agent whose version is **higher** than the controller's has a configuration
// the controller does not know about, which should not be possible — versions
// are controller-assigned and monotonic — and the honest response is to ask
// for the current one and let the controller's answer win, rather than to
// assume the local file is newer and keep running it.
func (a *Agent) reconcileConfig(
	ctx context.Context, connection Connection, ack *protocol.HeartbeatAck,
) error {
	if ack.ConfigVersion == 0 {
		// The controller has none for this host. Nothing to ask for, and
		// nothing to do about a local configuration that should not exist:
		// discarding it would stop collection on a host whose controller is
		// merely between revisions, which is strictly worse than carrying on.
		return nil
	}
	var current int64
	if stored := a.Configuration(); stored != nil {
		current = stored.Document.Version
	}
	if current == ack.ConfigVersion {
		return nil
	}
	a.logger.Info("requesting the current configuration",
		"running", current,
		"controller_has", ack.ConfigVersion,
	)
	return connection.Send(ctx, protocol.NewConfigRequest())
}

// handleConfigSnapshot verifies, applies and acknowledges a configuration.
//
// Returns an error only when the *connection* cannot be used — a failed send.
// A configuration the agent will not run is not an error here: it is a
// rejection, reported to the controller and surfaced in the interface, with
// the previous configuration left in force.
//
// A failure to write the file is logged and **not** reported as a rejection.
// The two mean different things to the operator: "this configuration is
// wrong" sends them to the schedule, "this host's disk is read-only" sends
// them to the host. Nor is it acknowledged, so the controller keeps reporting
// the host as pending — which is true.
func (a *Agent) handleConfigSnapshot(
	ctx context.Context, connection Connection, snapshot *protocol.ConfigSnapshot,
) error {
	outcome, err := monitoring.Apply(
		a.cfg.ConfigFile, snapshot.Version, snapshot.Checksum, []byte(snapshot.Document),
	)
	if err != nil {
		a.logger.Error("cannot store the configuration the controller sent",
			"version", snapshot.Version,
			"error", err,
		)
		return nil
	}

	if !outcome.Accepted() {
		a.logger.Warn("refusing the configuration the controller sent",
			"version", snapshot.Version,
			"problems", strings.Join(outcome.Problems, "; "),
		)
		return connection.Send(ctx, protocol.NewConfigNack(
			outcome.Version, outcome.Checksum, outcome.Problems,
		))
	}

	a.setConfiguration(outcome.Applied)
	// Applied to the scheduler *before* acknowledging, so the controller's
	// "up to date" means collection has actually started. Acknowledging first
	// and reconfiguring after would leave a window in which the interface
	// claimed a module was live and nothing was collecting it — which is
	// exactly the gap between desired and applied state that §23 asks the
	// product not to paper over.
	if a.collectorCtx != nil {
		a.collector.Apply(a.collectorCtx, outcome.Applied.Document)
	}
	a.logger.Info("applied a new configuration",
		"version", outcome.Version,
		"enabled_modules", strings.Join(outcome.Applied.Document.EnabledModules(), ","),
	)
	return connection.Send(ctx, protocol.NewConfigAck(outcome.Version, outcome.Checksum))
}

// sendTelemetry drains one batch and sends it.
//
// Does nothing when the queue is empty, which is the common case between
// collections — the notification channel can fire for an enqueue that a
// previous iteration already drained.
//
// The batch is **not** removed from the queue here; it is marked in flight and
// removed only when the controller acknowledges it. That is what makes
// delivery survive a dropped connection: an unacknowledged batch is still
// there when the socket comes back.
func (a *Agent) sendTelemetry(ctx context.Context, connection Connection) error {
	seq, batch := a.queue.Drain()
	if len(batch) == 0 {
		return nil
	}

	entries := make([]protocol.TelemetryEntry, 0, len(batch))
	for _, entry := range batch {
		samples := make([]protocol.MetricSample, 0, len(entry.Samples))
		for _, sample := range entry.Samples {
			samples = append(samples, protocol.MetricSample{
				Metric: sample.Metric,
				Value:  sample.Value,
				Labels: sample.Labels,
			})
		}
		entries = append(entries, protocol.TelemetryEntry{
			Module:        entry.Module,
			SchemaVersion: entry.SchemaVersion,
			// RFC 3339 with nanoseconds, in UTC. The controller parses an
			// aware datetime and refuses a naive one, so the zone is not
			// optional — and UTC rather than local, because a host whose zone
			// changes must not appear to have moved its measurements.
			CollectedAt: entry.CollectedAt.UTC().Format(time.RFC3339Nano),
			DurationMs:  entry.DurationMs,
			Error:       entry.Error,
			Samples:     samples,
		})
	}

	return connection.Send(ctx, protocol.NewTelemetry(seq, entries))
}

// handleTelemetryAck clears the in-flight batch.
//
// A rejection count is logged rather than retried. The controller refuses a
// sample for reasons retrying cannot fix — a module no longer enabled, a
// metric this agent's registry declares and the controller's does not — so
// re-sending would be an infinite loop against a permanent answer. The count
// and the reason are what the operator needs instead.
func (a *Agent) handleTelemetryAck(ack *protocol.TelemetryAck) {
	if !a.queue.Ack(ack.Seq) {
		// An acknowledgement for a batch this agent is not waiting on: a
		// stale frame from a previous connection. Ignored rather than acted
		// on, because clearing the wrong batch would discard data the
		// controller has not stored.
		a.logger.Debug("ignoring a telemetry acknowledgement for an unknown batch",
			"seq", ack.Seq,
		)
		return
	}
	if ack.Rejected > 0 {
		a.logger.Warn("the controller refused some samples",
			"seq", ack.Seq,
			"accepted", ack.Accepted,
			"rejected", ack.Rejected,
			"detail", ack.Detail,
		)
		return
	}
	a.logger.Debug("telemetry stored", "seq", ack.Seq, "samples", ack.Accepted)
}

// jitter spreads a backoff delay across [d/2, d).
//
// "Full jitter" would be [0, d), and the lower bound here is deliberately not
// zero: a delay that can round down to nothing reintroduces the tight retry
// loop the backoff exists to prevent. Half-to-full keeps the spread wide
// enough to break a fleet's lockstep while guaranteeing the wait is a wait.
//
// math/rand/v2, not crypto/rand: this is scheduling, not a secret, and a
// predictable jitter value gives an attacker nothing. Using the cryptographic
// source here would be cargo-culting.
func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	half := d / 2
	return half + time.Duration(rand.Int64N(int64(half)+1)) //nolint:gosec // scheduling, not a secret
}

// shutdown releases resources.
//
// Bounded by the configured shutdown timeout so a wedged dependency cannot
// prevent the process from exiting.
func (a *Agent) shutdown() error {
	ctx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownTimeout)
	defer cancel()
	_ = ctx

	a.logger.Info("shutting down",
		"timeout", a.cfg.ShutdownTimeout.String(),
		"uptime", a.uptime().Round(time.Second).String(),
	)
	a.logger.Info("shutdown complete")
	return nil
}

// uptime reports how long the agent has been running.
func (a *Agent) uptime() time.Duration {
	return a.clock.Now().Sub(a.startedAt)
}
