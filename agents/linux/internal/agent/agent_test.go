package agent_test

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
)

// fakeClock drives the agent's loop deterministically, so these tests take
// microseconds rather than sleeping for real intervals.
type fakeClock struct {
	mu      sync.Mutex
	now     time.Time
	ticks   chan time.Time
	stopped bool
}

func newFakeClock() *fakeClock {
	return &fakeClock{
		now:   time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC),
		ticks: make(chan time.Time, 16),
	}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Tick(time.Duration) (<-chan time.Time, func()) {
	return c.ticks, func() {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.stopped = true
	}
}

// advance emits a tick and moves the clock forward.
func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	c.mu.Unlock()
	c.ticks <- c.now
}

func (c *fakeClock) tickerStopped() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stopped
}

func testConfig() config.Config {
	cfg, err := config.Load(func(key string) (string, bool) {
		values := map[string]string{
			config.EnvControllerURL: "https://hcc.internal:8000",
			config.EnvToken:         "test-token",
			config.EnvHostname:      "test-host",
		}
		value, ok := values[key]
		return value, ok
	})
	if err != nil {
		panic(err)
	}
	if err := cfg.Validate(); err != nil {
		panic(err)
	}
	return cfg
}

func testLogger(w io.Writer, level slog.Level) *slog.Logger {
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: level}))
}

func TestNewRejectsNilLogger(t *testing.T) {
	t.Parallel()

	if _, err := agent.New(testConfig(), nil); err == nil {
		t.Error("expected an error for a nil logger rather than a panic later")
	}
}

func TestRunExitsCleanlyOnContextCancellation(t *testing.T) {
	t.Parallel()

	var logs syncBuffer
	instance, err := agent.New(testConfig(), testLogger(&logs, slog.LevelInfo),
		agent.WithClock(newFakeClock()))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() { done <- instance.Run(ctx) }()

	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Errorf("a cancelled context is a clean shutdown, got error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}

	output := logs.String()
	for _, want := range []string{"agent starting", "shutdown signal received", "shutdown complete"} {
		if !strings.Contains(output, want) {
			t.Errorf("expected %q in the log output, got:\n%s", want, output)
		}
	}
}

func TestRunProcessesTicksBeforeShutdown(t *testing.T) {
	t.Parallel()

	clock := newFakeClock()
	var logs syncBuffer
	// Debug level, because the per-cycle line is logged at debug.
	instance, err := agent.New(testConfig(), testLogger(&logs, slog.LevelDebug),
		agent.WithClock(clock))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- instance.Run(ctx) }()

	clock.advance(30 * time.Second)
	clock.advance(30 * time.Second)

	// Wait for both cycles to be observed before cancelling, so the assertion
	// is not racing the loop.
	deadline := time.After(2 * time.Second)
	for strings.Count(logs.String(), "report cycle") < 2 {
		select {
		case <-deadline:
			t.Fatalf("expected 2 report cycles, log was:\n%s", logs.String())
		default:
			time.Sleep(time.Millisecond)
		}
	}

	cancel()
	if err := <-done; err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	if !strings.Contains(logs.String(), "completed_cycles=2") {
		t.Errorf("expected completed_cycles=2 in shutdown log, got:\n%s", logs.String())
	}
}

func TestRunStopsTheTickerOnExit(t *testing.T) {
	t.Parallel()

	clock := newFakeClock()
	instance, err := agent.New(testConfig(), testLogger(io.Discard, slog.LevelInfo),
		agent.WithClock(clock))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- instance.Run(ctx) }()

	cancel()
	<-done

	// Leaking a ticker in a long-lived process is a real resource leak.
	if !clock.tickerStopped() {
		t.Error("Run must stop its ticker before returning")
	}
}

func TestRunStatesThatControllerCommunicationIsUnimplemented(t *testing.T) {
	t.Parallel()

	var logs syncBuffer
	instance, err := agent.New(testConfig(), testLogger(&logs, slog.LevelInfo),
		agent.WithClock(newFakeClock()))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(10 * time.Millisecond)
		cancel()
	}()
	if err := instance.Run(ctx); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Engineering rule 8: the agent must not imply it is doing work it is not.
	if !strings.Contains(logs.String(), "not implemented") {
		t.Errorf("startup must state that controller communication is absent, got:\n%s", logs.String())
	}
}

func TestRunWarnsWhenTLSVerificationIsDisabled(t *testing.T) {
	t.Parallel()

	cfg := testConfig()
	cfg.TLSSkipVerify = true

	var logs syncBuffer
	instance, err := agent.New(cfg, testLogger(&logs, slog.LevelInfo),
		agent.WithClock(newFakeClock()))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(10 * time.Millisecond)
		cancel()
	}()
	if err := instance.Run(ctx); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	output := logs.String()
	if !strings.Contains(output, "DISABLED") {
		t.Errorf("disabling TLS verification must produce a prominent warning, got:\n%s", output)
	}
	if !strings.Contains(output, "level=WARN") {
		t.Errorf("the TLS warning must be logged at WARN level, got:\n%s", output)
	}
}

func TestRunDoesNotLogTheToken(t *testing.T) {
	t.Parallel()

	const secret = "a-very-secret-token"

	cfg := testConfig()
	cfg.Token = secret

	var logs syncBuffer
	instance, err := agent.New(cfg, testLogger(&logs, slog.LevelDebug),
		agent.WithClock(newFakeClock()))
	if err != nil {
		t.Fatalf("constructing agent: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(10 * time.Millisecond)
		cancel()
	}()
	if err := instance.Run(ctx); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if strings.Contains(logs.String(), secret) {
		t.Errorf("the enrolment token must never appear in logs, got:\n%s", logs.String())
	}
}
