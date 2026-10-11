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
├── models/         # ORM models — user.py (see the module docstring)
├── security/       # framework-free auth primitives
│   ├── password.py       # Argon2id hashing, policy, concurrency cap
│   ├── tokens.py         # token generation + SHA-256 storage hashing
│   ├── ratelimit.py      # three-budget login throttling
│   ├── sessions.py       # session lifecycle (DB-aware, HTTP-free)
│   ├── authentication.py # login: throttle, verify, issue
│   ├── authorization.py  # roles + the fail-closed capability interface
│   ├── bootstrap.py      # first-run: one administrator, exactly once
│   ├── forwarded.py      # trusted-proxy client address and scheme
│   └── origins.py        # origin parsing for CSRF and CORS
├── cli/            # operator commands (`hcc`)
│   ├── main.py           # argparse dispatch
│   └── bootstrap.py      # `hcc bootstrap-admin`
└── api/
    ├── deps.py     # dependencies: session, CSRF, Viewer/Operator/AdminDep
    ├── health.py   # /healthz, /readyz — public, minimal disclosure
    └── v1/
        ├── router.py  # the default-deny boundary: public vs protected
        └── auth.py    # login (public), logout + me (protected)
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

## Endpoints (Phase 0)

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

Outside production, FastAPI also serves `/docs`, `/docs/oauth2-redirect` and
`/openapi.json` without a session. All three are **absent in production**.

**Authenticated.**

| Method | Path | Requires | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/info` | session | API and server version, environment. |
| POST | `/api/v1/auth/logout` | session + CSRF | Revoke the session server-side and clear its cookies. |
| GET | `/api/v1/auth/me` | session | The authenticated identity, and the CSRF token. |

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
- A role denial must not reveal the required role. Raise
  `InsufficientPermissionsError` and let the dependency log the specifics.
- Authentication failures must stay indistinguishable. Raise
  `InvalidCredentialsError` for every cause and log the real reason; a login
  that separates "unknown user" from "wrong password" is a username oracle.
