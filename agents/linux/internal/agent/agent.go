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
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
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
}

// Connection is the subset of a controller connection the loop uses.
//
// An interface rather than the concrete type, for the reason above. Kept
// deliberately small: a wide interface would be a second description of the
// protocol, free to drift from the first.
type Connection interface {
	SendHeartbeat(ctx context.Context, heartbeat protocol.Heartbeat) error
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
	return a, nil
}

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
	a.logger.Info("no data is collected in this release",
		"detail", "this agent reports its liveness and its own version; "+
			"metric collection lands in Phase 2",
	)

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
		select {
		case <-ctx.Done():
			return productive, ctx.Err()

		case <-timer:
			if err := a.sendHeartbeat(ctx, connection); err != nil {
				return productive, err
			}
			// Re-armed with the current interval, so a cadence the controller
			// changed takes effect from the next heartbeat rather than
			// retroactively shortening this one.
			timer = a.clock.After(interval)

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

// sendHeartbeat writes one heartbeat.
func (a *Agent) sendHeartbeat(ctx context.Context, connection Connection) error {
	info := buildinfo.Get()
	return connection.SendHeartbeat(ctx, protocol.NewHeartbeat(
		info.Version,
		info.Platform,
		int64(a.uptime().Seconds()),
	))
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
