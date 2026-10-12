// Package protocol is the agent's half of the controller wire format.
//
// It mirrors apps/server/hcc/agents/protocol.py, and the duplication is
// deliberate: these are two independently deployed programs that have to agree
// on a format, and a shared definition is not available across the language
// boundary without a code-generation step this project does not have yet
// (milestone 1.15 covers the browser client's types; the agent is a separate
// question).
//
// What keeps the two in step is [Version] and the controller's refusal to
// accept a handshake outside the set it supports. An agent built against a
// version the controller has dropped is told so, in one clear message, rather
// than having its frames parsed under the wrong assumptions.
//
// # There is nothing executable here
//
// No message type carries a command, a script, a path to run or a module to
// load, and that is the most important property of this file. An agent that
// will run what the controller tells it has turned a control-plane compromise
// into root on every monitored machine. See docs/AGENT_ARCHITECTURE.md §3.
package protocol

import (
	"encoding/json"
	"fmt"
)

// Version is the protocol version this agent speaks.
//
// Sent on the handshake in the X-HCC-Protocol-Version header and in every
// frame's "v" field. A controller that does not speak it refuses the handshake
// with HTTP 400 and names the versions it does.
const Version = 1

// HeaderProtocolVersion carries [Version] on the WebSocket handshake.
//
// A header rather than a query parameter, for the same reason the credential
// is: a handshake URL ends up in proxy access logs, and while the version is
// not secret, keeping the whole handshake out of the URL means there is never
// a question about which parts of it are.
const HeaderProtocolVersion = "X-HCC-Protocol-Version"

// Message types. A closed set on both sides: the controller refuses an
// unrecognised type rather than ignoring it, and so does [ParseServerFrame].
const (
	TypeHeartbeat    = "heartbeat"
	TypeHeartbeatAck = "heartbeat_ack"
	TypeClose        = "close"

	// Configuration synchronisation, phase 2. See docs/ADR-049: configuration
	// travels on this socket rather than over a second HTTP channel, because
	// the socket is already authenticated and identity-bound.
	//
	// TypeConfigAvailable is a *notification* carrying only a version. The
	// agent's correctness does not depend on receiving one: every heartbeat
	// acknowledgement states the version the controller has, so a missed
	// notification costs one heartbeat interval of latency.
	TypeConfigAvailable = "config_available"
	TypeConfigRequest   = "config_request"
	TypeConfigSnapshot  = "config_snapshot"
	TypeConfigAck       = "config_ack"
	TypeConfigNack      = "config_nack"
)

// Close reasons the controller may send.
//
// The agent branches on these to decide whether to retry, and how fast. The
// distinction that matters most is between CloseUnauthorized — which means
// human intervention is required and retrying fast is pointless — and
// CloseSuperseded, which usually means this agent's own newer connection won
// and is not a reason to back off at all.
const (
	// CloseUnauthorized means the credential is not usable. Deliberately the
	// same answer for a credential that never existed and one that was
	// revoked: there is nothing the agent could do differently, and the
	// controller declining to say which is what stops this being an oracle.
	CloseUnauthorized = "unauthorized"
	// CloseHostDisabled means the credential is fine and an operator has
	// switched this host off. Worth retrying patiently: it will come back.
	CloseHostDisabled = "host_disabled"
	// CloseUnsupportedProtocol means this agent's build is too old or too new.
	// Retrying cannot fix it.
	CloseUnsupportedProtocol = "unsupported_protocol"
	// CloseProtocolError means a frame was malformed, oversized or unknown.
	CloseProtocolError = "protocol_error"
	// CloseRateLimited means this agent sent frames faster than its budget.
	CloseRateLimited = "rate_limited"
	// CloseIdleTimeout means no heartbeat arrived in time.
	CloseIdleTimeout = "idle_timeout"
	// CloseSuperseded means a newer connection for this host was accepted.
	CloseSuperseded = "superseded"
	// CloseControllerShutdown means the controller is restarting.
	CloseControllerShutdown = "controller_shutdown"
)

// MaxFrameBytes is the largest frame either side will read.
//
// Enforced by the agent as well as by the controller, and not only out of
// symmetry: a compromised or impersonated controller sending an enormous frame
// would otherwise be a memory exhaustion attack against every host in the
// fleet at once, which is a far worse position to be in than the reverse.
const MaxFrameBytes = 64 * 1024

// Heartbeat is the agent's liveness frame.
//
// It carries no host identifier, and the absence is the design. The host is
// decided by the credential that authenticated the connection, so there is
// nothing here for an agent — or for something that has stolen an agent's
// credential — to claim to be. Every other field is advisory: the controller
// stores them for display and never makes a decision from them.
type Heartbeat struct {
	V    int    `json:"v"`
	Type string `json:"type"`

	AgentVersion  string `json:"agent_version,omitempty"`
	Platform      string `json:"platform,omitempty"`
	UptimeSeconds int64  `json:"uptime_seconds,omitempty"`

	// ConfigVersion and ConfigChecksum report the configuration this agent is
	// actually running. They are what makes the synchronisation loop
	// self-correcting rather than dependent on every push arriving.
	//
	// `omitempty` is load-bearing on both: an agent with no configuration must
	// report that by *omission*, not by sending zero. There is no version zero,
	// and claiming one would make "never configured" indistinguishable from
	// "configured, then something went wrong" — the controller refuses a
	// version below 1 for exactly that reason.
	ConfigVersion  int64  `json:"config_version,omitempty"`
	ConfigChecksum string `json:"config_checksum,omitempty"`
}

// NewHeartbeat builds a heartbeat frame.
//
// configVersion and configChecksum describe the configuration currently
// applied; pass 0 and "" when there is none.
func NewHeartbeat(
	agentVersion, platform string,
	uptimeSeconds int64,
	configVersion int64,
	configChecksum string,
) Heartbeat {
	// Negative uptime is impossible and the controller refuses it, so a clock
	// that stepped backwards is clamped rather than turned into a disconnect.
	// An agent must not be able to lose its connection over a cosmetic field.
	if uptimeSeconds < 0 {
		uptimeSeconds = 0
	}
	// Likewise a nonsense version: the controller refuses anything below 1,
	// and reporting nothing is both honest and keeps the connection.
	if configVersion < 1 {
		configVersion = 0
		configChecksum = ""
	}
	return Heartbeat{
		V:              Version,
		Type:           TypeHeartbeat,
		AgentVersion:   agentVersion,
		Platform:       platform,
		UptimeSeconds:  uptimeSeconds,
		ConfigVersion:  configVersion,
		ConfigChecksum: configChecksum,
	}
}

// HeartbeatAck is the controller's acknowledgement.
//
// IntervalSeconds lets the controller correct an agent whose configured
// cadence has drifted, which matters because the controller cannot reach into
// the fleet to reconfigure anything — it can only answer.
type HeartbeatAck struct {
	V               int    `json:"v"`
	Type            string `json:"type"`
	IntervalSeconds int    `json:"interval_seconds"`
	ServerTime      string `json:"server_time"`

	// ConfigVersion is the version the controller has for this host, or 0 when
	// it has none. An agent running something else should send a
	// config_request — which is the whole drift-detection mechanism, in one
	// field, needing no separate channel and no push to have arrived.
	ConfigVersion int64 `json:"config_version,omitempty"`
}

// Close is the controller's termination notice.
//
// Sent as a frame before the WebSocket close, so the agent's log says why
// rather than reporting a numeric close code and leaving the operator to look
// it up.
type Close struct {
	V      int    `json:"v"`
	Type   string `json:"type"`
	Reason string `json:"reason"`
	Detail string `json:"detail,omitempty"`

	SupportedVersions []int `json:"supported_versions,omitempty"`
}

// ConfigAvailable tells the agent a newer configuration exists.
//
// Carries a version and nothing else. The agent answers with a
// [ConfigRequest]; it does not act on the version itself, because a
// notification is not evidence of anything — only a snapshot whose checksum
// verifies is.
type ConfigAvailable struct {
	V       int    `json:"v"`
	Type    string `json:"type"`
	Version int64  `json:"version"`
}

// ConfigSnapshot carries one configuration document.
//
// Document is a *string*, and that is the whole of the checksum design: it
// holds the canonical bytes the controller computed the digest over, so the
// agent verifies sha256(Document) == Checksum on exactly what arrived and then
// parses it.
//
// Decoding into a struct and re-serialising to check instead would make the
// comparison depend on Go's encoding/json producing byte-identical output to
// Python's json — which it does not, for key order, integer rendering and
// non-ASCII escaping. The failure would be a checksum mismatch that looks
// exactly like tampering.
type ConfigSnapshot struct {
	V        int    `json:"v"`
	Type     string `json:"type"`
	Version  int64  `json:"version"`
	Checksum string `json:"checksum"`
	Document string `json:"document"`
}

// ConfigRequest asks the controller for the current configuration.
//
// No fields beyond the envelope, and the absence is deliberate in the same way
// [Heartbeat]'s missing host identifier is: there is no version to ask for and
// no host to ask about, so there is nothing here to abuse. The controller
// forbids unknown fields, so adding one is a protocol error rather than a
// silently ignored key.
type ConfigRequest struct {
	V    int    `json:"v"`
	Type string `json:"type"`
}

// NewConfigRequest builds a configuration request.
func NewConfigRequest() ConfigRequest {
	return ConfigRequest{V: Version, Type: TypeConfigRequest}
}

// ConfigAck reports that a configuration was verified and applied.
type ConfigAck struct {
	V        int    `json:"v"`
	Type     string `json:"type"`
	Version  int64  `json:"version"`
	Checksum string `json:"checksum"`
}

// NewConfigAck builds an acknowledgement.
//
// The checksum travels as well as the version so that a truncated or tampered
// document cannot be acknowledged as applied under a version that looks right.
func NewConfigAck(version int64, checksum string) ConfigAck {
	return ConfigAck{V: Version, Type: TypeConfigAck, Version: version, Checksum: checksum}
}

// ConfigNack reports that a configuration was refused, and why.
//
// The agent keeps running its last known good configuration. A monitoring
// agent that stops monitoring because somebody typed a bad interval has failed
// at its job.
type ConfigNack struct {
	V        int      `json:"v"`
	Type     string   `json:"type"`
	Version  int64    `json:"version"`
	Checksum string   `json:"checksum"`
	Problems []string `json:"problems"`
}

// MaxNackProblems and MaxNackProblemLength bound a rejection.
//
// Bounded by the *sender*, not only by the controller. The controller clamps
// too, but an agent that sent a megabyte of problems would have its frame
// refused for size before the clamp ran — losing the rejection entirely. So
// the agent sends something that will be accepted.
const (
	MaxNackProblems      = 32
	MaxNackProblemLength = 256
)

// NewConfigNack builds a rejection, clamped to what the controller accepts.
func NewConfigNack(version int64, checksum string, problems []string) ConfigNack {
	clamped := make([]string, 0, MaxNackProblems+1)
	for _, problem := range problems {
		if len(clamped) == MaxNackProblems {
			break
		}
		if len(problem) > MaxNackProblemLength {
			problem = problem[:MaxNackProblemLength]
		}
		clamped = append(clamped, problem)
	}
	if remaining := len(problems) - MaxNackProblems; remaining > 0 {
		clamped = append(clamped, fmt.Sprintf("… and %d further problem(s), not sent", remaining))
	}
	return ConfigNack{
		V:        Version,
		Type:     TypeConfigNack,
		Version:  version,
		Checksum: checksum,
		Problems: clamped,
	}
}

// ServerFrame is one decoded controller → agent frame.
//
// A tagged union rather than an interface, because a caller has to handle
// every case: an interface would let a new case be added and silently ignored
// by every existing switch. Exactly one pointer is non-nil, and which one is
// named by Type.
type ServerFrame struct {
	Type string

	Ack       *HeartbeatAck
	Close     *Close
	Available *ConfigAvailable
	Snapshot  *ConfigSnapshot
}

// ErrFrameTooLarge reports a frame the agent refused to decode.
type ErrFrameTooLarge struct {
	Size int
}

func (e *ErrFrameTooLarge) Error() string {
	return fmt.Sprintf("controller frame is %d bytes, over the %d byte limit", e.Size, MaxFrameBytes)
}

// ParseServerFrame decodes a controller → agent frame.
//
// Size is checked before the JSON decoder is given anything, so an oversized
// frame costs a length comparison rather than an allocation.
//
// An unrecognised type is an error rather than a silently skipped frame. A
// frame the agent does not understand means the two sides disagree about the
// protocol, and continuing as though nothing happened is how an agent ends up
// believing a connection is healthy while the controller is trying to close it.
func ParseServerFrame(raw []byte) (ServerFrame, error) {
	if len(raw) > MaxFrameBytes {
		return ServerFrame{}, &ErrFrameTooLarge{Size: len(raw)}
	}

	var envelope struct {
		V    int    `json:"v"`
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return ServerFrame{}, fmt.Errorf("controller frame is not valid JSON: %w", err)
	}

	switch envelope.Type {
	case TypeHeartbeatAck:
		var ack HeartbeatAck
		if err := json.Unmarshal(raw, &ack); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeHeartbeatAck, err)
		}
		return ServerFrame{Type: TypeHeartbeatAck, Ack: &ack}, nil

	case TypeClose:
		var closed Close
		if err := json.Unmarshal(raw, &closed); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeClose, err)
		}
		return ServerFrame{Type: TypeClose, Close: &closed}, nil

	case TypeConfigAvailable:
		var available ConfigAvailable
		if err := json.Unmarshal(raw, &available); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeConfigAvailable, err)
		}
		return ServerFrame{Type: TypeConfigAvailable, Available: &available}, nil

	case TypeConfigSnapshot:
		var snapshot ConfigSnapshot
		if err := json.Unmarshal(raw, &snapshot); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeConfigSnapshot, err)
		}
		return ServerFrame{Type: TypeConfigSnapshot, Snapshot: &snapshot}, nil

	default:
		return ServerFrame{}, fmt.Errorf(
			"controller sent an unrecognised frame type; this agent speaks protocol version %d",
			Version,
		)
	}
}

// Retryable reports whether an agent should keep trying after this close
// reason, and is the one piece of policy in this file.
//
// The three that return false all mean the same thing operationally: no amount
// of reconnecting will help, and a tight retry loop would only add load to a
// controller that has already refused. An operator has to do something — fix
// the credential, upgrade the agent — and the agent's job is to say so clearly
// and stop hammering.
//
// Everything else, including a protocol error, is retried: a malformed frame
// may be a transient encoding fault, and an agent that gave up permanently on
// one bad frame would need a manual restart for a problem that has passed.
func Retryable(reason string) bool {
	switch reason {
	case CloseUnauthorized, CloseUnsupportedProtocol:
		return false
	default:
		return true
	}
}

// Fatal reports whether a close reason should stop the agent entirely rather
// than leave it retrying slowly.
//
// Only CloseUnauthorized qualifies. An unauthorized agent is one whose
// credential has been revoked or never existed, and the remedy is
// re-enrollment by a human — so the process exits non-zero, which is what
// makes systemd's restart counter and `systemctl status` tell the operator
// something true instead of showing a process that is up and doing nothing.
//
// CloseUnsupportedProtocol deliberately does *not* qualify, even though it is
// also not retryable in the short term: a controller being upgraded may speak
// the agent's version again in a minute, and an agent that exited would need a
// manual start afterwards.
func Fatal(reason string) bool {
	return reason == CloseUnauthorized
}
