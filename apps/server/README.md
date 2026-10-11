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
├── security/       # framework-free auth primitives — password.py (Argon2id)
└── api/
    ├── deps.py     # shared FastAPI dependencies
    ├── health.py   # /healthz (liveness), /readyz (readiness)
    └── v1/         # versioned product API
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

| Method | Path           | Purpose |
| ------ | -------------- | ------- |
| GET    | `/healthz`     | Liveness. No dependency checks. |
| GET    | `/readyz`      | Readiness. Verifies PostgreSQL. 503 when degraded. |
| GET    | `/api/v1/info` | API and server version metadata. |
| GET    | `/docs`        | Swagger UI. Disabled when `HCC_ENVIRONMENT=production`. |

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
