package main

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// The joining of the session loop to the privileged installer happens once, in
// updateOptions. These tests are about that join rather than about the
// updater, which has its own suite: what is at stake here is whether a real
// `hcc-agent run` can be made to install anything, and under what conditions.

func loggerTo(buffer *bytes.Buffer) *slog.Logger {
	return slog.New(slog.NewTextHandler(buffer, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

func TestNoUpdateApplierIsInstalledWhenUpdatesAreOff(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer
	options, err := updateOptions(config.Config{AutoUpdate: false}, loggerTo(&logs))
	if err != nil {
		t.Fatalf("building options failed: %v", err)
	}

	// No applier at all, rather than an applier that refuses. The agent then
	// has no code path from a socket frame to the installer, which is a
	// stronger statement than "the installer declines".
	if len(options) != 0 {
		t.Fatalf("got %d options for a host with updates off, want none", len(options))
	}
	if !strings.Contains(logs.String(), config.EnvAutoUpdate) {
		t.Errorf("the log does not say how to enable updates: %s", logs.String())
	}
}

func TestAnApplierIsInstalledWhenUpdatesAreOn(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer
	options, err := updateOptions(
		config.Config{
			AutoUpdate:    true,
			UpdateChannel: "beta",
			ReleaseKeys:   []string{"BMJvLkLsgw+qZiwSa9mZq97XL7G5n/l6rKd2Pa0fWT4="},
		},
		loggerTo(&logs),
	)
	if err != nil {
		t.Fatalf("building options failed: %v", err)
	}
	if len(options) != 1 {
		t.Fatalf("got %d options, want one", len(options))
	}
	if !strings.Contains(logs.String(), "channel=beta") {
		t.Errorf("the log does not report the channel: %s", logs.String())
	}
	if !strings.Contains(logs.String(), "trusted_keys=1") {
		t.Errorf("the log does not report the trust store size: %s", logs.String())
	}
}

func TestAnUndecodableKeyIsAStartupFailure(t *testing.T) {
	t.Parallel()

	// Config.Load already refuses this, so reaching updateApplier with one
	// means something else built the config. Refusing here too means the
	// binary cannot be started with a trust store it could not build —
	// starting anyway would run a host that reports itself healthy and
	// declines every update.
	var logs bytes.Buffer
	_, err := updateApplier(
		config.Config{AutoUpdate: true, ReleaseKeys: []string{"!!!not-a-key"}},
		loggerTo(&logs),
	)
	if err == nil {
		t.Fatal("an undecodable key was accepted")
	}
}

func TestADevelopmentBuildTrustsNothingAndSaysSo(t *testing.T) {
	t.Parallel()

	// Every build of this repository that is not an official release. The
	// agent runs, declines instructions, and explains why — rather than
	// refusing to start, which would make a development agent unusable, or
	// staying quiet, which would make the refusals a mystery.
	var logs bytes.Buffer
	applier, err := updateApplier(config.Config{AutoUpdate: true}, loggerTo(&logs))
	if err != nil {
		t.Fatalf("building the applier failed: %v", err)
	}
	if applier == nil {
		t.Fatal("no applier for a host with updates enabled")
	}
	if !strings.Contains(logs.String(), "trusts no publisher key") {
		t.Errorf("the warning is missing: %s", logs.String())
	}
}

func TestTheApplierDeclinesEveryInstructionWithoutATrustedKey(t *testing.T) {
	t.Parallel()

	// The end-to-end consequence of the previous test, driven through the
	// real adapter and the real updater: an instruction that is otherwise
	// perfectly formed — unexpired, for this platform, naming a newer version
	// — is declined, and the reason reaches the controller.
	var logs bytes.Buffer
	applier, err := updateApplier(config.Config{AutoUpdate: true}, loggerTo(&logs))
	if err != nil {
		t.Fatalf("building the applier failed: %v", err)
	}
	if applier == nil {
		t.Fatal("no applier for a host with updates enabled")
	}

	recorder := &recordingReporter{}
	err = applier.Apply(context.Background(), protocol.UpdateInstruction{
		InstructionID: "11111111-1111-4111-8111-111111111111",
		ToVersion:     "9.9.9",
		Platform:      buildinfo.Get().Platform,
		ExpiresAt:     "2099-01-01T00:00:00Z",
		Metadata:      `{"schema_version":2}`,
		Signature:     `{"algorithm":"ed25519","key_id":"0000000000000000","signature":"` + strings.Repeat("A", 86) + `=="}`,
	}, recorder)
	if err == nil {
		t.Fatal("an instruction was accepted by a build that trusts no key")
	}

	// Reported, not merely refused. "Why has this host not updated" has to
	// have an answer in the history, and silence is the one answer that makes
	// a working refusal indistinguishable from a broken agent.
	if len(recorder.results) != 1 {
		t.Fatalf("results = %v, want exactly one", recorder.results)
	}
	if recorder.results[0].state != protocol.StateFailed {
		t.Errorf("state = %q, want %q", recorder.results[0].state, protocol.StateFailed)
	}
	if recorder.results[0].failure == "" {
		t.Error("the controller was told nothing about why")
	}
}

type recordingReporter struct {
	results []struct {
		state   string
		failure string
	}
}

func (r *recordingReporter) Progress(_ context.Context, _, _ string) error { return nil }

func (r *recordingReporter) Result(_ context.Context, state, _, failure string) error {
	r.results = append(r.results, struct {
		state   string
		failure string
	}{state, failure})
	return nil
}
