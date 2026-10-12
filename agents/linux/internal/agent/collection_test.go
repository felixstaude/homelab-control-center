package agent_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/scheduler"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/telemetry"
)

// These tests exercise the *wiring*: a real configuration, the real scheduler,
// the real collectors reading recorded /proc fixtures, the real queue, and the
// real frame encoding. The individual pieces are tested in their own packages;
// what only this level can show is whether they are connected — which is the
// difference between "the collectors work" and "the agent collects".

// procFixture writes a minimal but realistic /proc into a temporary directory.
//
// Written here rather than shared with the collect package's testdata: an
// import across test packages is not possible, and a copy of five short files
// is cheaper than the indirection.
func procFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{
		"stat": "cpu  1000 100 500 8000 200 10 20 0 0 0\n" +
			"cpu0 250 25 125 2000 50 2 5 0 0 0\n" +
			"intr 1 2 3\nctxt 4\n",
		"meminfo": "MemTotal:       16777216 kB\n" +
			"MemFree:          524288 kB\n" +
			"MemAvailable:    8388608 kB\n" +
			"Buffers:          262144 kB\n" +
			"Cached:          6291456 kB\n" +
			"SReclaimable:     524288 kB\n" +
			"SwapTotal:       2097152 kB\n" +
			"SwapFree:        1048576 kB\n",
		"loadavg": "2.64 3.07 2.98 3/1234 56789\n",
		"uptime":  "125203.33 2404876.21\n",
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(root, name), []byte(content), 0o600); err != nil {
			t.Fatalf("cannot write the %s fixture: %v", name, err)
		}
	}
	return root
}

// collectingConfig is an enrolled configuration pointed at a /proc fixture.
func collectingConfig(t *testing.T) config.Config {
	t.Helper()
	directory := t.TempDir()
	credentialPath := filepath.Join(directory, "credential")
	if err := credentials.Save(credentialPath, strings.Repeat("c", 43)); err != nil {
		t.Fatalf("save credential: %v", err)
	}

	cfg, err := config.Load(func(key string) (string, bool) {
		values := map[string]string{
			config.EnvControllerURL:    "https://hcc.internal:8000",
			config.EnvCredentialFile:   credentialPath,
			config.EnvConfigFile:       filepath.Join(directory, "config.json"),
			config.EnvProcRoot:         procFixture(t),
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

// enabledDocument builds a configuration document enabling modules at each
// module's own interval floor.
//
// Note which modules a test can afford to enable: the floors differ, and
// `uptime` is sixty seconds because uptime is a clock. A test waiting ten
// seconds for an `uptime` collection will always time out — which is how the
// first version of this file failed. `memory`, `cpu` and `swap` have a
// one-second floor and are the ones to use.
func enabledDocument(t *testing.T, version int64, enabled ...string) []byte {
	t.Helper()
	wanted := make(map[string]bool, len(enabled))
	for _, id := range enabled {
		wanted[id] = true
	}
	modules := map[string]any{}
	for _, id := range monitoring.ModuleIDs() {
		descriptor, _ := monitoring.Lookup(id)
		modules[id] = map[string]any{
			"enabled": wanted[id],
			"schedule": map[string]any{
				"kind":             "interval",
				"interval_seconds": descriptor.MinIntervalSeconds,
				"jitter_seconds":   0,
			},
		}
	}
	raw, err := json.Marshal(map[string]any{
		"document_version": monitoring.DocumentVersion,
		"registry_version": monitoring.RegistryVersion,
		"host_id":          "11111111-2222-3333-4444-555555555555",
		"version":          version,
		"generated_at":     "2026-10-13T00:00:00+00:00",
		"modules":          modules,
		"limits": map[string]any{
			"min_interval_seconds":        monitoring.MinIntervalSeconds,
			"max_interval_seconds":        monitoring.MaxIntervalSeconds,
			"module_min_interval_seconds": map[string]int{},
		},
	})
	if err != nil {
		t.Fatalf("cannot encode the document: %v", err)
	}
	return raw
}

func waitFor(t *testing.T, what string, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// ---------------------------------------------------------------------------
// Default-deny, at the last layer
// ---------------------------------------------------------------------------

func TestAnAgentWithNoConfigurationCollectsNothing(t *testing.T) {
	// The real collectors, the real scheduler, and nothing enabled. §2.1 at
	// the point where it finally matters: whether anything is read.
	cfg := collectingConfig(t)
	queue := telemetry.New(0)
	connection := &fakeConn{exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, err := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if stats := queue.Stats(); stats.Queued != 0 || stats.InFlight != 0 {
		t.Errorf("an unconfigured agent queued %+v", stats)
	}
}

func TestADisabledModuleIsNotCollectedEvenWithTheCollectorPresent(t *testing.T) {
	cfg := collectingConfig(t)
	// Every module present and off, which is what a fresh host receives.
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1)); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	connection := &fakeConn{exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if stats := queue.Stats(); stats.Queued != 0 {
		t.Errorf("a document with every module disabled still collected: %+v", stats)
	}
}

// ---------------------------------------------------------------------------
// Collecting for real
// ---------------------------------------------------------------------------

func TestAnEnabledModuleIsCollectedIntoTheQueue(t *testing.T) {
	// The whole wiring, with the real clock so the real ticker fires: a stored
	// configuration enabling memory at its one-second floor, the real
	// /proc/meminfo parser reading a fixture, and the samples arriving in the
	// queue.
	//
	// The real clock rather than the fake one, deliberately. The fake clock is
	// right for testing the *supervision* loop, where the schedule is the
	// behaviour; here the question is whether the pieces are connected, and
	// substituting the clock would leave the scheduler's own ticker out of
	// the path being tested.
	cfg := collectingConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	instance, err := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(ctx context.Context, _ string) (agent.Connection, error) {
			// Never returns a frame and blocks until shutdown, so the
			// connection neither drains the queue nor ends the session.
			return &fakeConn{block: true}, nil
		}),
	)
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()

	waitFor(t, "a collection to be queued", func() bool {
		stats := queue.Stats()
		return stats.Queued+stats.InFlight > 0
	})

	cancel()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent did not shut down")
	}

	_, batch := queue.Drain()
	if len(batch) == 0 {
		t.Fatal("nothing was queued")
	}
	if batch[0].Module != "memory" {
		t.Errorf("collected %q, expected memory", batch[0].Module)
	}

	// The real values from the fixture: 16 GiB total, 8 GiB available.
	var total, available float64
	for _, sample := range batch[0].Samples {
		switch sample.Metric {
		case "total":
			total = sample.Value
		case "available":
			available = sample.Value
		}
	}
	if total != 16*1024*1024*1024 {
		t.Errorf("total memory is %v, expected 16 GiB", total)
	}
	if available != 8*1024*1024*1024 {
		t.Errorf("available memory is %v, expected 8 GiB — MemFree would be 0.5 GiB", available)
	}
}

func TestCollectionContinuesWhileTheControllerIsUnreachable(t *testing.T) {
	// AGENT_ARCHITECTURE.md §8: a controller outage must not create a
	// monitoring gap. The dialer always fails, so there is never a
	// connection — and the queue fills anyway.
	cfg := collectingConfig(t)
	// `memory`, not `uptime`: the floors differ and uptime's is sixty seconds.
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory", "swap")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			// A plain error, not context.DeadlineExceeded. Run treats the
			// context errors as "we are shutting down" and returns, which
			// would stop the collector — so using one here tested the
			// opposite of what this test is about. An ordinary dial failure
			// goes to the backoff path and keeps the process alive.
			return nil, errors.New("connection refused")
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()

	waitFor(t, "collections to be queued despite no connection", func() bool {
		return queue.Stats().Queued > 0
	})

	cancel()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent did not shut down")
	}
}

func TestTheQueueDropsOldestWhenItCannotBeDrained(t *testing.T) {
	// The bound, under the condition it exists for: collection continues and
	// the controller is not storing anything.
	cfg := collectingConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory", "swap", "cpu")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	// A capacity of two, so it overflows almost immediately. The connection
	// answers heartbeats but **not** telemetry, so the queue has to build — a
	// connection that acknowledged every batch would drain it as fast as it
	// filled, which is the opposite of the state under test.
	queue := telemetry.New(2)
	connection := &ackingConn{heartbeatInterval: 5, refuseTelemetryAck: true}
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()

	waitFor(t, "the queue to overflow", func() bool { return queue.Stats().Dropped > 0 })

	cancel()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent did not shut down")
	}

	if queued := queue.Stats().Queued; queued > 2 {
		t.Errorf("the queue holds %d entries, over its capacity of 2", queued)
	}
}

func TestAnUnacknowledgedBatchIsNotResentInALoop(t *testing.T) {
	// The busy loop this found. With a batch in flight and no acknowledgement,
	// the agent must send it **once** for that connection rather than
	// re-encoding and re-sending it as fast as the socket allows — which would
	// flood the controller, trip its sample rate limiter, and starve the
	// heartbeat timer until the connection died of an idle timeout.
	cfg := collectingConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	connection := &ackingConn{refuseTelemetryAck: true}
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()

	waitFor(t, "a telemetry frame", func() bool { return connection.telemetryCount() > 0 })
	// Two seconds is thousands of iterations of a busy loop and about two
	// collections of a one-second module.
	time.Sleep(2 * time.Second)
	sent := connection.telemetryCount()

	cancel()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent did not shut down")
	}

	if sent > 3 {
		t.Errorf("the same batch was sent %d times; it should be sent once per connection", sent)
	}
}

func TestTheHeartbeatReportsTheQueueDepthAndDrops(t *testing.T) {
	// §26. Asserted on the *first* heartbeat, with the queue pre-filled: the
	// agent re-arms its heartbeat timer only on its own tick, so a cadence the
	// controller shortens takes effect from the second heartbeat — meaning a
	// test that waited for one would wait thirty seconds.
	cfg := collectingConfig(t)
	queue := telemetry.New(2)
	// Three entries into a queue of two: two queued, one dropped.
	for index := range 3 {
		queue.Enqueue(scheduler.Result{
			Module:        "memory",
			SchemaVersion: 1,
			At:            time.Now().Add(time.Duration(index) * time.Second),
			Samples:       []collect.Sample{{Metric: "total", Value: 1}},
		})
	}

	connection := &fakeConn{exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	var reported bool
	for _, frame := range connection.sentFrames() {
		beat, ok := frame.(protocol.Heartbeat)
		if !ok {
			continue
		}
		reported = true
		if beat.BufferedItems != 2 {
			t.Errorf("buffered_items is %d, expected 2", beat.BufferedItems)
		}
		if beat.DroppedItems != 1 {
			t.Errorf("dropped_items is %d, expected 1", beat.DroppedItems)
		}
	}
	if !reported {
		t.Fatal("no heartbeat was sent")
	}
}

// ---------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------

func TestQueuedTelemetryIsSentAndAcknowledged(t *testing.T) {
	cfg := collectingConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	// Acknowledges whatever it is sent, by watching the outgoing frames.
	connection := &ackingConn{}
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()

	waitFor(t, "a telemetry frame to be acknowledged", func() bool {
		return connection.acknowledged() > 0
	})

	cancel()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent did not shut down")
	}

	// Acknowledged batches have left the queue, which is what makes the
	// acknowledgement the thing that frees space rather than the send.
	if stats := queue.Stats(); stats.InFlight != 0 {
		t.Errorf("an acknowledged batch is still in flight: %+v", stats)
	}
}

func TestATelemetryFrameCarriesNoHostIdentifier(t *testing.T) {
	// §15, asserted against the real encoded frame rather than the struct: the
	// host is decided by the credential, so there must be nothing on the wire
	// for an agent to claim to be.
	encoded, err := json.Marshal(protocol.NewTelemetry(1, []protocol.TelemetryEntry{
		{
			Module:        "cpu",
			SchemaVersion: 1,
			CollectedAt:   time.Now().UTC().Format(time.RFC3339Nano),
			Samples:       []protocol.MetricSample{{Metric: "utilisation", Value: 11.5}},
		},
	}))
	if err != nil {
		t.Fatalf("cannot encode: %v", err)
	}
	if strings.Contains(strings.ToLower(string(encoded)), "host") {
		t.Fatalf("a telemetry frame names a host: %s", encoded)
	}
}

func TestATelemetryTimestampIsUtcWithAZone(t *testing.T) {
	// The controller refuses a naive timestamp, so the zone is not optional —
	// and UTC rather than local, because a host whose zone changes must not
	// appear to have moved its measurements.
	cfg := collectingConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, enabledDocument(t, 1, "memory")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	queue := telemetry.New(0)
	connection := &ackingConn{}
	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithQueue(queue),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = instance.Run(ctx)
	}()
	waitFor(t, "a telemetry frame", func() bool { return connection.telemetryCount() > 0 })
	cancel()
	<-finished

	for _, frame := range connection.sentFrames() {
		sent, ok := frame.(protocol.Telemetry)
		if !ok {
			continue
		}
		for _, entry := range sent.Batch {
			parsed, err := time.Parse(time.RFC3339Nano, entry.CollectedAt)
			if err != nil {
				t.Fatalf("collected_at %q is not RFC 3339: %v", entry.CollectedAt, err)
			}
			if parsed.Location() != time.UTC {
				t.Errorf("collected_at is in %s, expected UTC", parsed.Location())
			}
		}
		return
	}
	t.Fatal("no telemetry frame was sent")
}

// ackingConn acknowledges every telemetry frame it is sent.
//
// Separate from fakeConn because it has to *react* to what it receives, which
// fakeConn deliberately does not: fakeConn replays a fixed script, which is
// right for testing the supervision loop and wrong for testing a request and
// its reply.
type ackingConn struct {
	// heartbeatInterval, when non-zero, is answered to every heartbeat. Lets a
	// test shorten the agent's cadence to the controller's five-second floor
	// rather than waiting thirty seconds for a second heartbeat.
	heartbeatInterval int
	// refuseTelemetryAck leaves telemetry unacknowledged, so the queue builds.
	refuseTelemetryAck bool

	mu       sync.Mutex
	outgoing []any
	acks     int
	frames   chan protocol.ServerFrame
	closed   bool
}

func (c *ackingConn) Send(_ context.Context, frame any) error {
	c.mu.Lock()
	c.outgoing = append(c.outgoing, frame)
	var reply *protocol.ServerFrame
	switch sent := frame.(type) {
	case protocol.Telemetry:
		c.acks++
		if c.refuseTelemetryAck {
			break
		}
		reply = &protocol.ServerFrame{
			Type: protocol.TypeTelemetryAck,
			TelemetryAck: &protocol.TelemetryAck{
				V:        protocol.Version,
				Type:     protocol.TypeTelemetryAck,
				Seq:      sent.Seq,
				Accepted: len(sent.Batch),
			},
		}
	case protocol.Heartbeat:
		if c.heartbeatInterval > 0 {
			reply = &protocol.ServerFrame{
				Type: protocol.TypeHeartbeatAck,
				Ack: &protocol.HeartbeatAck{
					V:               protocol.Version,
					Type:            protocol.TypeHeartbeatAck,
					IntervalSeconds: c.heartbeatInterval,
					ServerTime:      time.Now().UTC().Format(time.RFC3339),
				},
			}
		}
	}
	channel := c.ensureFramesLocked()
	c.mu.Unlock()

	if reply != nil {
		// Buffered and non-blocking: a test that stopped reading must not
		// deadlock the agent's send path.
		select {
		case channel <- *reply:
		default:
		}
	}
	return nil
}

func (c *ackingConn) ensureFramesLocked() chan protocol.ServerFrame {
	if c.frames == nil {
		c.frames = make(chan protocol.ServerFrame, 64)
	}
	return c.frames
}

func (c *ackingConn) Read(ctx context.Context) (protocol.ServerFrame, error) {
	c.mu.Lock()
	channel := c.ensureFramesLocked()
	c.mu.Unlock()
	select {
	case frame := <-channel:
		return frame, nil
	case <-ctx.Done():
		return protocol.ServerFrame{}, ctx.Err()
	}
}

func (c *ackingConn) Close() {
	c.mu.Lock()
	c.closed = true
	c.mu.Unlock()
}

func (c *ackingConn) sentFrames() []any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]any, len(c.outgoing))
	copy(out, c.outgoing)
	return out
}

func (c *ackingConn) acknowledged() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.acks
}

func (c *ackingConn) telemetryCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	count := 0
	for _, frame := range c.outgoing {
		if _, ok := frame.(protocol.Telemetry); ok {
			count++
		}
	}
	return count
}
