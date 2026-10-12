package agent_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// fakeClock drives the loop deterministically, so these tests take
// milliseconds rather than sleeping through real backoff delays.
//
// It records every wait, because the *schedule* is the behaviour under test: a
// loop that reconnects correctly but hammers the controller while doing so is
// a bug the controller feels and the agent does not.
//
// The two kinds of wait are recorded separately. Sleep is used only by the
// reconnection backoff and After only by the heartbeat timer, so keeping them
// apart lets a backoff assertion stay readable rather than having to filter a
// single interleaved list.
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	sleeps []time.Duration
	ticks  []time.Duration
	// cancelAfter aborts the next wait once this many have been recorded,
	// across both kinds, which is how a test ends an otherwise infinite
	// supervision loop.
	cancelAfter int
	cancel      context.CancelFunc
	waits       int
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(ctx context.Context, d time.Duration) bool {
	c.mu.Lock()
	c.sleeps = append(c.sleeps, d)
	c.mu.Unlock()
	if c.advance(d) {
		return false
	}
	return ctx.Err() == nil
}

// After records the requested interval and fires shortly afterwards.
//
// "Shortly" rather than "immediately", and the delay is load-bearing. The
// session loop selects over the heartbeat timer and the socket read, and Go
// picks at random among ready cases — so a timer that fired immediately would
// starve the scripted frames about half the time and make every assertion
// about frame handling flaky. A few milliseconds is three orders of magnitude
// more than the fake connection needs to deliver a frame, so the frame always
// wins while the whole suite still runs in milliseconds.
func (c *fakeClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	c.ticks = append(c.ticks, d)
	c.mu.Unlock()

	fired := make(chan time.Time, 1)
	if c.advance(d) {
		// Cancellation has been requested, so the loop should leave through
		// ctx.Done. A channel that never fires leaves the select no choice.
		return fired
	}
	go func() {
		time.Sleep(5 * time.Millisecond)
		fired <- c.Now()
	}()
	return fired
}

// advance moves the clock and reports whether the test's cancellation fired.
func (c *fakeClock) advance(d time.Duration) bool {
	c.mu.Lock()
	c.now = c.now.Add(d)
	c.waits++
	count := c.waits
	cancel := c.cancel
	limit := c.cancelAfter
	c.mu.Unlock()

	if limit > 0 && count >= limit && cancel != nil {
		cancel()
		return true
	}
	return false
}

// recorded returns the reconnection backoff waits, in order.
func (c *fakeClock) recorded() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.sleeps...)
}

// ticked returns the heartbeat intervals the loop waited, in order.
func (c *fakeClock) ticked() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.ticks...)
}

// fakeConn is a scripted controller connection.
//
// When its frames run out it returns io.EOF, which ends the session — that is
// what most of these tests want. `block` makes it wait on the context
// instead, which is what a healthy connection does and what lets the
// heartbeat timer fire more than once.
type fakeConn struct {
	mu        sync.Mutex
	frames    []protocol.ServerFrame
	readErr   error
	sent      int
	closed    bool
	sendErr   error
	exhausted error
	block     bool

	// outgoing records every frame the agent sent, so a test can assert what
	// it asked for rather than only how many times it spoke. `sent` still
	// counts heartbeats specifically, because several tests are about the
	// heartbeat cadence and would be made harder to read by filtering.
	outgoing []any
}

func (c *fakeConn) Send(_ context.Context, frame any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.outgoing = append(c.outgoing, frame)
	if _, isHeartbeat := frame.(protocol.Heartbeat); isHeartbeat {
		c.sent++
	}
	return c.sendErr
}

// sentFrames returns a copy of everything the agent sent.
func (c *fakeConn) sentFrames() []any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]any, len(c.outgoing))
	copy(out, c.outgoing)
	return out
}

func (c *fakeConn) Read(ctx context.Context) (protocol.ServerFrame, error) {
	c.mu.Lock()
	if c.readErr != nil {
		err := c.readErr
		c.mu.Unlock()
		return protocol.ServerFrame{}, err
	}
	if len(c.frames) == 0 {
		exhausted, block := c.exhausted, c.block
		c.mu.Unlock()
		if block {
			<-ctx.Done()
			return protocol.ServerFrame{}, ctx.Err()
		}
		if exhausted != nil {
			return protocol.ServerFrame{}, exhausted
		}
		return protocol.ServerFrame{}, io.EOF
	}
	frame := c.frames[0]
	c.frames = c.frames[1:]
	c.mu.Unlock()
	return frame, nil
}

func (c *fakeConn) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.closed = true
}

func (c *fakeConn) heartbeats() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sent
}

func (c *fakeConn) wasClosed() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.closed
}

func ack(intervalSeconds int) protocol.ServerFrame {
	return protocol.ServerFrame{
		Type: protocol.TypeHeartbeatAck,
		Ack: &protocol.HeartbeatAck{
			V:               protocol.Version,
			Type:            protocol.TypeHeartbeatAck,
			IntervalSeconds: intervalSeconds,
			ServerTime:      "2026-01-01T12:00:00Z",
		},
	}
}

func closeFrame(reason, detail string) protocol.ServerFrame {
	return protocol.ServerFrame{
		Type:  protocol.TypeClose,
		Close: &protocol.Close{V: protocol.Version, Type: protocol.TypeClose, Reason: reason, Detail: detail},
	}
}

// enrolledConfig returns a configuration whose credential file exists.
func enrolledConfig(t *testing.T) config.Config {
	t.Helper()
	directory := t.TempDir()
	path := filepath.Join(directory, "credential")
	if err := credentials.Save(path, strings.Repeat("c", 43)); err != nil {
		t.Fatalf("save credential: %v", err)
	}

	cfg, err := config.Load(func(key string) (string, bool) {
		values := map[string]string{
			config.EnvControllerURL:  "https://hcc.internal:8000",
			config.EnvCredentialFile: path,
			// Pointed into the temporary directory, not left at its default.
			// Without this every test here would read — and the configuration
			// tests would *write* — /etc/hcc-agent/config.json on the
			// developer's own machine.
			config.EnvConfigFile:       filepath.Join(directory, "config.json"),
			config.EnvReconnectMinWait: "2s",
			config.EnvReconnectMaxWait: "60s",
		}
		value, ok := values[key]
		return value, ok
	})
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
	return cfg
}

func testLogger(w io.Writer, level slog.Level) *slog.Logger {
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: level}))
}

// runUntil drives the loop until the clock has recorded sleeps sleeps.
func runUntil(
	t *testing.T, cfg config.Config, dial agent.Dialer, sleeps int, out io.Writer,
) (*fakeClock, error) {
	t.Helper()
	clock := newFakeClock()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	clock.mu.Lock()
	clock.cancelAfter = sleeps
	clock.cancel = cancel
	clock.mu.Unlock()

	instance, err := agent.New(cfg, testLogger(out, slog.LevelDebug),
		agent.WithClock(clock), agent.WithDialer(dial))
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return clock, instance.Run(ctx)
}

func TestNewRejectsNilLogger(t *testing.T) {
	t.Parallel()

	if _, err := agent.New(enrolledConfig(t), nil); err == nil {
		t.Fatal("expected a nil logger to be rejected")
	}
}

func TestRunExitsCleanlyOnContextCancellation(t *testing.T) {
	t.Parallel()

	cfg := enrolledConfig(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	instance, err := agent.New(cfg, testLogger(io.Discard, slog.LevelInfo),
		agent.WithClock(newFakeClock()),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			t.Error("the agent should not dial after cancellation")
			return nil, errors.New("unreachable")
		}))
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	if err := instance.Run(ctx); err != nil {
		t.Fatalf("a cancelled context is a clean shutdown, got: %v", err)
	}
}

func TestAHeartbeatIsSentAndTheAcknowledgementSetsTheInterval(t *testing.T) {
	t.Parallel()

	// The controller owns the cadence, because it is the only side that can
	// see the whole fleet — and the agent cannot be reconfigured remotely any
	// other way (docs/AGENT_ARCHITECTURE.md §9).
	connection := &fakeConn{frames: []protocol.ServerFrame{ack(45)}, block: true}
	var log strings.Builder

	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) { return connection, nil },
		2, &log)

	if connection.heartbeats() < 2 {
		t.Fatalf("heartbeats = %d, want the timer to have fired at least once", connection.heartbeats())
	}
	// The *first* interval is the default: the timer is armed before any
	// acknowledgement has arrived, which is correct — the agent cannot know
	// the controller's cadence until the controller has told it. What matters
	// is that the next one honours it.
	ticks := clock.ticked()
	if len(ticks) < 2 {
		t.Fatalf("intervals = %v, want at least two", ticks)
	}
	if ticks[0] != 30*time.Second {
		t.Errorf("intervals = %v, want the first to be the 30s default", ticks)
	}
	if ticks[len(ticks)-1] != 45*time.Second {
		t.Errorf("intervals = %v, want the last to be the controller's 45s", ticks)
	}
	if !strings.Contains(log.String(), "heartbeat interval set by the controller") {
		t.Errorf("the change should be logged, got: %s", log.String())
	}
}

func TestAnImplausibleIntervalFromTheControllerIsIgnored(t *testing.T) {
	t.Parallel()

	// A controller bug — or a compromised one — returning zero would turn
	// every agent into a busy loop sending frames as fast as the socket
	// allows, which the controller would then rate-limit, producing a
	// reconnect storm driven by its own answer.
	connection := &fakeConn{frames: []protocol.ServerFrame{ack(0)}, block: true}
	var log strings.Builder

	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) { return connection, nil },
		2, &log)

	for _, waited := range clock.ticked() {
		if waited <= 0 {
			t.Fatalf("the agent waited %s; a zero interval must be refused", waited)
		}
	}
	if !strings.Contains(log.String(), "implausible heartbeat interval") {
		t.Errorf("the refusal should be logged, got: %s", log.String())
	}
}

func TestTheConnectionIsClosedWhenTheSessionEnds(t *testing.T) {
	t.Parallel()

	connection := &fakeConn{frames: []protocol.ServerFrame{ack(30)}}

	_, _ = runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) { return connection, nil },
		2, io.Discard)

	if !connection.wasClosed() {
		t.Error("the connection should be closed on the way out of a session")
	}
}

func TestBackoffGrowsAndIsJittered(t *testing.T) {
	t.Parallel()

	// A fixed retry turns a controller restart into a thousand hosts hammering
	// it during the exact minute it is coming up; pure exponential backoff
	// makes a fleet that dropped together reconnect together forever. Both the
	// growth and the spread are asserted.
	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return nil, errors.New("connection refused")
		},
		5, io.Discard)

	sleeps := clock.recorded()
	if len(sleeps) < 4 {
		t.Fatalf("expected several attempts, got %v", sleeps)
	}

	// Jitter is half-to-full, so each delay sits in [base/2, base].
	bases := []time.Duration{2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second}
	for index, base := range bases {
		slept := sleeps[index]
		if slept < base/2 || slept > base {
			t.Errorf("attempt %d slept %s, want within [%s, %s]", index+1, slept, base/2, base)
		}
	}
}

func TestBackoffNeverExceedsTheConfiguredMaximum(t *testing.T) {
	t.Parallel()

	// A host must not stay invisible for longer than the operator allowed,
	// however long the outage lasted.
	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return nil, errors.New("connection refused")
		},
		12, io.Discard)

	for _, slept := range clock.recorded() {
		if slept > 60*time.Second {
			t.Errorf("slept %s, above the 60s maximum", slept)
		}
	}
}

func TestBackoffNeverReachesZero(t *testing.T) {
	t.Parallel()

	// "Full jitter" would be [0, d), and a delay that rounds down to nothing
	// reintroduces the tight retry loop the backoff exists to prevent.
	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return nil, errors.New("connection refused")
		},
		8, io.Discard)

	for index, slept := range clock.recorded() {
		if slept <= 0 {
			t.Fatalf("attempt %d slept %s; a wait must be a wait", index+1, slept)
		}
	}
}

func TestBackoffResetsAfterAConnectionThatWorked(t *testing.T) {
	t.Parallel()

	// Without the reset, a host whose link flaps once a day creeps up to the
	// maximum and stays there — so a one-second blip costs a minute of
	// invisibility.
	var attempts int
	dial := func(context.Context, string) (agent.Connection, error) {
		attempts++
		switch attempts {
		case 1, 2, 3:
			return nil, errors.New("connection refused")
		default:
			// A session that *worked* — it exchanged a heartbeat and an
			// acknowledgement — and then ended. Mere acceptance is
			// deliberately not enough: see the reset in Agent.Run.
			return &fakeConn{frames: []protocol.ServerFrame{
				ack(30),
				closeFrame(protocol.CloseSuperseded, ""),
			}}, nil
		}
	}

	clock, _ := runUntil(t, enrolledConfig(t), dial, 5, io.Discard)

	sleeps := clock.recorded()
	if len(sleeps) < 4 {
		t.Fatalf("expected at least four waits, got %v", sleeps)
	}
	// The fourth attempt connected, so the wait after it is back at the base.
	if sleeps[3] > 2*time.Second {
		t.Errorf("the wait after a successful connection was %s, want it reset to ~2s", sleeps[3])
	}
}

func TestBeingAcceptedAndImmediatelyClosedDoesNotResetTheBackoff(t *testing.T) {
	t.Parallel()

	// The reconnect storm live verification caught. A disabled host's
	// connection is accepted, told why, and closed at once — so resetting the
	// backoff on *acceptance* made the agent reconnect about once a second,
	// forever, against a controller that had just said no.
	clock, _ := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{
				frames: []protocol.ServerFrame{closeFrame(protocol.CloseHostDisabled, "disabled")},
			}, nil
		},
		6, io.Discard)

	sleeps := clock.recorded()
	if len(sleeps) < 3 {
		t.Fatalf("expected several attempts, got %v", sleeps)
	}
	// Growing, not pinned at the base. Compared across two doublings rather
	// than consecutive waits, because jitter means one wait can legitimately
	// be shorter than the one before it.
	if sleeps[len(sleeps)-1] <= sleeps[0] {
		t.Errorf("waits = %v, want them to grow against a host that keeps refusing", sleeps)
	}
}

func TestAnUnauthorizedHandshakeStopsTheAgent(t *testing.T) {
	t.Parallel()

	// Deliberately fatal. An agent whose credential has been revoked and is
	// quietly retrying leaves systemd reporting "active (running)", which is
	// the opposite of the truth; exiting non-zero makes `systemctl status` say
	// something an operator can act on.
	var log strings.Builder
	clock, err := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return nil, controller.ErrUnauthorized
		},
		5, &log)

	if !errors.Is(err, agent.ErrRevoked) {
		t.Fatalf("expected ErrRevoked, got: %v", err)
	}
	if len(clock.recorded()) != 0 {
		t.Errorf("the agent should not have retried, slept %v", clock.recorded())
	}
	if !strings.Contains(log.String(), "re-enroll") {
		t.Errorf("the log should name the remedy, got: %s", log.String())
	}
}

func TestAnUnauthorizedCloseFrameStopsTheAgent(t *testing.T) {
	t.Parallel()

	// The same refusal can arrive after the socket is open — a credential
	// revoked while the agent was connected — and must be treated the same.
	connection := &fakeConn{
		frames: []protocol.ServerFrame{closeFrame(protocol.CloseUnauthorized, "credential revoked")},
	}

	_, err := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) { return connection, nil },
		5, io.Discard)

	if !errors.Is(err, agent.ErrRevoked) {
		t.Fatalf("expected ErrRevoked, got: %v", err)
	}
}

func TestADisabledHostIsRetriedPatientlyRatherThanFatal(t *testing.T) {
	t.Parallel()

	// The credential is fine and an operator has switched the host off. It
	// will come back, and an agent that exited would need somebody to go and
	// start it again afterwards.
	var log strings.Builder
	clock, err := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{
				frames: []protocol.ServerFrame{closeFrame(protocol.CloseHostDisabled, "disabled")},
			}, nil
		},
		3, &log)

	if errors.Is(err, agent.ErrRevoked) {
		t.Fatal("a disabled host must not be fatal")
	}
	if len(clock.recorded()) == 0 {
		t.Error("the agent should keep retrying a disabled host")
	}
	if !strings.Contains(log.String(), "disabled") {
		t.Errorf("the reason should be logged, got: %s", log.String())
	}
}

func TestAnUnsupportedProtocolIsLoggedWithTheRemedyAndRetriedSlowly(t *testing.T) {
	t.Parallel()

	// Not fatal, even though retrying cannot fix it in the short term: a
	// controller mid-upgrade may speak this version again in a minute, and an
	// agent that exited would need a manual start.
	var log strings.Builder
	_, err := runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{frames: []protocol.ServerFrame{
				closeFrame(protocol.CloseUnsupportedProtocol, "supported versions: 2"),
			}}, nil
		},
		3, &log)

	if errors.Is(err, agent.ErrRevoked) {
		t.Fatal("a protocol mismatch must not be fatal")
	}
	if !strings.Contains(log.String(), "install an agent build matching the controller") {
		t.Errorf("the log should name the remedy, got: %s", log.String())
	}
}

func TestASupersededConnectionIsNotTreatedAsAProblem(t *testing.T) {
	t.Parallel()

	// A supersession is almost always this agent's own reconnection winning,
	// so logging it as an error would train an operator to ignore the log.
	var log strings.Builder
	_, _ = runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{
				frames: []protocol.ServerFrame{closeFrame(protocol.CloseSuperseded, "")},
			}, nil
		},
		2, &log)

	output := log.String()
	if !strings.Contains(output, "superseded") {
		t.Errorf("the supersession should be logged, got: %s", output)
	}
	if strings.Contains(output, "level=ERROR") {
		t.Errorf("a supersession is not an error, got: %s", output)
	}
}

func TestAnUnenrolledAgentStopsWithAnInstruction(t *testing.T) {
	t.Parallel()

	// Not an error to retry: a freshly installed agent that has not been
	// enrolled is in a normal state, and the right response is to say what to
	// do rather than to loop.
	cfg := enrolledConfig(t)
	cfg.CredentialFile = filepath.Join(t.TempDir(), "absent")
	var log strings.Builder

	_, err := runUntil(t, cfg,
		func(context.Context, string) (agent.Connection, error) {
			t.Error("the agent should not dial without a credential")
			return nil, errors.New("unreachable")
		},
		3, &log)

	if !errors.Is(err, credentials.ErrNotEnrolled) {
		t.Fatalf("expected ErrNotEnrolled, got: %v", err)
	}
	if !strings.Contains(log.String(), "hcc-agent enroll") {
		t.Errorf("the log should name the command to run, got: %s", log.String())
	}
}

func TestTheCredentialIsReReadOnEveryAttempt(t *testing.T) {
	t.Parallel()

	// What makes re-enrollment work without a restart: an operator who runs
	// `hcc-agent enroll` again on a running host has the new credential picked
	// up by the next attempt.
	cfg := enrolledConfig(t)
	var seen []string
	var attempts int

	dial := func(_ context.Context, credential string) (agent.Connection, error) {
		seen = append(seen, credential)
		attempts++
		if attempts == 1 {
			// Replace the credential between attempts.
			if err := credentials.Save(cfg.CredentialFile, strings.Repeat("n", 43)); err != nil {
				t.Errorf("save: %v", err)
			}
		}
		return nil, errors.New("connection refused")
	}

	_, _ = runUntil(t, cfg, dial, 3, io.Discard)

	if len(seen) < 2 {
		t.Fatalf("expected at least two attempts, got %v", len(seen))
	}
	if seen[0] == seen[1] {
		t.Error("the credential should be re-read on each attempt, not cached at startup")
	}
	if seen[1] != strings.Repeat("n", 43) {
		t.Errorf("attempt 2 used %q, want the replaced credential", seen[1])
	}
}

func TestTheAgentNeverLogsTheCredential(t *testing.T) {
	t.Parallel()

	// At debug level, which is where a careless field would show up, and with
	// a credential distinctive enough to find in the output.
	cfg := enrolledConfig(t)
	secret := "SECRET-CREDENTIAL-VALUE-9f2c-do-not-log-this"
	if err := credentials.Save(cfg.CredentialFile, secret); err != nil {
		t.Fatalf("save: %v", err)
	}
	var log strings.Builder

	_, _ = runUntil(t, cfg,
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{frames: []protocol.ServerFrame{ack(30)}}, nil
		},
		3, &log)

	if strings.Contains(log.String(), secret) {
		t.Fatalf("the agent logged its credential:\n%s", log.String())
	}
}

func TestAnUnconfiguredAgentSaysSoRatherThanLookingLikeItIsMonitoring(t *testing.T) {
	t.Parallel()

	// The agent must not look like it is monitoring when it is not, and the
	// log is the one place an operator checks.
	//
	// This test used to assert the literal sentence "no data is collected in
	// this release", which was true until collection shipped. The *property*
	// is what mattered, so it is now asserted as the property: an agent with
	// no stored configuration says it has none, and starts no collection.
	var log strings.Builder

	_, _ = runUntil(t, enrolledConfig(t),
		func(context.Context, string) (agent.Connection, error) {
			return &fakeConn{frames: []protocol.ServerFrame{ack(30)}}, nil
		},
		2, &log)

	if !strings.Contains(log.String(), "no stored configuration") {
		t.Errorf("the startup log should say there is no configuration, got: %s", log.String())
	}
	// And it did not claim to be collecting anything.
	if strings.Contains(log.String(), "collecting a module") {
		t.Errorf("an unconfigured agent started a collector: %s", log.String())
	}
}

// A reminder that the credential helper is the only way the agent reads its
// secret; a future change that read the file directly would bypass every
// permission check.
var _ = os.Getuid
