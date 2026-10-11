// Package agent contains the agent's runtime loop.
//
// Phase 0 scope: the lifecycle only. The agent starts, validates its
// configuration, logs its identity, ticks on its reporting interval, and
// shuts down cleanly on a signal.
//
// It deliberately collects nothing. There is no metric collection and no
// Docker access (engineering rule 1), and no stub that pretends to report
// data (rule 8). What exists here is the supervision skeleton that Phase 2's
// collectors will plug into.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
)

// Agent owns the runtime loop.
type Agent struct {
	cfg    config.Config
	logger *slog.Logger

	// clock is injected so tests can drive the loop without sleeping.
	clock Clock

	// startedAt is used for uptime reporting.
	startedAt time.Time
}

// Clock abstracts time so the loop is testable.
//
// The alternative — sleeping in tests — makes the suite slow and flaky. This
// is the one abstraction in the agent that exists purely for testability, and
// it is justified: a supervision loop that cannot be tested deterministically
// is a supervision loop nobody will change with confidence.
type Clock interface {
	// Now returns the current time.
	Now() time.Time
	// Tick returns a channel delivering ticks at the given interval, plus a
	// stop function the caller must invoke to release resources.
	Tick(d time.Duration) (<-chan time.Time, func())
}

// RealClock is the production Clock, backed by the time package.
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) Tick(d time.Duration) (<-chan time.Time, func()) {
	ticker := time.NewTicker(d)
	return ticker.C, ticker.Stop
}

// Option customises an Agent.
type Option func(*Agent)

// WithClock replaces the agent's clock. Intended for tests.
func WithClock(clock Clock) Option {
	return func(a *Agent) { a.clock = clock }
}

// New constructs an Agent from a validated configuration.
//
// Returns an error rather than panicking on a nil logger, so a programming
// mistake surfaces at startup with a clear message.
func New(cfg config.Config, logger *slog.Logger, opts ...Option) (*Agent, error) {
	if logger == nil {
		return nil, errors.New("agent: logger must not be nil")
	}

	a := &Agent{
		cfg:    cfg,
		logger: logger,
		clock:  RealClock{},
	}
	for _, opt := range opts {
		opt(a)
	}
	a.startedAt = a.clock.Now()
	return a, nil
}

// Run executes the agent until ctx is cancelled.
//
// Returns nil on a clean, context-cancelled shutdown. Any other error is a
// genuine failure worth a non-zero exit code.
func (a *Agent) Run(ctx context.Context) error {
	info := buildinfo.Get()

	a.logger.Info("agent starting",
		"version", info.Version,
		"commit", info.Commit,
		"platform", info.Platform,
		"hostname", a.cfg.Hostname,
		"controller_url", a.cfg.ControllerURL,
		"report_interval", a.cfg.ReportInterval.String(),
	)

	// An operator who disabled certificate verification should be reminded
	// every time the agent starts, not just when they first configured it.
	if a.cfg.TLSSkipVerify {
		a.logger.Warn("TLS certificate verification is DISABLED; " +
			"traffic to the controller can be intercepted. " +
			"Install your CA certificate on this host and unset " +
			config.EnvTLSSkipVerify + " as soon as possible.")
	}

	// Phase 0 has no controller client, and says so plainly rather than
	// logging a fake successful registration.
	a.logger.Info("controller communication is not implemented in this release",
		"phase", "0",
		"detail", "the agent runs its lifecycle only; it does not register, "+
			"collect metrics or access Docker",
	)

	ticks, stop := a.clock.Tick(a.cfg.ReportInterval)
	defer stop()

	var cycles uint64

	for {
		select {
		case <-ctx.Done():
			// Context cancellation is the normal shutdown path.
			a.logger.Info("shutdown signal received",
				"completed_cycles", cycles,
				"uptime", a.uptime().Round(time.Second).String(),
			)
			if err := a.shutdown(); err != nil {
				return fmt.Errorf("shutdown: %w", err)
			}
			return nil

		case <-ticks:
			cycles++
			if err := a.reportCycle(ctx, cycles); err != nil {
				// Logged, never swallowed (engineering rule 6). A failed cycle
				// is not fatal: the agent must survive a controller outage and
				// retry on the next tick.
				a.logger.Error("report cycle failed",
					"cycle", cycles,
					"error", err,
				)
			}
		}
	}
}

// reportCycle is one iteration of the reporting loop.
//
// In Phase 2 this collects metrics and pushes them to the controller. For now
// it only emits a debug heartbeat, so that an operator running at debug level
// can confirm the loop is alive without the agent claiming to have sent data.
func (a *Agent) reportCycle(ctx context.Context, cycle uint64) error {
	// Respect cancellation promptly: a cycle starting as shutdown begins
	// should not delay exit.
	if err := ctx.Err(); err != nil {
		return err
	}

	a.logger.Debug("report cycle",
		"cycle", cycle,
		"uptime", a.uptime().Round(time.Second).String(),
		"note", "no data collected; collection lands in Phase 2",
	)
	return nil
}

// shutdown releases resources.
//
// Bounded by the configured shutdown timeout so a wedged dependency cannot
// prevent the process from exiting. Phase 0 holds no resources, so this
// returns immediately; the structure exists for Phase 2's collectors and
// in-flight HTTP requests.
func (a *Agent) shutdown() error {
	ctx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownTimeout)
	defer cancel()

	a.logger.Info("shutting down", "timeout", a.cfg.ShutdownTimeout.String())

	// Nothing to drain yet. When collectors exist, their Close calls go here
	// and must honour ctx.
	_ = ctx

	a.logger.Info("shutdown complete")
	return nil
}

// uptime reports how long the agent has been running.
func (a *Agent) uptime() time.Duration {
	return a.clock.Now().Sub(a.startedAt)
}
