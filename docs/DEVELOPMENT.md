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

One asymmetry remains, and it is why `.env.example` sets `HCC_CORS_ORIGINS`:
the proxy runs with `changeOrigin`, so it rewrites `Host` to the backend's own
address while the browser's `Origin` stays `http://localhost:5173`. The
backend therefore cannot derive the browser's origin from the request and must
be told it. In production the client is served from the same origin as the API
and no CORS configuration is needed at all. Both directions are covered by
`TestViteDevProxyTopology` in
`apps/server/tests/test_browser_contract_integration.py`.

### Signing in

With all three terminals up, open http://localhost:5173:

- **No administrator yet** → the first-run screen appears and shows the
  `hcc bootstrap-admin` command to run. It performs no bootstrap itself; a
  browser flow that could would be exactly the endpoint
  [ADR-033](ARCHITECTURE.md#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint)
  refuses to create. Run the command in another terminal and press **Check
  again** — no reload needed.
- **Administrator exists** → the login page appears. Sign in with the
  credentials from `make bootstrap`.
- **Backend not running** → a "cannot reach the control plane" screen with a
  retry button, *not* a login page. An unreachable backend is deliberately not
  reported as a signed-out session.

After signing in, the account menu at the bottom of the sidebar shows your
username and role and holds **Sign out**. Deep links work: visiting
`/containers` while signed out takes you to the login page and returns you to
`/containers` afterwards.

The dashboard pages themselves are still static fixtures behind a banner —
milestone 1.6 wired up authentication, not monitoring data.

## Quality gates

```bash
make check            # everything CI runs
make check-docs       # doc links, anchors, ADR numbering, roadmap structure
make check-server     # ruff format + ruff check + mypy + pytest
make check-web        # tsc + eslint + vitest
make check-agent      # gofmt + go vet + go test -race
```

Run `make check` before opening a pull request. Two gates are **not** in
`make check` because they need a running PostgreSQL, and CI runs them as
separate jobs:

```bash
make smoke            # a real uvicorn process, probed over HTTP
make e2e              # a real browser against a real control plane
```

**There is no Prettier configuration, and Prettier is not the formatter.**
Frontend style is whatever ESLint accepts; running `npx prettier --write`
will rewrite quotes and line breaks across a file and produce a diff that is
almost entirely noise.

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
make test-web            # vitest
make test-agent
make e2e                 # Playwright, real browser against a real backend
```

Backend tests come in two tiers:

- **Unit tests** always run. They point at a deliberately unreachable database
  (port 1), so a test that accidentally opens a connection fails loudly
  instead of silently reaching your real database.
- **Integration tests** are marked `@pytest.mark.integration` and run only when
  `HCC_TEST_DATABASE_URL` is set. They skip with an explicit message
  otherwise. CI always sets it, so they never skip silently where it matters.

#### Browser end-to-end tests

```bash
make e2e                                  # the whole suite
./scripts/e2e.sh e2e/session.spec.ts      # one file
./scripts/e2e.sh -g 'expired session'     # one test
./scripts/e2e.sh --headed                 # watch it happen
```

Arguments are forwarded to Playwright. Four suites run serially against one
installation:

| Suite | What it covers |
| --- | --- |
| `auth-journey.spec.ts` | First run, bootstrap, sign in, navigate, reload, sign out |
| `responsive.spec.ts` | The same screens at 1920×1080, 1440×900, 768×1024 and 390×844 — overflow, target sizes, reachable navigation, layout shift, keyboard order, password-manager compatibility |
| `security.spec.ts` | Session fixation, login CSRF from a foreign origin, borrowed CSRF tokens, rejected `Origin` shapes, CORS, back-button disclosure, enumeration, the live routed surface |
| `session.spec.ts` | Expiry, server-side revocation, forged cookies, client restart, 401 versus 403, an unreachable backend and recovery, real throttling |


`scripts/e2e.sh` owns the whole fixture, so there is nothing to set up and
nothing to clean up:

1. Creates a **disposable** database with a unique name and applies migrations.
   **It never touches your development database** — it refuses to proceed if
   the name it generated already exists.
2. Creates an administrator with the real `hcc bootstrap-admin` command, so
   what the tests sign in with is produced the same way a real deployment's
   first account is.
3. Starts a real uvicorn control plane and serves the **production** bundle
   with `vite preview`, so the tests run against the built artefact rather than
   the dev server.
4. Runs Playwright headless, then drops the database and stops both processes
   even on failure.

Playwright itself starts nothing — the script owns the lifecycle, so the suite
behaves identically locally and in CI. Logs from a failed run land in
`/tmp/hcc-e2e-*.log` and CI uploads them as an artefact.

The script shortens the login throttle *window* to two seconds while leaving
the budgets at their production defaults, so the throttling test spends a real
budget and then waits a real window out instead of leaving every later test
locked out for five minutes.

First run needs the browser once:

```bash
cd apps/web && npx playwright install --with-deps chromium-headless-shell
```

#### API contract drift

```bash
make openapi          # regenerate apps/web/src/api/openapi.snapshot.json
make check-contract   # fail if the committed snapshot is stale
```

The frontend's API types are hand-written and asserted against that committed
snapshot of the backend's real OpenAPI document
([ADR-037](ARCHITECTURE.md#adr-037-the-clients-api-types-are-hand-written-and-validated-against-the-real-schema)).
**Change a response model on the server and the frontend test suite fails**,
which is the point — hand-written types otherwise validate the client against
its own belief about the server and break only at runtime. Run `make openapi`
and commit the diff as part of the same change. Full generation is milestone
1.15.

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
        ├── api/         the HTTP boundary: client, errors, contract types
        ├── auth/        authentication state, route guards, permissions
        ├── routes/      one module per page, plus login and setup
        ├── mocks/       Phase 0 sample data (deleted in Phase 3)
        ├── test/        fake backend + renderApp helpers
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
- **All HTTP goes through `apiRequest`** in `src/api/client.ts`. Never call
  `fetch` from a component: credentials, CSRF, timeouts and error shape are
  decided once, and a second call path would get one of them wrong.
- **Never put anything about the session in `localStorage` or
  `sessionStorage`.** The session cookie is `HttpOnly`, which is what stops an
  XSS bug from becoming a stolen session; mirroring it into readable storage
  undoes that.
- **Branch on the error kind, not the status code.** `isSessionExpired` is 401
  only — a 403 is an authorization failure and must not send the user to the
  login page.
- **Never treat an unreachable backend as signed out.** `unavailable` is its
  own authentication state for this reason.
- **The client is not an authorization boundary.** Permission helpers decide
  what the interface offers; the server decides what happens. An unrecognised
  role therefore falls back to the smallest interface rather than throwing.
- **Add a query key to `lib/query-keys.ts`.** Inline key arrays drift, and a
  key that sign-out does not know about survives it.
- Protect a page by **where you mount it** — inside `<RequireAuth>` in
  `App.tsx` — not with a check inside the component.

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

**The browser shows the first-run screen but I already have an account.**
The account exists but the `installation` row does not — a database created
before migration `0004` and populated by hand. `make bootstrap` will report
exit code `0` or `3`; if it creates an administrator, the row is now there.

**The frontend test suite fails on `openapi.test.ts`.**
A backend response model changed. That is the drift check working. Run
`make openapi` and commit the regenerated snapshot with your change.

**`make e2e` fails immediately with a database error.**
It needs PostgreSQL running (`make db-up`) — it creates its own disposable
database on that server and never touches yours.

**A browser test fails that has nothing to do with my change.**
The suites run serially against one installation and several sign in and out
of the same account, so selecting a subset can skip something a later file
expects. Every suite bootstraps an administrator itself if one is missing, so
running one file is safe — but `auth-journey.spec.ts` is the only one that can
observe the uninitialised state, and only on a fresh database.

**The backend starts but the frontend shows "cannot reach the control plane".**
Check the backend is on :8000 and that `HCC_CORS_ORIGINS` includes
`http://localhost:5173`; `make dev-server` sets it. The dev proxy rewrites
`Host`, so the backend cannot infer the browser's origin and has to be told.

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
