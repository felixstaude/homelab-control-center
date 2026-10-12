module github.com/felixstaude/homelab-control-center/agents/linux

go 1.24

// Exactly one third-party dependency, and ADR-011 was amended rather than
// broken to allow it (see ADR-047).
//
// This binary runs as a long-lived process on other people's servers, so the
// original rule was "standard library only": the agent's supply chain is the
// Go standard library and nothing else. Milestone 1.8 needed a WebSocket
// client, and the standard library has none. The three options were:
//
//   - Hand-roll RFC 6455. Framing, masking, continuation, close handshakes
//     and ping/pong, written once and maintained by nobody, on the one code
//     path that parses untrusted input from the network. The worst of the
//     three by a wide margin.
//   - Use long-polling over net/http instead. No new dependency, and a
//     permanently worse protocol: no server-initiated frames, so revocation
//     could not cut an agent off until its next poll.
//   - Take one audited, dependency-free library.
//
// github.com/coder/websocket has **zero transitive dependencies** — `go mod
// graph` shows it requiring nothing but a Go version — so adding it widens
// the supply chain by one module rather than by a tree. It is also vendored
// (see ./vendor), so the source that gets compiled is in this repository, is
// reviewable in a diff, and does not depend on a module proxy being reachable
// or honest at build time.
require github.com/coder/websocket v1.8.15
