package agent_test

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// documentFor builds a configuration document with the given version and
// enabled modules.
func documentFor(t *testing.T, version int64, enabled ...string) []byte {
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
		"generated_at":     "2026-10-12T12:00:00+00:00",
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

func snapshotFrame(t *testing.T, version int64, enabled ...string) protocol.ServerFrame {
	t.Helper()
	raw := documentFor(t, version, enabled...)
	return protocol.ServerFrame{
		Type: protocol.TypeConfigSnapshot,
		Snapshot: &protocol.ConfigSnapshot{
			V:        protocol.Version,
			Type:     protocol.TypeConfigSnapshot,
			Version:  version,
			Checksum: monitoring.Checksum(raw),
			Document: string(raw),
		},
	}
}

func ackFrame(configVersion int64) protocol.ServerFrame {
	return protocol.ServerFrame{
		Type: protocol.TypeHeartbeatAck,
		Ack: &protocol.HeartbeatAck{
			V:               protocol.Version,
			Type:            protocol.TypeHeartbeatAck,
			IntervalSeconds: 30,
			ServerTime:      time.Now().UTC().Format(time.RFC3339),
			ConfigVersion:   configVersion,
		},
	}
}

// outgoingOf returns every frame of one type the agent sent.
func outgoingOf[T any](frames []any) []T {
	var matched []T
	for _, frame := range frames {
		if typed, ok := frame.(T); ok {
			matched = append(matched, typed)
		}
	}
	return matched
}

// ---------------------------------------------------------------------------
// Applying
// ---------------------------------------------------------------------------

func TestASnapshotIsAppliedAndAcknowledged(t *testing.T) {
	cfg := enrolledConfig(t)
	connection := &fakeConn{
		frames:    []protocol.ServerFrame{snapshotFrame(t, 1, "cpu")},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, err := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
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

	current := instance.Configuration()
	if current == nil {
		t.Fatal("the configuration was not applied")
	}
	if current.Document.Version != 1 {
		t.Errorf("applied version is %d, expected 1", current.Document.Version)
	}
	if got := current.Document.EnabledModules(); len(got) != 1 || got[0] != "cpu" {
		t.Errorf("enabled modules are %v, expected [cpu]", got)
	}

	acks := outgoingOf[protocol.ConfigAck](connection.sentFrames())
	if len(acks) != 1 {
		t.Fatalf("expected one acknowledgement, got %d", len(acks))
	}
	if acks[0].Version != 1 {
		t.Errorf("acknowledged version %d, expected 1", acks[0].Version)
	}
	if acks[0].Checksum != current.Checksum {
		t.Error("the acknowledgement's checksum does not match the applied configuration")
	}
}

func TestAnAppliedSnapshotIsWrittenToDisk(t *testing.T) {
	// So a restart resumes without waiting for the controller.
	cfg := enrolledConfig(t)
	connection := &fakeConn{
		frames:    []protocol.ServerFrame{snapshotFrame(t, 1, "memory")},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	stored, err := monitoring.Load(cfg.ConfigFile)
	if err != nil {
		t.Fatalf("the configuration was not stored: %v", err)
	}
	if got := stored.Document.EnabledModules(); len(got) != 1 || got[0] != "memory" {
		t.Errorf("stored modules are %v, expected [memory]", got)
	}
}

func TestACorruptedSnapshotIsRejectedAndNotApplied(t *testing.T) {
	cfg := enrolledConfig(t)
	frame := snapshotFrame(t, 1, "cpu")
	// The digest no longer matches the document, which is what corruption or
	// tampering in transit looks like.
	frame.Snapshot.Checksum = monitoring.ChecksumPrefix + strings.Repeat("a", 64)

	connection := &fakeConn{frames: []protocol.ServerFrame{frame}, exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if instance.Configuration() != nil {
		t.Error("a corrupted configuration was applied")
	}
	nacks := outgoingOf[protocol.ConfigNack](connection.sentFrames())
	if len(nacks) != 1 {
		t.Fatalf("expected one rejection, got %d", len(nacks))
	}
	if !strings.Contains(strings.Join(nacks[0].Problems, " "), "checksum") {
		t.Errorf("the rejection should name the checksum, got %v", nacks[0].Problems)
	}
	if _, err := os.Stat(cfg.ConfigFile); !os.IsNotExist(err) {
		t.Error("a rejected configuration was written to disk")
	}
}

func TestAnInvalidSnapshotIsRejectedWithEveryProblem(t *testing.T) {
	cfg := enrolledConfig(t)

	// load's floor is five seconds; one is below it.
	broken := map[string]any{
		"document_version": monitoring.DocumentVersion,
		"registry_version": monitoring.RegistryVersion,
		"host_id":          "11111111-2222-3333-4444-555555555555",
		"version":          1,
		"generated_at":     "2026-10-12T12:00:00+00:00",
		"modules": map[string]any{
			"load": map[string]any{
				"enabled":  true,
				"schedule": map[string]any{"kind": "interval", "interval_seconds": 1},
			},
			"cpu": map[string]any{
				"enabled":  true,
				"schedule": map[string]any{"kind": "interval", "interval_seconds": 0},
			},
		},
		"limits": map[string]any{
			"min_interval_seconds":        monitoring.MinIntervalSeconds,
			"max_interval_seconds":        monitoring.MaxIntervalSeconds,
			"module_min_interval_seconds": map[string]int{},
		},
	}
	raw, err := json.Marshal(broken)
	if err != nil {
		t.Fatalf("cannot encode: %v", err)
	}

	connection := &fakeConn{
		frames: []protocol.ServerFrame{{
			Type: protocol.TypeConfigSnapshot,
			Snapshot: &protocol.ConfigSnapshot{
				V:        protocol.Version,
				Type:     protocol.TypeConfigSnapshot,
				Version:  1,
				Checksum: monitoring.Checksum(raw),
				Document: string(raw),
			},
		}},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	nacks := outgoingOf[protocol.ConfigNack](connection.sentFrames())
	if len(nacks) != 1 {
		t.Fatalf("expected one rejection, got %d", len(nacks))
	}
	if len(nacks[0].Problems) < 2 {
		t.Errorf("expected every problem, got %v", nacks[0].Problems)
	}
	if instance.Configuration() != nil {
		t.Error("an invalid configuration was applied")
	}
}

func TestARejectedSnapshotLeavesThePreviousConfigurationRunning(t *testing.T) {
	// The rule that matters most: a monitoring agent that stops monitoring
	// because somebody typed a bad interval has failed at its job.
	cfg := enrolledConfig(t)

	good := snapshotFrame(t, 1, "cpu")
	broken := snapshotFrame(t, 2, "cpu")
	broken.Snapshot.Checksum = monitoring.ChecksumPrefix + strings.Repeat("b", 64)

	connection := &fakeConn{
		frames:    []protocol.ServerFrame{good, broken},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	current := instance.Configuration()
	if current == nil {
		t.Fatal("the first configuration was lost")
	}
	if current.Document.Version != 1 {
		t.Errorf("running version %d; the rejected one replaced the good one", current.Document.Version)
	}
}

// ---------------------------------------------------------------------------
// Reporting and reconciliation
// ---------------------------------------------------------------------------

func TestAHeartbeatReportsNoConfigurationWhenThereIsNone(t *testing.T) {
	// By omission, not by claiming version zero. The controller refuses a
	// version below one, so reporting zero would cost the connection.
	cfg := enrolledConfig(t)
	connection := &fakeConn{exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	beats := outgoingOf[protocol.Heartbeat](connection.sentFrames())
	if len(beats) == 0 {
		t.Fatal("no heartbeat was sent")
	}
	if beats[0].ConfigVersion != 0 || beats[0].ConfigChecksum != "" {
		t.Errorf("an unconfigured agent reported version %d", beats[0].ConfigVersion)
	}

	// And `omitempty` keeps the fields off the wire entirely, so the
	// controller sees absence rather than a zero it would have to interpret.
	encoded, err := json.Marshal(beats[0])
	if err != nil {
		t.Fatalf("cannot encode: %v", err)
	}
	if strings.Contains(string(encoded), "config_version") {
		t.Errorf("an unconfigured heartbeat carried a config_version: %s", encoded)
	}
}

func TestAHeartbeatReportsTheRunningConfiguration(t *testing.T) {
	cfg := enrolledConfig(t)
	// Pre-seed the stored configuration, so the agent starts with one.
	raw := documentFor(t, 7, "cpu", "memory")
	if err := monitoring.Save(cfg.ConfigFile, raw); err != nil {
		t.Fatalf("cannot seed the configuration: %v", err)
	}

	connection := &fakeConn{exhausted: context.Canceled}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	beats := outgoingOf[protocol.Heartbeat](connection.sentFrames())
	if len(beats) == 0 {
		t.Fatal("no heartbeat was sent")
	}
	if beats[0].ConfigVersion != 7 {
		t.Errorf("reported version %d, expected 7", beats[0].ConfigVersion)
	}
	if beats[0].ConfigChecksum != monitoring.Checksum(raw) {
		t.Error("the reported checksum does not match the stored configuration")
	}
}

func TestAStoredConfigurationIsResumedAtStartup(t *testing.T) {
	// A controller outage must not create a monitoring gap, so the agent
	// collects from its own disk before it has spoken to anybody.
	cfg := enrolledConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, documentFor(t, 3, "swap")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}

	instance, err := agent.New(cfg, testLogger(&strings.Builder{}, 0))
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	current := instance.Configuration()
	if current == nil {
		t.Fatal("the stored configuration was not resumed")
	}
	if current.Document.Version != 3 {
		t.Errorf("resumed version %d, expected 3", current.Document.Version)
	}
}

func TestACorruptStoredConfigurationIsLoggedAndNotFatal(t *testing.T) {
	// The controller sends a fresh document on the next connection, so
	// exiting would turn a self-healing problem into a manual one — but the
	// operator still has to be told, because an agent that silently discarded
	// it would come up collecting nothing while reporting itself healthy.
	cfg := enrolledConfig(t)
	if err := os.WriteFile(cfg.ConfigFile, []byte("{broken"), 0o600); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}

	var logs strings.Builder
	instance, err := agent.New(cfg, testLogger(&logs, 0))
	if err != nil {
		t.Fatalf("a corrupt configuration must not prevent startup: %v", err)
	}
	if instance.Configuration() != nil {
		t.Error("a corrupt configuration was used")
	}
	if !strings.Contains(logs.String(), "ignoring the stored configuration") {
		t.Errorf("the operator was not told; logs were:\n%s", logs.String())
	}
}

func TestAVersionMismatchInAnAcknowledgementAsksForTheConfiguration(t *testing.T) {
	// Drift detection, needing no push to have arrived. A controller restart,
	// a missed notification and a stale local file all produce this.
	cfg := enrolledConfig(t)
	connection := &fakeConn{
		frames:    []protocol.ServerFrame{ackFrame(5)},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if requests := outgoingOf[protocol.ConfigRequest](connection.sentFrames()); len(requests) != 1 {
		t.Fatalf("expected one configuration request, got %d", len(requests))
	}
}

func TestAMatchingVersionAsksForNothing(t *testing.T) {
	cfg := enrolledConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, documentFor(t, 5, "cpu")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}
	connection := &fakeConn{
		frames:    []protocol.ServerFrame{ackFrame(5)},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if requests := outgoingOf[protocol.ConfigRequest](connection.sentFrames()); len(requests) != 0 {
		t.Errorf("an up-to-date agent asked for a configuration %d times", len(requests))
	}
}

func TestAControllerWithNoConfigurationIsNotAskedForOne(t *testing.T) {
	// And a local configuration is kept rather than discarded: a controller
	// merely between revisions must not stop collection on a host.
	cfg := enrolledConfig(t)
	if err := monitoring.Save(cfg.ConfigFile, documentFor(t, 2, "cpu")); err != nil {
		t.Fatalf("cannot seed: %v", err)
	}
	connection := &fakeConn{
		frames:    []protocol.ServerFrame{ackFrame(0)},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if requests := outgoingOf[protocol.ConfigRequest](connection.sentFrames()); len(requests) != 0 {
		t.Errorf("asked for a configuration the controller does not have")
	}
	if current := instance.Configuration(); current == nil || current.Document.Version != 2 {
		t.Error("the local configuration was discarded")
	}
}

func TestANotificationIsAnsweredWithARequestRatherThanActedOn(t *testing.T) {
	// A notification is not evidence. Only a snapshot whose checksum verifies
	// says anything about what to collect.
	cfg := enrolledConfig(t)
	connection := &fakeConn{
		frames: []protocol.ServerFrame{{
			Type: protocol.TypeConfigAvailable,
			Available: &protocol.ConfigAvailable{
				V: protocol.Version, Type: protocol.TypeConfigAvailable, Version: 9,
			},
		}},
		exhausted: context.Canceled,
	}
	clock := &fakeClock{now: time.Unix(0, 0), cancelAfter: 2}

	instance, _ := agent.New(cfg, testLogger(&strings.Builder{}, 0),
		agent.WithClock(clock),
		agent.WithDialer(func(context.Context, string) (agent.Connection, error) {
			return connection, nil
		}),
	)
	ctx, cancel := context.WithCancel(context.Background())
	clock.cancel = cancel
	_ = instance.Run(ctx)

	if requests := outgoingOf[protocol.ConfigRequest](connection.sentFrames()); len(requests) != 1 {
		t.Fatalf("expected one request, got %d", len(requests))
	}
	if instance.Configuration() != nil {
		t.Error("the agent acted on a notification without receiving a document")
	}
}

// ---------------------------------------------------------------------------
// The config file and the credential must not collide
// ---------------------------------------------------------------------------

func TestTheConfigurationIsNeverWrittenOverTheCredential(t *testing.T) {
	// Caught by configuration validation rather than discovered at runtime:
	// the failure otherwise is that the first accepted configuration
	// overwrites the credential and the agent locks itself out of its own
	// host, recoverable only by re-enrolling.
	directory := t.TempDir()
	path := filepath.Join(directory, "credential")

	cfg := enrolledConfig(t)
	cfg.CredentialFile = path
	cfg.ConfigFile = path

	err := cfg.Validate()
	if err == nil {
		t.Fatal("expected the identical paths to be refused")
	}
	if !strings.Contains(err.Error(), "must not be the same path") {
		t.Errorf("the refusal should say why, got %v", err)
	}
}
