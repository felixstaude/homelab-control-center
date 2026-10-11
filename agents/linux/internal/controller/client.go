// Package controller defines the agent's interface to the control plane.
//
// Phase 0 ships the contract only — there is no HTTP implementation yet, by
// design (engineering rule 8: no placeholder that pretends to work). Defining
// the interface now documents the intended protocol and lets the agent's
// runtime loop be written and tested against a fake.
//
// # Protocol
//
// The agent is always the client. It dials out to the controller over HTTPS
// and the controller never connects back to the agent. Consequences, all
// deliberate:
//
//   - The agent needs no inbound firewall rule and no listening socket.
//   - The agent works behind NAT, which is the normal homelab case.
//   - The Docker socket is never exposed on the network. The agent reads its
//     own host's /var/run/docker.sock locally and sends results outward. See
//     docs/SECURITY.md.
//
// # Authentication
//
// Every request carries a bearer enrolment token. Transport is TLS. The
// controller authorises per host, so a compromised agent token grants access
// to one host's data, not the fleet.
//
// # Management commands
//
// The agent polls for pending commands rather than accepting pushed ones,
// which preserves the no-inbound-connections property above. Commands are
// drawn from a closed set defined by CommandType — there is no generic
// "run this" command, and the agent must reject any type it does not
// recognise. Arbitrary shell execution is explicitly out of scope for the
// product.
package controller

import (
	"context"
	"errors"
	"time"
)

// Errors the transport is expected to return, so callers can branch on cause
// rather than string matching.
var (
	// ErrUnauthenticated means the token was rejected. Retrying will not help
	// until the operator re-enrols the agent.
	ErrUnauthenticated = errors.New("controller rejected the agent token")

	// ErrUnavailable means the controller could not be reached or returned
	// 5xx. Retrying with backoff is appropriate.
	ErrUnavailable = errors.New("controller is unavailable")

	// ErrNotImplemented is returned by the Phase 0 stub. It exists so that a
	// caller wiring up the runtime loop gets an explicit, testable failure
	// instead of silent success from a fake implementation.
	ErrNotImplemented = errors.New("controller client is not implemented yet")
)

// RegistrationRequest is sent once at startup to announce the agent.
type RegistrationRequest struct {
	Hostname     string `json:"hostname"`
	AgentVersion string `json:"agent_version"`
	Platform     string `json:"platform"`
	// StartedAt lets the controller distinguish a restart from a new host.
	StartedAt time.Time `json:"started_at"`
}

// RegistrationResponse is the controller's acknowledgement.
type RegistrationResponse struct {
	// HostID is the controller's identifier for this host. The agent stores
	// it and includes it in subsequent calls.
	HostID string `json:"host_id"`
	// ReportInterval lets the controller override the agent's configured
	// interval, so an operator can retune the fleet centrally.
	ReportInterval time.Duration `json:"report_interval"`
}

// Heartbeat reports that the agent is alive.
//
// Phase 0 carries no measurements. Metric payloads are added in Phase 2 as a
// separate method, so that liveness reporting and metric ingestion can fail
// and be rate-limited independently.
type Heartbeat struct {
	HostID       string        `json:"host_id"`
	SentAt       time.Time     `json:"sent_at"`
	Uptime       time.Duration `json:"uptime"`
	AgentVersion string        `json:"agent_version"`
}

// CommandType enumerates the management actions an agent will ever perform.
//
// Closed by design. Adding a member is a deliberate decision requiring a
// matching authorisation rule and audit entry on the controller. There is no
// member for executing arbitrary commands, and there never will be.
type CommandType string

const (
	CommandContainerStart   CommandType = "container.start"
	CommandContainerStop    CommandType = "container.stop"
	CommandContainerRestart CommandType = "container.restart"
)

// Command is a single management action issued by the controller.
type Command struct {
	ID   string      `json:"id"`
	Type CommandType `json:"type"`
	// TargetID identifies the object to act on, e.g. a container ID.
	TargetID string `json:"target_id"`
	// IssuedAt and ExpiresAt let the agent refuse stale commands, so a
	// command queued during a network partition cannot execute much later
	// against changed state.
	IssuedAt  time.Time `json:"issued_at"`
	ExpiresAt time.Time `json:"expires_at"`
}

// CommandResult reports the outcome back to the controller for the audit log.
type CommandResult struct {
	CommandID string `json:"command_id"`
	Succeeded bool   `json:"succeeded"`
	// Error is the failure reason when Succeeded is false. Never empty in
	// that case: a failed command with no reason is not auditable.
	Error       string    `json:"error,omitempty"`
	CompletedAt time.Time `json:"completed_at"`
}

// Client is the agent's view of the control plane.
//
// Implementations must be safe for concurrent use and must respect context
// cancellation so that shutdown is not blocked by an in-flight request.
type Client interface {
	// Register announces this agent and obtains its host identity.
	Register(ctx context.Context, req RegistrationRequest) (RegistrationResponse, error)

	// SendHeartbeat reports liveness.
	SendHeartbeat(ctx context.Context, hb Heartbeat) error

	// PollCommands retrieves pending management commands. Returns an empty
	// slice, not an error, when there is nothing to do.
	PollCommands(ctx context.Context, hostID string) ([]Command, error)

	// ReportCommandResult records the outcome of a command.
	ReportCommandResult(ctx context.Context, result CommandResult) error
}
