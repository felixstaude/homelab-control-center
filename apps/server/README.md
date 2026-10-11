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
│   ├── ratelimit.py      # in-process login throttling
│   ├── sessions.py       # session lifecycle (DB-aware, HTTP-free)
│   └── authentication.py # login: throttle, verify, issue
└── api/
    ├── deps.py     # shared dependencies incl. CurrentSessionDep + CSRF
    ├── health.py   # /healthz (liveness), /readyz (readiness)
    └── v1/         # versioned product API — auth.py, router.py
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

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/healthz` | — | Liveness. No dependency checks. |
| GET | `/readyz` | — | Readiness. Verifies PostgreSQL. 503 when degraded. |
| GET | `/api/v1/info` | — | API and server version metadata. |
| POST | `/api/v1/auth/login` | — | Authenticate; sets the session and CSRF cookies. |
| POST | `/api/v1/auth/logout` | session + CSRF | Revoke the session server-side and clear its cookies. |
| GET | `/api/v1/auth/me` | session | The authenticated identity, and the CSRF token. |
| GET | `/docs` | — | Swagger UI. Disabled when `HCC_ENVIRONMENT=production`. |

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
- An authenticated endpoint takes `CurrentSessionDep`. That dependency also
  enforces CSRF and `Origin` on state-changing methods, so protection cannot
  be forgotten by adding a route and omitting a separate dependency.
- Authentication failures must stay indistinguishable. Raise
  `InvalidCredentialsError` for every cause and log the real reason; a login
  that separates "unknown user" from "wrong password" is a username oracle.
