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
//
// # 2 adds update management
//
// The update frames below are the first controller→agent messages added since
// this protocol existed, and they could not simply be sent: [ParseServerFrame]
// *errors* on an unrecognised type rather than ignoring it, so pushing an
// update_instruction to an already-installed fleet would make every agent
// report a protocol error and disconnect.
//
// So the controller supports {1, 2} and sends an update frame only to a
// connection that handshook at 2. An agent built before update management
// existed keeps working and is simply never offered an update over the socket.
//
// The notification rides on [HeartbeatAck] rather than arriving as a new
// frame, which is what makes the transition safe in the other direction too:
// Go's encoding/json ignores unknown object keys, so a version-1 agent
// receiving an acknowledgement with update_instruction_id in it ignores the
// field and carries on.
const Version = 2

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

	// Telemetry, phase 3. Measurements flow towards the controller — the
	// direction that cannot instruct anything — and the reply is two counts.
	TypeTelemetry    = "telemetry"
	TypeTelemetryAck = "telemetry_ack"

	// Update management, protocol 2.
	//
	// **Nothing here carries anything executable.** No command, no script, no
	// path to run, no shell. TypeUpdateInstruction is data describing which
	// published release to install; this agent verifies the publisher's
	// Ed25519 signature over the release metadata itself and decides whether
	// to proceed. An agent that ran what the controller told it would have
	// turned a control-plane compromise into root on every monitored machine.
	TypeUpdateInstruction = "update_instruction"
	TypeUpdateRequest     = "update_request"
	TypeUpdateProgress    = "update_progress"
	TypeUpdateResult      = "update_result"
	TypeUpdateCancel      = "update_cancel"
)

// Update states an agent may report.
//
// Mirrors hcc.models.updates.UpdateState, and only the subset an *agent*
// produces: the controller owns `available`, `queued`, `cancelled` and
// `deferred`, and an agent claiming one of those would be claiming a decision
// that is not its to make. The controller validates every report against its
// transition table regardless, so this list is the agent being well-behaved
// rather than the enforcement.
const (
	StateDownloading     = "downloading"
	StateVerifying       = "verifying"
	StateStaged          = "staged"
	StateInstalling      = "installing"
	StateRestarting      = "restarting"
	StateVerifyingHealth = "verifying_health"
	StateSucceeded       = "succeeded"
	StateFailed          = "failed"
	StateRolledBack      = "rolled_back"
)

// MaxUpdateDetailLength bounds what this agent attaches to a report.
//
// Bounded by the sender, not only by the controller, and for the same reason
// ConfigNack's problems are: a frame the controller refuses for size loses the
// report entirely, which is the one thing the report exists to prevent.
const MaxUpdateDetailLength = 1024

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

	// BufferedItems and DroppedItems report the telemetry queue, so an
	// operator can see a backlog building before it starts dropping — and see
	// that it dropped, which is the thing a silent queue would hide.
	//
	// DroppedItems is cumulative rather than since-last-report: a reader that
	// missed a heartbeat would otherwise under-count, and the controller can
	// difference two readings itself.
	BufferedItems int   `json:"buffered_items,omitempty"`
	DroppedItems  int64 `json:"dropped_items,omitempty"`
}

// HeartbeatState is everything a heartbeat reports about the agent.
//
// A struct rather than seven positional parameters. The function had three and
// grew to five with configuration and seven with the queue counters, at which
// point `NewHeartbeat(v, p, 3600, 7, "sha256:…", 12, 0)` is a line nobody can
// read and a line where two ints can be swapped silently.
type HeartbeatState struct {
	AgentVersion  string
	Platform      string
	UptimeSeconds int64

	// ConfigVersion and ConfigChecksum describe the configuration currently
	// applied. Zero and empty when there is none.
	ConfigVersion  int64
	ConfigChecksum string

	// BufferedItems and DroppedItems come from the telemetry queue.
	BufferedItems int
	DroppedItems  int64
}

// NewHeartbeat builds a heartbeat frame.
func NewHeartbeat(state HeartbeatState) Heartbeat {
	agentVersion, platform := state.AgentVersion, state.Platform
	uptimeSeconds := state.UptimeSeconds
	configVersion, configChecksum := state.ConfigVersion, state.ConfigChecksum
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
		BufferedItems:  state.BufferedItems,
		DroppedItems:   state.DroppedItems,
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

	// UpdateInstructionID names an update instruction waiting for this host,
	// or is empty.
	//
	// On the acknowledgement rather than in a new frame, and that is the whole
	// migration strategy: encoding/json ignores unknown keys, so a version-1
	// agent receiving this ignores it. A new frame type would have been a
	// protocol error for every agent already installed.
	//
	// The agent answers with an update_request and does **not** act on the
	// identifier: a notification is not evidence of anything, only an
	// instruction whose signature verifies is.
	UpdateInstructionID string `json:"update_instruction_id,omitempty"`
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

// MetricSample is one measurement.
//
// Value is a float64 because these are measurements: a utilisation percentage
// is genuinely fractional. That is the opposite of the rule for configuration,
// where integers are mandatory because the document is checksummed and two
// languages' float formatting need not agree. Nothing here is checksummed.
//
// Labels is omitted when empty rather than sent as {}, which is most samples —
// the controller treats an absent map and an empty one identically, and
// omitting it keeps a per-core batch meaningfully smaller.
type MetricSample struct {
	Metric string            `json:"metric"`
	Value  float64           `json:"value"`
	Labels map[string]string `json:"labels,omitempty"`
}

// TelemetryEntry is one module's collection.
type TelemetryEntry struct {
	Module        string `json:"module"`
	SchemaVersion int    `json:"schema_version"`
	CollectedAt   string `json:"collected_at"`
	DurationMs    int    `json:"duration_ms,omitempty"`
	// Error is why this collection produced nothing. Sent *with* an empty
	// sample list, because a module that has been failing for an hour is
	// information the operator needs.
	Error   string         `json:"error,omitempty"`
	Samples []MetricSample `json:"samples"`
}

// Telemetry carries a batch of measurements to the controller.
//
// **No host identifier**, the same absence [Heartbeat] has and for a sharper
// reason: the host is decided by the credential that authenticated the
// connection, so a compromised agent cannot submit telemetry attributed to
// another host because there is no field in which to name one. The controller
// forbids unknown fields, so adding one is a protocol error.
type Telemetry struct {
	V     int              `json:"v"`
	Type  string           `json:"type"`
	Seq   int64            `json:"seq"`
	Batch []TelemetryEntry `json:"batch"`
}

// NewTelemetry builds a telemetry frame.
func NewTelemetry(seq int64, batch []TelemetryEntry) Telemetry {
	return Telemetry{V: Version, Type: TypeTelemetry, Seq: seq, Batch: batch}
}

// TelemetryAck is the controller's confirmation that a batch is stored.
//
// Rejected is reported rather than being a silent difference: a sample the
// controller refused — an unknown metric, a module that is no longer enabled —
// is something the operator should see, and an agent told only "accepted: 11"
// of twelve would have no way to say which.
type TelemetryAck struct {
	V        int    `json:"v"`
	Type     string `json:"type"`
	Seq      int64  `json:"seq"`
	Accepted int    `json:"accepted"`
	Rejected int    `json:"rejected"`
	Detail   string `json:"detail,omitempty"`
}

// UpdateInstruction authorizes this agent to install one release.
//
// # What makes this safe to act on without a human
//
// Metadata and Signature. The digest the agent checks comes out of Metadata,
// whose signature was made by a key the controller does not hold — so a
// compromised control plane can withhold updates or offer an older one, which
// is its job, but cannot produce metadata this agent accepts for a binary the
// publisher did not build.
//
// Metadata is a **string**, carrying the exact bytes that were signed. Not a
// struct: the signature covers those bytes, and two JSON libraries disagree
// about key order, integer rendering and non-ASCII escaping, so verifying a
// re-serialisation would turn a formatting difference into something
// indistinguishable from tampering. The configuration snapshot carries its
// document the same way and for the same reason.
type UpdateInstruction struct {
	V    int    `json:"v"`
	Type string `json:"type"`

	InstructionID string `json:"instruction_id"`
	ToVersion     string `json:"to_version"`
	Platform      string `json:"platform"`
	// ExpiresAt is when this authorization stops being valid, RFC 3339.
	//
	// Enforced by the agent, which is the half that matters: the controller
	// cannot reach a disconnected host to withdraw anything, so a machine
	// switched on after a week must refuse by itself rather than install what
	// was decided then.
	ExpiresAt string `json:"expires_at"`
	Metadata  string `json:"metadata"`
	Signature string `json:"signature"`
	// AllowDowngrade is whether an administrator authorized going backwards.
	AllowDowngrade bool `json:"allow_downgrade,omitempty"`
}

// UpdateCancel withdraws an instruction.
//
// Advisory once the binary is being replaced: by then this agent's own
// rollback is the recovery path, and honouring a cancel would mean leaving a
// half-installed service behind.
type UpdateCancel struct {
	V             int    `json:"v"`
	Type          string `json:"type"`
	InstructionID string `json:"instruction_id"`
	Reason        string `json:"reason,omitempty"`
}

// UpdateRequest asks the controller for the instruction it mentioned.
//
// No fields beyond the envelope, the same deliberate absence [ConfigRequest]
// has: there is no instruction to name and no host to ask about, because the
// host is decided by the credential that authenticated the connection.
type UpdateRequest struct {
	V    int    `json:"v"`
	Type string `json:"type"`
}

// NewUpdateRequest builds a request.
func NewUpdateRequest() UpdateRequest {
	return UpdateRequest{V: Version, Type: TypeUpdateRequest}
}

// UpdateProgress reports a state transition as it happens.
type UpdateProgress struct {
	V    int    `json:"v"`
	Type string `json:"type"`

	InstructionID string `json:"instruction_id"`
	State         string `json:"state"`
	Detail        string `json:"detail,omitempty"`
}

// NewUpdateProgress builds a progress report, clamped to what the controller
// accepts.
func NewUpdateProgress(instructionID, state, detail string) UpdateProgress {
	return UpdateProgress{
		V:             Version,
		Type:          TypeUpdateProgress,
		InstructionID: instructionID,
		State:         state,
		Detail:        clampDetail(detail),
	}
}

// UpdateResult reports a terminal outcome and the version now running.
//
// ResultingVersion is reported separately from the instruction's target
// because they can differ, and the difference is the interesting case: a
// result claiming success while the host runs the old version is a rollback
// nobody noticed.
type UpdateResult struct {
	V    int    `json:"v"`
	Type string `json:"type"`

	InstructionID    string `json:"instruction_id"`
	State            string `json:"state"`
	ResultingVersion string `json:"resulting_version,omitempty"`
	Error            string `json:"error,omitempty"`
}

// NewUpdateResult builds a result, clamped to what the controller accepts.
func NewUpdateResult(instructionID, state, resultingVersion, failure string) UpdateResult {
	return UpdateResult{
		V:                Version,
		Type:             TypeUpdateResult,
		InstructionID:    instructionID,
		State:            state,
		ResultingVersion: resultingVersion,
		Error:            clampDetail(failure),
	}
}

// clampDetail bounds a message to what the controller will accept.
//
// Truncated rather than dropped: losing the tail of an error is better than
// losing the report, and a frame over the limit is refused whole.
func clampDetail(text string) string {
	if len(text) <= MaxUpdateDetailLength {
		return text
	}
	return text[:MaxUpdateDetailLength]
}

// ServerFrame is one decoded controller → agent frame.
//
// A tagged union rather than an interface, because a caller has to handle
// every case: an interface would let a new case be added and silently ignored
// by every existing switch. Exactly one pointer is non-nil, and which one is
// named by Type.
type ServerFrame struct {
	Type string

	Ack          *HeartbeatAck
	Close        *Close
	Available    *ConfigAvailable
	Snapshot     *ConfigSnapshot
	TelemetryAck *TelemetryAck
	Instruction  *UpdateInstruction
	Cancel       *UpdateCancel
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

	case TypeTelemetryAck:
		var ack TelemetryAck
		if err := json.Unmarshal(raw, &ack); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeTelemetryAck, err)
		}
		return ServerFrame{Type: TypeTelemetryAck, TelemetryAck: &ack}, nil

	case TypeUpdateInstruction:
		var instruction UpdateInstruction
		if err := json.Unmarshal(raw, &instruction); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeUpdateInstruction, err)
		}
		return ServerFrame{Type: TypeUpdateInstruction, Instruction: &instruction}, nil

	case TypeUpdateCancel:
		var cancel UpdateCancel
		if err := json.Unmarshal(raw, &cancel); err != nil {
			return ServerFrame{}, fmt.Errorf("malformed %s frame: %w", TypeUpdateCancel, err)
		}
		return ServerFrame{Type: TypeUpdateCancel, Cancel: &cancel}, nil

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
