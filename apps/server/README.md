# Control Plane (`apps/server`)

FastAPI control plane for Homelab Control Center. Owns all persistent state and
is the only component that talks to the database.

See [`docs/DEVELOPMENT.md`](../../docs/DEVELOPMENT.md) for the full setup guide
and [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) for how this fits into
the system.

## Layout

```
hcc/
├── app.py          # create_app() factory, middleware + router wiring, lifespan
├── main.py         # process entrypoint (uvicorn)
├── config.py       # environment-backed, validated Settings
├── logging.py      # structlog + stdlib bridge
├── errors.py       # AppError hierarchy, uniform error envelope, handlers
├── middleware.py   # request id correlation + access logging
├── db.py           # engine, session factory, DeclarativeBase
├── models/         # ORM models — read the module docstrings
│   ├── user.py           # accounts; canonical username, display name
│   ├── session.py        # server-side sessions
│   ├── installation.py   # the one-row first-run invariant
│   ├── audit.py          # append-only audit events
│   └── host.py           # hosts, enrolment credentials, agent credentials
├── agents/         # the agent-facing half: a different kind of caller
│   ├── protocol.py       # the wire format, versioned and bounded
│   └── registry.py       # which sockets *this process* holds (ADR-046)
├── services/       # business rules: invariants, refusals, orchestration
│   ├── users.py          # user administration + the last-admin invariant
│   ├── account.py        # self-service: password change, own sessions
│   ├── audit.py          # the audit read side, with keyset pagination
│   ├── hosts.py          # host administration + enrolment issuance
│   └── enrollment.py     # the agent side: spending a credential, connecting
├── security/       # framework-free auth primitives
│   ├── password.py       # Argon2id hashing, policy, concurrency cap
│   ├── tokens.py         # token generation + SHA-256 storage hashing
│   ├── ratelimit.py      # three-budget login throttling
│   ├── sessions.py       # session lifecycle (DB-aware, HTTP-free)
│   ├── authentication.py # login: throttle, verify, issue
│   ├── authorization.py  # roles + the fail-closed capability interface
│   ├── bootstrap.py      # first-run: one administrator, exactly once
│   ├── audit.py          # the audit write side, and denial records
│   ├── forwarded.py      # trusted-proxy client address and scheme
│   └── origins.py        # origin parsing for CSRF and CORS
├── cli/            # operator commands (`hcc`)
│   ├── main.py           # argparse dispatch
│   └── bootstrap.py      # `hcc bootstrap-admin`
└── api/
    ├── deps.py     # dependencies: session, CSRF, Viewer/Operator/AdminDep
    ├── health.py   # /healthz, /readyz — public, minimal disclosure
    └── v1/
        ├── router.py   # the boundary: public, protected, administrator
        ├── auth.py     # login (public), logout + me (protected)
        ├── setup.py    # setup status (public, one bit)
        ├── users.py    # user administration (administrator)
        ├── account.py  # self-service account and sessions (any role)
        ├── audit.py    # the audit trail (administrator)
        ├── hosts.py    # the host registry: reads any role, writes admin
        └── agents.py   # enrolment + the agent WebSocket (per-host credential)
alembic/            # migration environment + versions
tests/              # unit tests (always run) + integration tests (need Postgres)
```

## Quick start

From the repository root:

```bash
make install-server    # create .venv, install with dev extras
make db-up             # start PostgreSQL
make migrate           # apply migrations
make dev-server        # http://127.0.0.1:8000
```

Configuration comes from the environment; copy `.env.example` to `.env` first.

## Creating the initial administrator

A fresh installation has no accounts, and there is deliberately no HTTP
endpoint that creates the first one (ADR-033). Run this on the controller host
or inside its container, where the database credentials already are:

```bash
hcc bootstrap-admin --username admin
```

The password is prompted for twice with echo off, and is **never** accepted as
an argument or an environment variable. For automation:

```bash
hcc bootstrap-admin --username admin --password-stdin < /run/secrets/admin-password
```

Bootstrap runs **once per installation** and does not reopen — not even if
every account is later deleted. Exit codes: `0` created, `1` runtime failure,
`2` usage or input error, `3` already initialized.

## Endpoints

Authentication is **default-deny**: every route requires a session unless it
appears in the public list below. See
[`docs/SECURITY.md`](../../docs/SECURITY.md#authentication-enforcement-implemented).

**Public — the complete list.**

| Method | Path | Why it has to be public |
| --- | --- | --- |
| GET | `/healthz` | Liveness. Orchestrators have no session. Returns `{"status": "ok"}` and nothing more. |
| GET | `/readyz` | Readiness. Same reason. Returns an aggregate status; 503 when degraded. |
| POST | `/api/v1/auth/login` | Authentication precedes any session. Throttled before password verification. |
| GET | `/api/v1/setup/status` | The first screen a browser can reach must work with no credential. One boolean (ADR-036). |

Outside production, FastAPI also serves `/docs`, `/docs/oauth2-redirect` and
`/openapi.json` without a session. All three are **absent in production**.

**Authenticated, any role.**

| Method | Path | Requires | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/info` | session | API and server version, environment. |
| POST | `/api/v1/auth/logout` | session + CSRF | Revoke the session server-side and clear its cookies. |
| GET | `/api/v1/auth/me` | session | The authenticated identity, and the CSRF token. |
| GET | `/api/v1/account` | session | The caller's own account. No CSRF token — that is `/auth/me`'s job. |
| GET | `/api/v1/account/sessions` | session | The caller's own live sessions, with the current one flagged. |
| POST | `/api/v1/account/sessions/revoke-others` | session + CSRF | End every session except this one. |
| POST | `/api/v1/account/sessions/{id}/revoke` | session + CSRF | End one of the caller's own. 404 for anyone else's. |
| POST | `/api/v1/account/password` | session + CSRF | Change the caller's own password. Revokes every other session and rotates this one (ADR-040). |

These need no role because each is scoped to the caller by construction: the
user id comes from the resolved session, and no request field names an
account.

**Administrator only.** Gated on the router, not per handler, so a route added
to `users.py` or `audit.py` is privileged by placement. Denials here are
written to the audit trail.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/users` | List accounts. Search, role and status filters; `limit` ≤ 100, `offset` ≤ 1,000,000; live-session count per account. Two queries per page. |
| POST | `/api/v1/users` | Create an account. Role defaults to `viewer`. 409 `username_taken` for a case-insensitive duplicate. |
| GET | `/api/v1/users/{id}` | One account. |
| PATCH | `/api/v1/users/{id}` | Display name, role, status. Partial; `display_name: null` clears it. **No `username` field** — usernames are immutable (ADR-041). |
| POST | `/api/v1/users/{id}/deactivate` | End access, keeping the row. Revokes live sessions. |
| POST | `/api/v1/users/{id}/reactivate` | Restore the ability to sign in. Does **not** restore revoked sessions. |
| POST | `/api/v1/users/{id}/sessions/revoke` | End every live session. Reports the count; 404 for an unknown account. |
| GET | `/api/v1/audit/events` | The audit trail, newest first, keyset-paginated. |
| GET | `/api/v1/audit/actions` | The action vocabulary this build writes. |
| POST | `/api/v1/hosts` | Register a host. Creates the record only — **no credential is minted**, so a mistyped name does not leave a live secret. 409 `host_name_taken` for a case-insensitive duplicate. |
| PATCH | `/api/v1/hosts/{id}` | Name, description, environment. Labels only: no field here can set the lifecycle, the administrative state or any connectivity column. |
| POST | `/api/v1/hosts/{id}/disable` | Refuse this host's agent at the door, keeping its enrolment. Disconnects a live agent in milliseconds. |
| POST | `/api/v1/hosts/{id}/enable` | Allow it to connect again. Reconnects nothing itself — the controller never dials out (ADR-018). |
| POST | `/api/v1/hosts/{id}/enrollment` | Mint a one-time credential. **The only response in this API that contains a secret**, and the only time it exists outside the agent's filesystem. Revokes any pending one. 409 `host_already_enrolled` without `allow_reenrollment`. |
| POST | `/api/v1/hosts/{id}/enrollment/revoke` | Kill an unused credential. Reports the count, so "nothing was pending" stays distinguishable from "one was revoked". |
| POST | `/api/v1/hosts/{id}/credential/revoke` | Withdraw trust from the agent. 409 `no_live_credential` rather than a cheerful success, because "revoked" and "there was nothing to revoke" are different answers. |

**Any authenticated role** reads the fleet, and that split is the product
decision rather than an oversight: a Viewer who cannot list hosts cannot use a
monitoring product, and an Operator who could mint enrolment credentials could
enroll a machine of their own and feed the installation fabricated metrics.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/hosts` | List hosts. Name, lifecycle and status filters; `limit` ≤ 100. The `connectivity` filter is applied **within** the page, because connectivity is derived rather than stored ([ADR-043](../../docs/ARCHITECTURE.md#adr-043-connectivity-is-derived-never-stored-as-a-boolean)) and a SQL predicate would be a second copy of that rule. |
| GET | `/api/v1/hosts/{id}` | One host. |

**Agent-facing**, authenticated by a **per-host credential** rather than a
session — a fourth router, because neither credential kind may stand in for
the other.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/agents/enroll` | Spend a one-time credential for a durable one. 200, not 201: nothing addressable was created, and there is no endpoint that returns the credential again. |
| WEBSOCKET | `/api/v1/agents/connect` | The long-lived outbound connection. Refused *before* `accept` when unauthenticated, so no socket is ever opened for an anonymous caller. |

Demoting or deactivating the **last active administrator** is refused with
409 `last_administrator`, held by a transaction-scoped advisory lock rather
than a count-then-update (ADR-039). There is deliberately no delete endpoint
and no administrator-set-password endpoint — see
[`docs/SECURITY.md`](../../docs/SECURITY.md#user-administration-implemented).

### Behind a reverse proxy

Set **`HCC_PUBLIC_ORIGIN`** to the origin browsers use
(`https://hcc.example.com`), or `HCC_TRUSTED_PROXIES` to the proxy's address
or network. A production deployment must set one, and startup fails with
neither: the application otherwise compares a browser's `https://` origin
against its own internal `http://` view and rejects every state-changing
request with a misleading CSRF error.

`HCC_TRUSTED_PROXIES` is also what makes per-address login throttling work —
without it every client appears as the proxy and they share one budget. See
[`docs/SECURITY.md`](../../docs/SECURITY.md#reverse-proxy-and-https-requirements).

### Authentication

```
POST /api/v1/auth/login
  {"username": "...", "password": "..."}
  -> 200  + Set-Cookie: hcc_session (HttpOnly, Secure*, SameSite=Lax)
          + Set-Cookie: hcc_csrf    (readable, Secure*, SameSite=Lax)
          {"user_id", "username", "role", "is_active",
           "session_expires_at", "csrf_token"}
  -> 401  invalid_credentials   (identical for wrong password, unknown
                                 username and inactive account)
  -> 429  rate_limited          + Retry-After
  -> 422  validation_failed     (malformed body)

GET  /api/v1/auth/me
  -> 200  the same body as login, for a client recovering state after a reload
  -> 401  not_authenticated     (identical for absent, malformed, unknown,
                                 expired, idle, revoked, or inactive user)

POST /api/v1/auth/logout
  X-CSRF-Token: <csrf_token>
  -> 200  {"status": "logged_out"}
  -> 401  not_authenticated
  -> 403  csrf_validation_failed
```

Every state-changing authenticated request must send the session's CSRF token
in `X-CSRF-Token`. Get it from the login response, from `/auth/me`, or from
the readable `hcc_csrf` cookie.

\* `Secure` is on in production and off elsewhere, so plain-HTTP local
development works. Full model in
[`docs/SECURITY.md`](../../docs/SECURITY.md#sessions-and-login-implemented).

## Tests

```bash
make test-server         # unit tests, no services required
make test-integration    # requires Postgres + HCC_TEST_DATABASE_URL
```

Integration tests skip with an explicit message when `HCC_TEST_DATABASE_URL` is
unset. CI always sets it, so they never skip silently there.

Some properties here can only be demonstrated with **real PostgreSQL and real
concurrency**, and the relevant suites open genuinely separate sessions rather
than reusing one:

| Suite | What only a real database can show |
| --- | --- |
| `test_last_admin_integration.py` | Two administrators demoting each other both read a safe count under READ COMMITTED, because neither sees the other's uncommitted write. One connection cannot reproduce it, and a mocked repository would return whatever the test said. |
| `test_api_account_integration.py` | Two simultaneous password changes verifying against the same hash, and the `FOR UPDATE` that stops them. |
| `test_security_user_management_integration.py` | That the listing stays at four queries with twenty-one accounts — counted by instrumenting the engine, because one query and twenty-six look identical in a response. |
| `test_migrations_integration.py` | That the migration chain applies, reverses and reapplies, and that `alembic check` finds no drift. |

## Conventions

- Every error returns the envelope defined in `hcc/errors.py`. Raise an
  `AppError` subclass; never return an ad-hoc error dict.
- Errors are logged before being converted to a response. Nothing is swallowed.
- All settings are validated at startup. No `os.environ` reads outside
  `config.py`.
- New ORM model modules must be imported in `hcc/models/__init__.py`, or
  Alembic autogenerate will not see them.
- Types used inside `Mapped[...]` must be imported at runtime, not under
  `TYPE_CHECKING`: SQLAlchemy resolves those annotations to derive column
  types.
- Model invariants belong in `@validates` hooks so they fail at assignment
  rather than at flush. Secrets must never appear in `__repr__`.
- `hcc/security/**` must stay free of FastAPI and HTTP concerns, so it is
  callable from a handler, a CLI command or a test alike. A test asserts the
  package imports without pulling in `fastapi` or `starlette`.
- Password hashing goes through `hcc.security.password`. Use the `*_async`
  methods from request handlers: a hash costs ~50 ms of CPU and would
  otherwise block the event loop.
- **Mount new routers on the protected router** in `hcc/api/v1/router.py`.
  Authentication comes from a router-level dependency, so a route is protected
  by default and public only by being deliberately moved. Making something
  public also means adding it to `hcc.app.PUBLIC_PATHS` with a reason, and to
  the expected set in `tests/test_api_authorization.py` — both deliberately,
  because a silently growing public surface is the failure being guarded
  against.
- An authenticated endpoint takes `CurrentSessionDep`, or a role dependency
  (`ViewerDep`, `OperatorDep`, `AdminDep`) which returns the same session.
  FastAPI caches dependencies per request, so the session is resolved once
  either way. The session dependency also enforces CSRF and `Origin` on
  state-changing methods.
- Role checks use `require_min_role`; roles are hierarchical. If a permission
  is not monotonic in `viewer < operator < administrator`, it belongs on the
  capability axis rather than becoming a new role (ADR-023).
- **Business rules go in `hcc/services/`, not in a handler.** "Deactivating
  the last active administrator is refused" is neither a request validation
  nor a constraint PostgreSQL can express, and in a route it would live
  wherever the first caller happened to be. Services take an `AsyncSession`
  and plain values, raise typed exceptions, and import no FastAPI.
- **Services do not commit.** The handler does, so a mutation and the audit
  event describing it land together or not at all. The one exception is
  recording a *refusal*, which by definition has nothing else to commit with
  and is documented where it happens.
- **Every operation that can reduce the set of active administrators takes the
  advisory lock in `hcc/services/users.py` before it reads.** A count followed
  by an update is wrong in a way a single connection cannot show (ADR-039).
- **A locked read needs `populate_existing`.** `SELECT … FOR UPDATE` acquires
  the lock, but SQLAlchemy returns the identity-map copy of a row this session
  has already loaded — and session resolution has loaded the user row on every
  authenticated request. Without it the lock is held around stale data, which
  looks correct in every single-request test.
- **Request models set `extra="forbid"`** and are translated into explicit
  field sets, so an unknown or internal field is *rejected* rather than
  silently dropped. A client that believed it set `password_hash` finds out.
- **Response models are allow-lists, not serialised rows.** `password_hash` is
  not excluded from a user response; it is absent from a model that was never
  given it.
- **Audit events are written through `hcc/security/audit.py`**, which builds
  `details` from named fields. There is deliberately no parameter a caller
  could fill with a request body — that is how credential material reaches an
  audit log in practice.
- A role denial must not reveal the required role. Raise
  `InsufficientPermissionsError` and let the dependency log the specifics.
- Authentication failures must stay indistinguishable. Raise
  `InvalidCredentialsError` for every cause and log the real reason; a login
  that separates "unknown user" from "wrong password" is a username oracle.
