# Development

How to get a working development environment and the conventions to follow
once you have one.

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.12+ | Verified on 3.14 |
| Node.js | 20.19+ or 22.12+ | Required by Vite 8 |
| Go | 1.24+ | Agent only |
| Docker or Podman | any current | Podman is fully supported (ADR-010) |
| GNU Make | 4+ | Task runner |

### Fedora

```bash
sudo dnf install -y python3 python3-pip nodejs npm golang podman podman-compose make git
```

Fedora ships Podman rather than Docker. Everything works with it — the
Makefile detects which runtime is installed. Confirm with:

```bash
make help    # prints the detected runtime on the last line
```

### Debian / Ubuntu

```bash
sudo apt install -y python3 python3-venv python3-pip nodejs npm golang make git
# plus Docker Engine from docs.docker.com
```

If your distribution's Node or Go is too old, use
[`nvm`](https://github.com/nvm-sh/nvm) and
[the official Go tarball](https://go.dev/dl/).

## First-time setup

```bash
git clone https://github.com/felixstaude/homelab-control-center.git
cd homelab-control-center

make setup       # Python venv + npm install
make env         # create .env files from the committed examples
make db-up       # start PostgreSQL and wait for readiness
make migrate     # apply migrations
make bootstrap   # create the initial administrator (prompts)
```

`make setup` takes a few minutes on a first run. Everything after it is fast.

### Creating the initial administrator

A fresh installation has no accounts, and there is deliberately **no HTTP
endpoint** that creates the first one — see
[ADR-033](ARCHITECTURE.md#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint).
It is created by a local command, which proves access to the database:

```bash
make bootstrap
# or, directly:
cd apps/server && .venv/bin/hcc bootstrap-admin --username admin
```

The password is prompted for twice with echo off. The minimum is 8 characters
and there are no composition rules (ADR-026).

For automation, read it from standard input so it never appears in a process
listing or in shell history:

```bash
hcc bootstrap-admin --username admin --password-stdin < /run/secrets/admin-password
```

The password is **never** accepted as a command-line argument or an
environment variable. Exit codes: `0` created, `1` runtime failure, `2` usage
or input error, `3` already initialized — so a provisioning script can treat
`3` as success.

**Bootstrap runs once per installation and does not reopen.** Initialization is
recorded as a row in the `installation` table, so deleting or deactivating
every account does *not* let it run again. In development, to start over:

```bash
make db-reset    # destroys the volume and re-applies migrations
```

There is no password-reset flow yet, so a forgotten development password means
`make db-reset`. In a real deployment it means issuing SQL — the same
authority bootstrap required. Recorded as a known gap in
[`SECURITY.md`](SECURITY.md#known-gaps).

## Running it

Three terminals. The processes run on the host with hot reload; only
PostgreSQL is containerised.

```bash
# Terminal 1 — control plane on :8000
make dev-server

# Terminal 2 — web client on :5173
make dev-web

# Terminal 3 — agent (optional; Phase 0 only runs its lifecycle)
make agent-run
```

Then open:

| URL | What |
| --- | --- |
| http://localhost:5173 | Web client |
| http://localhost:8000/docs | API documentation (Swagger UI) |
| http://localhost:8000/healthz | Liveness |
| http://localhost:8000/readyz | Readiness, including the database |

The Vite dev server proxies `/api`, `/healthz` and `/readyz` to the backend, so
the browser only ever talks to one origin — matching production, where the API
and the built client are served together. This is deliberate: without the
proxy, development would rely on CORS that production does not use, and the
difference would surface as a production-only bug.

## Quality gates

```bash
make check            # everything CI runs
make check-docs       # doc links, anchors, ADR numbering, roadmap structure
make check-server     # ruff format + ruff check + mypy + pytest
make check-web        # tsc + eslint + vitest
make check-agent      # gofmt + go vet + go test -race
```

Run `make check` before opening a pull request. CI runs the same commands, so a
local pass means a CI pass.

Auto-fix what can be fixed:

```bash
make fmt-server    # ruff format + ruff check --fix
make fmt-web       # eslint --fix
make fmt-agent     # gofmt -w
```

### Tests

```bash
make test-server         # unit tests; no services needed
make test-integration    # needs `make db-up` first
make test-web
make test-agent
```

Backend tests come in two tiers:

- **Unit tests** always run. They point at a deliberately unreachable database
  (port 1), so a test that accidentally opens a connection fails loudly
  instead of silently reaching your real database.
- **Integration tests** are marked `@pytest.mark.integration` and run only when
  `HCC_TEST_DATABASE_URL` is set. They skip with an explicit message
  otherwise. CI always sets it, so they never skip silently where it matters.

## Database work

```bash
make db-up                              # start
make db-down                            # stop, keeping data
make db-reset                           # destroy the volume and rebuild
make db-shell                           # psql
make migrate                            # upgrade head
make migrate-down                       # downgrade -1
make migration m="add hosts table"      # autogenerate a revision
```

### Writing a migration

1. Add or change ORM models.
2. **Import the model's module in `apps/server/hcc/models/__init__.py`.**
   Alembic autogenerate only sees modules reachable from there — miss this and
   it will generate an empty migration, or worse, drop statements.
3. `make migration m="what changed"`.
4. **Read the generated file.** Autogenerate does not detect renames (it emits
   drop + add, losing data), nor enum member changes, nor triggers. It also
   creates PostgreSQL enum types implicitly without dropping them on
   downgrade — see `0002_users` for the corrected pattern, and ADR-013.
5. Verify it round-trips: `make migrate && make migrate-down && make migrate`.
   The second `migrate` is the part that matters: a downgrade leaving an enum
   type or function behind only fails on re-upgrade.
6. Give the revision a readable sequential id (`0003_hosts`) rather than
   Alembic's default hash, matching the existing chain.

Objects Alembic cannot see — triggers, functions — must be created *and*
dropped explicitly in the migration. Their invisibility to autogenerate means
they never cause false drift, so `alembic check` stays clean.

CI enforces that migrations apply to an empty database, are reversible, and
that no model change is missing a migration.

## Documentation

The docs carry the architecture and the security model, and they cross-link
heavily, so they are checked mechanically:

```bash
make check-docs
```

This validates that every relative link resolves, every `#anchor` matches a
real heading, every referenced `ADR-NNN` is defined in `ARCHITECTURE.md`, ADR
numbering is contiguous, and roadmap phases and milestone ids are consistent.
It runs in CI's hygiene job. A broken link silently turns a documented
decision into an undocumented one, which is why this is automated rather than
left to review.

### Where things are documented

| Topic | Document |
| --- | --- |
| System design, ADRs, open questions | [`ARCHITECTURE.md`](ARCHITECTURE.md) — the hub |
| Agent transport, enrollment, config sync | [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md) |
| Module catalogue and hardware caveats | [`MONITORING_MODULES.md`](MONITORING_MODULES.md) |
| Scheduling, DST, drift | [`SCHEDULING.md`](SCHEDULING.md) |
| Retention, downsampling, storage limits | [`DATA_RETENTION.md`](DATA_RETENTION.md) |
| Capabilities, terminal, audit | [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md) |
| Future extension points | [`PLUGINS.md`](PLUGINS.md) |

### Writing an ADR

Architectural decisions go in
[`ARCHITECTURE.md §13`](ARCHITECTURE.md#13-architecture-decision-records), with
the next sequential number, and state the decision, the reasoning, and the
cost.

**If a decision contradicts an existing ADR, say so explicitly.** Amend the
old ADR with an "Amended" or "Reaffirmed" note, or add a new one that names
what it supersedes — ADR-022 superseding the blanket shell-execution exclusion
is the worked example. Silently contradicting an earlier decision leaves the
codebase with two documented answers and no way to tell which is current.

## Project layout

```
apps/
├── server/              FastAPI control plane
│   ├── hcc/
│   │   ├── app.py       create_app() factory, wiring
│   │   ├── config.py    validated environment settings
│   │   ├── errors.py    AppError hierarchy + handlers
│   │   ├── db.py        engine, sessions, DeclarativeBase
│   │   ├── models/      ORM models (users)
│   │   └── api/         health probes + versioned routers
│   ├── alembic/         migration environment
│   └── tests/           unit + integration
└── web/                 React client
    └── src/
        ├── index.css    design tokens — the single source of styling truth
        ├── components/
        │   ├── ui/      design-system primitives
        │   └── layout/  shell, sidebar, navigation
        ├── routes/      one module per page
        ├── mocks/       Phase 0 sample data (deleted in Phase 2)
        └── lib/
agents/linux/            Go agent
deploy/compose.yaml      PostgreSQL for development
docs/                    architecture, vision, roadmap, security, subsystems
scripts/check-docs.sh    documentation link and ADR consistency checks
```

## Conventions

### Everywhere

- **Never swallow an error.** Log it with context, or propagate it. An empty
  `except`/`catch` will be rejected in review, and the linters are configured
  to catch it.
- **Explicit types.** mypy runs in strict mode; TypeScript has
  `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes` on.
- **No credentials in code, ever.** Configuration comes from the environment.
  Only `.env.example` is committed.
- **Unknown is a real state.** Never render missing data as zero or healthy.

### Backend

- Raise an `AppError` subclass from `hcc/errors.py`. Never return an ad-hoc
  error dict — clients depend on one envelope.
- Read configuration only through `hcc.config.Settings`. No `os.environ`
  outside `config.py`.
- Resolve dependencies from `app.state`, not from module globals or
  `get_settings()`. A handler that reads the cached settings singleton would
  see different configuration than `create_app(settings)` was given.
- Annotate every function. `ANN` rules are enabled.
- Dependency annotations in `hcc/api/**` must be importable **at runtime** —
  FastAPI resolves them to build its dependency graph, so a
  `TYPE_CHECKING`-only import breaks route registration. The `TC` lint rules
  are disabled for that package for exactly this reason.
- The same applies to `hcc/models/**`: SQLAlchemy resolves `Mapped[...]`
  annotations at class-definition time to derive column types, so types used
  inside them (`datetime`, `uuid.UUID`) must be imported at runtime too.
- ORM models carry their invariants. Use a `@validates` hook for anything that
  must hold on every assignment (see `User.username` keeping
  `username_canonical` in step), so a violation raises where the bad value was
  set rather than at flush time.
- Never put a secret in a model's `__repr__`. `User.__repr__` omits
  `password_hash` deliberately — these objects reach log lines and exception
  context.

### Frontend

- **Compose the design system; do not restyle.** Use primitives from
  `src/components/ui`. If a new variant is needed, add it to the primitive's
  `cva` definition rather than passing one-off classes at the call site. This
  is what keeps seven pages looking like one product.
- **Never hard-code a colour.** Use semantic token classes (`text-text-muted`,
  `bg-surface-raised`, `text-status-critical`). Tokens are defined once in
  `src/index.css`.
- **Accessibility is not optional.** Interactive elements need accessible
  names; status must never be conveyed by colour alone — pair `StatusDot` with
  a text label, which `StatusBadge` does for you. `jsx-a11y` runs in CI.
- **Mock data is labelled.** Anything rendering from `src/mocks` sits beneath a
  `<MockDataBanner />`. A test enforces this.
- Prefer `type` aliases over `interface`; most prop types are intersections.

### Agent

- **No third-party dependencies** (ADR-011). The standard library covers it. If
  you believe you need a dependency, raise it as a decision first.
- **Never log a secret.** `config.Config` redacts its token in both `String()`
  and `LogValue()`; tests enforce both paths.
- **Honour context cancellation.** Shutdown must not be blocked by in-flight
  work.
- **Tests must pass under `-race`.** The agent's core is a concurrent loop. Use
  the `syncBuffer` helper when a test inspects output written by another
  goroutine — a plain `bytes.Buffer` is a data race, and CI will catch it.

## Troubleshooting

**`make db-up` says no container runtime found.**
Install Docker or `podman-compose`. Check detection with `make help`.

**Backend exits with `Invalid configuration`.**
Working as intended — it names the offending variable. Run `make env`, or
export `HCC_DATABASE_URL`. There is deliberately no default.

**`/readyz` returns 503.**
The database is unreachable. `make db-up`, then check
`podman logs hcc-postgres` (or `docker logs`).

**Integration tests all skip.**
`HCC_TEST_DATABASE_URL` is unset. Use `make test-integration`, which sets it.

**Vite fails with a port conflict.**
`strictPort` is on deliberately: a silent port change would break the proxy and
the backend's configured CORS origin. Free port 5173.

**`npm ci` fails.**
`package.json` and `package-lock.json` have drifted. Run `npm install` and
commit the updated lockfile.

**Go: `go: command not found` after installing a tarball.**
Add it to `PATH`, e.g. `export PATH="$HOME/.local/share/gotoolchain/go/bin:$PATH"`.

## Dependency updates

Versions are pinned: exact versions in `package.json`, compatible-release
ranges (`~=`) in `pyproject.toml`, with the exact resolution in
`package-lock.json`. Dependabot opens grouped weekly PRs
(`.github/dependabot.yml`).

Two constraints to know before upgrading:

- **TypeScript is pinned to 6.0.x.** `typescript-eslint` declares
  `typescript: >=4.8.4 <6.1.0`. Upgrading TypeScript past that silently
  disables type-aware linting, so the two move together — Dependabot groups
  them.
- **ESLint is pinned to 9.x.** `eslint-plugin-jsx-a11y` does not support
  ESLint 10 yet. Accessibility linting is worth more than the newest major;
  revisit when the plugin supports it.
