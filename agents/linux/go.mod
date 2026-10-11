module github.com/felixstaude/homelab-control-center/agents/linux

go 1.24

// No third-party dependencies, deliberately (ADR-011).
//
// This binary runs as a long-lived process on other people's servers. The
// standard library covers everything the agent needs — structured logging
// (log/slog), flag parsing, signal handling and HTTP — so the agent's supply
// chain surface is the Go standard library and nothing else.
