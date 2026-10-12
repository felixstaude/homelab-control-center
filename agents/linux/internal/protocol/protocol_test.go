package protocol_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

func TestAHeartbeatCarriesTheNegotiatedVersion(t *testing.T) {
	t.Parallel()

	heartbeat := protocol.NewHeartbeat("0.1.0", "linux/amd64", 3600)

	if heartbeat.V != protocol.Version {
		t.Errorf("v = %d, want %d", heartbeat.V, protocol.Version)
	}
	if heartbeat.Type != protocol.TypeHeartbeat {
		t.Errorf("type = %q, want %q", heartbeat.Type, protocol.TypeHeartbeat)
	}
}

func TestANegativeUptimeIsClampedRatherThanSent(t *testing.T) {
	t.Parallel()

	// The controller refuses a negative uptime, so a clock that stepped
	// backwards would otherwise cost the agent its connection over a cosmetic
	// field. NTP stepping a freshly-booted machine is the common cause.
	heartbeat := protocol.NewHeartbeat("0.1.0", "linux/amd64", -5)

	if heartbeat.UptimeSeconds != 0 {
		t.Errorf("uptime = %d, want it clamped to 0", heartbeat.UptimeSeconds)
	}
}

func TestAHeartbeatHasNoFieldForClaimingAHost(t *testing.T) {
	t.Parallel()

	// The absence that makes attribution trustworthy: the host comes from the
	// credential that authenticated the connection, so there is nothing here
	// for a compromised agent to claim to be. Asserted against the encoded
	// form, which is what actually crosses the wire.
	encoded, err := json.Marshal(protocol.NewHeartbeat("0.1.0", "linux/amd64", 1))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var fields map[string]any
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, forbidden := range []string{"host_id", "hostname", "host_name", "host"} {
		if _, present := fields[forbidden]; present {
			t.Errorf("the heartbeat carries %q, which an agent could use to claim another host", forbidden)
		}
	}
}

func TestNoMessageTypeCarriesAnythingExecutable(t *testing.T) {
	t.Parallel()

	// The most important property in this package. An agent that will run what
	// the controller tells it has turned a control-plane compromise into root
	// on every monitored machine. Asserted against the encoded forms rather
	// than trusted, because adding such a field would be a one-line change
	// that no behavioural test would notice.
	samples := []any{
		protocol.NewHeartbeat("v", "p", 1),
		protocol.HeartbeatAck{V: 1, Type: protocol.TypeHeartbeatAck, IntervalSeconds: 30},
		protocol.Close{V: 1, Type: protocol.TypeClose, Reason: protocol.CloseSuperseded},
	}
	forbidden := []string{"command", "cmd", "exec", "script", "shell", "run", "argv", "path"}

	for _, sample := range samples {
		encoded, err := json.Marshal(sample)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		var fields map[string]any
		if err := json.Unmarshal(encoded, &fields); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		for name := range fields {
			for _, word := range forbidden {
				if strings.Contains(strings.ToLower(name), word) {
					t.Errorf("%T has a field %q that looks executable", sample, name)
				}
			}
		}
	}
}

func TestAnAcknowledgementIsParsed(t *testing.T) {
	t.Parallel()

	frame, err := protocol.ParseServerFrame([]byte(
		`{"v":1,"type":"heartbeat_ack","interval_seconds":45,"server_time":"2026-01-01T00:00:00Z"}`,
	))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if frame.Type != protocol.TypeHeartbeatAck || frame.Ack == nil {
		t.Fatalf("frame = %+v, want an acknowledgement", frame)
	}
	if frame.Ack.IntervalSeconds != 45 {
		t.Errorf("interval = %d, want 45", frame.Ack.IntervalSeconds)
	}
}

func TestACloseFrameIsParsedAsAFrameNotAnError(t *testing.T) {
	t.Parallel()

	// The reason it carries is the whole point: the caller has to branch on it
	// to decide whether to retry, how fast, and whether to exit. Returning it
	// as an error would flatten that into one string.
	frame, err := protocol.ParseServerFrame([]byte(
		`{"v":1,"type":"close","reason":"host_disabled","detail":"disabled by an operator"}`,
	))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if frame.Close == nil || frame.Close.Reason != protocol.CloseHostDisabled {
		t.Fatalf("frame = %+v, want a host_disabled close", frame)
	}
	if frame.Close.Detail == "" {
		t.Error("the detail should survive parsing; it is what the operator reads")
	}
}

func TestAnOversizedFrameIsRefusedBeforeDecoding(t *testing.T) {
	t.Parallel()

	// Enforced on the agent side as well as the controller's, and not only for
	// symmetry: a compromised or impersonated controller sending an enormous
	// frame would otherwise be a memory exhaustion attack against the whole
	// fleet at once.
	oversized := []byte("{" + strings.Repeat("a", protocol.MaxFrameBytes))

	_, err := protocol.ParseServerFrame(oversized)

	var tooLarge *protocol.ErrFrameTooLarge
	if !asError(err, &tooLarge) {
		t.Fatalf("expected ErrFrameTooLarge, got: %v", err)
	}
}

func TestAnUnrecognisedFrameTypeIsAnErrorNotASkippedFrame(t *testing.T) {
	t.Parallel()

	// A frame the agent does not understand means the two sides disagree about
	// the protocol, and continuing is how an agent ends up believing a
	// connection is healthy while the controller is trying to close it.
	_, err := protocol.ParseServerFrame([]byte(`{"v":1,"type":"run_command"}`))
	if err == nil {
		t.Fatal("expected an unrecognised frame type to be rejected")
	}
	if !strings.Contains(err.Error(), "protocol version") {
		t.Errorf("the error should name the version mismatch as the likely cause, got: %v", err)
	}
}

func TestMalformedJSONIsReported(t *testing.T) {
	t.Parallel()

	for _, payload := range []string{"", "not json", "{", "[1,2]"} {
		if _, err := protocol.ParseServerFrame([]byte(payload)); err == nil {
			t.Errorf("expected %q to be rejected", payload)
		}
	}
}

func TestRetryabilityIsDecidedByReason(t *testing.T) {
	t.Parallel()

	cases := map[string]bool{
		protocol.CloseUnauthorized:        false,
		protocol.CloseUnsupportedProtocol: false,
		// Retried: the host will be enabled again, and an agent that gave up
		// would need somebody to go and start it.
		protocol.CloseHostDisabled: true,
		// Retried: usually this agent's own reconnection winning.
		protocol.CloseSuperseded: true,
		// Retried: a malformed frame may be a transient fault, and giving up
		// permanently on one bad frame would need a manual restart for a
		// problem that has passed.
		protocol.CloseProtocolError:      true,
		protocol.CloseRateLimited:        true,
		protocol.CloseIdleTimeout:        true,
		protocol.CloseControllerShutdown: true,
		// An unknown reason from a newer controller: retried, because refusing
		// to reconnect over a reason this build does not recognise would make
		// every future close reason a fleet-wide outage.
		"something-new": true,
	}

	for reason, want := range cases {
		if got := protocol.Retryable(reason); got != want {
			t.Errorf("Retryable(%q) = %t, want %t", reason, got, want)
		}
	}
}

func TestOnlyAnUnauthorizedCloseIsFatal(t *testing.T) {
	t.Parallel()

	// Fatal means the process exits non-zero, so systemd's restart counter and
	// `systemctl status` say something true. Only a revoked credential
	// qualifies: everything else either recovers or is worth waiting out.
	if !protocol.Fatal(protocol.CloseUnauthorized) {
		t.Error("an unauthorized close must be fatal")
	}
	for _, reason := range []string{
		protocol.CloseHostDisabled,
		protocol.CloseUnsupportedProtocol,
		protocol.CloseSuperseded,
		protocol.CloseProtocolError,
		protocol.CloseIdleTimeout,
		protocol.CloseControllerShutdown,
	} {
		if protocol.Fatal(reason) {
			t.Errorf("Fatal(%q) = true; only a revoked credential should stop the agent", reason)
		}
	}
}

func TestTheProtocolVersionHeaderNameIsStable(t *testing.T) {
	t.Parallel()

	// The controller reads this exact header. A rename on one side only is a
	// fleet-wide handshake failure, so the name is pinned here as well as
	// being a constant.
	if protocol.HeaderProtocolVersion != "X-HCC-Protocol-Version" {
		t.Errorf("header = %q, want X-HCC-Protocol-Version", protocol.HeaderProtocolVersion)
	}
}

// asError is errors.As with a concrete target, kept local so the test file
// does not need the errors import for one call.
func asError(err error, target **protocol.ErrFrameTooLarge) bool {
	if err == nil {
		return false
	}
	candidate, ok := err.(*protocol.ErrFrameTooLarge)
	if ok {
		*target = candidate
	}
	return ok
}
