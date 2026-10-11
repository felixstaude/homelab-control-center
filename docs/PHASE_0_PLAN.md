# Phase 0 Implementation Plan

Status: **complete**. This document is kept as the record of what Phase 0 set out to
do and which deviations from the original brief were taken, and why.

## Goal

Establish a production-oriented foundation — build system, architecture, design
system, CI — for a self-hosted homelab monitoring platform. **No product features.**

## Work breakdown

| Task | Scope | Deliverable |
| ---- | ----- | ----------- |
| A | Architecture | `docs/ARCHITECTURE.md`, `docs/SECURITY.md` |
| B | Monorepo layout | `apps/`, `agents/`, `deploy/`, `docs/`, `scripts/`, root `Makefile` |
| C | Backend foundation | FastAPI app, settings, structured logging, error handling, `/healthz`, `/readyz`, SQLAlchemy, Alembic, pytest |
| D | Frontend foundation | Vite + React + TS, Tailwind v4 design tokens, app shell, 7 nav placeholders, labelled mock overview |
| E | Go agent foundation | config, `slog` logging, graceful shutdown, version info, documented controller interface, unit tests |
| F | Dev environment | `compose.yaml` (Postgres), `.env.example`, `.gitignore`, README |
| G | Quality + CI | ruff, mypy, pytest, ESLint, `tsc`, vitest, gofmt, `go vet`, `go test`, GitHub Actions |

## Accepted deviations from the brief

Each deviation is justified here and cross-referenced from
`docs/ARCHITECTURE.md` as an ADR.

1. **No `packages/` directory.** The brief suggests one. Phase 0 has exactly one
   producer and one consumer of shared types, and the sharing mechanism will be an
   OpenAPI-generated client (ADR-007), not a hand-written package. An empty
   `packages/` would be speculative structure, violating engineering rule 3. It gets
   created when a second consumer exists.

2. **Ruff replaces Black + isort + Flake8.** One tool, one config, same formatting
   contract, materially faster. Ruff's formatter is Black-compatible.

3. **Async SQLAlchemy + asyncpg**, not sync. The ingestion path (logs, metrics) and
   Phase 4 WebSockets are both I/O-bound and concurrent; retrofitting async later
   would touch every repository function.

4. **Tailwind CSS v4** (CSS-first `@theme`) rather than v3 with `tailwind.config.js`.
   v4 is current, the Vite plugin removes the PostCSS layer, and design tokens live in
   CSS where the rest of the design system already is.

5. **`slog` only in the agent — zero third-party dependencies.** The stdlib covers
   structured logging, flag parsing, and signal handling. An agent that runs on other
   people's servers should have the smallest possible dependency surface.

6. **Compose file targets both Docker and Podman.** The development box for this
   project runs Fedora with Podman 5.8 and no Docker. The compose file uses only
   OCI-portable keys, so `docker compose` and `podman-compose` both work. See ADR-010.

7. **Two health endpoints, not one.** `/healthz` is a liveness probe with no
   dependency checks; `/readyz` verifies the database. Collapsing them would make a
   brief database blip restart the container. See ADR-005.

## Verification gate

Phase 0 is not done until, actually executed:

- `ruff format --check`, `ruff check`, `mypy` clean
- `pytest` green (unit always; integration when a Postgres URL is provided)
- `tsc --noEmit`, `eslint`, `vitest`, `vite build` clean
- `gofmt -l` empty, `go vet`, `go test` green
