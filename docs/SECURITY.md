# Security

The security model for Homelab Control Center.

> **Current status (Phase 1, milestone 1.5).** An operator can now
> **initialize a fresh installation and log in**. The initial administrator is
> created by a local command, `hcc bootstrap-admin`
> ([first-run bootstrap](#first-run-bootstrap-implemented)); login, logout,
> server-side sessions, CSRF protection, login throttling, default-deny
> authentication and role authorization are implemented and tested. Agent
> communication, monitoring and remote management remain design only.
>
> Sections marked **(design)** describe what later milestones will build; they
> are documented now because they constrain the architecture that *is* built.
> Sections marked **(implemented)** are in the code today and covered by
> tests.
>
> **This revision expands the designed threat surface.** Remote management is
> now a set of opt-in capabilities including an interactive terminal
> ([the capability section](#remote-management-capabilities)), and some collectors invoke
> external binaries ([the external-binaries section](#external-collector-binaries)). Both are new
> risks relative to read-only monitoring, and both are assessed rather than
> asserted away.

## Threat model

### What is being protected

| Asset | Why it matters |
| --- | --- |
| Docker socket access | Equivalent to **root** on the host |
| Agent credentials | Allow reporting as a host, and receiving its commands |
| User sessions | Allow management actions, and terminal access for an Administrator |
| Log content | Routinely contains tokens, IPs, paths, usernames |
| Notification destination credentials | Ntfy tokens, SMTP passwords, webhook secrets |
| Terminal session recordings | Contain whatever the operator typed, including secrets |
| Host configuration | Reveals which capabilities are enabled, i.e. where the soft targets are |

### Assumed adversaries

1. **An attacker on the local network** who can reach the control plane's
   HTTP port. The authentication endpoints are reachable without a session by
   design, which is why they are throttled and bounded.
2. **A compromised monitored host** — one agent, fully controlled. It must not
   escalate to the fleet.
3. **A malicious or careless authenticated user** with a low-privilege role.
   Now also: an Administrator whose session is stolen, which is the adversary
   the terminal capability's gates exist for.
4. **A compromised dependency** in any of the three components' supply chains.
5. **An attacker with a stolen database dump**, offline. Password hashes are
   Argon2id and session tokens are stored hashed, so neither is replayable.

### Explicitly out of scope

- An attacker with root on the host running the control plane. They own the
  database and the process; no application-level control helps.
- Physical access to hardware.
- Nation-state-grade targeting of a homelab.

---

## Structural properties

These are the load-bearing security decisions. They are enforced by
architecture rather than by configuration, so an operator cannot accidentally
turn them off.

### 1. The agent has no listening socket (implemented)

The agent is always the client. It opens outbound HTTPS connections to the
control plane; the control plane never connects to an agent.

Consequences:

- No inbound firewall rule on monitored hosts.
- No listening port to attack, fingerprint or exploit.
- Works behind NAT, which is the normal homelab case.
- **A compromised control plane cannot reach into hosts.** It can only wait to
  be polled, and what it can answer is bounded by the closed command set
  below.

### 2. The Docker socket is never exposed on the network (design)

Not bound to a port. Not proxied. Not forwarded. Not tunnelled.

The agent opens `/var/run/docker.sock` as a **local Unix socket on its own
host**, and sends results outward. This is the primary reason the agent exists:
without it, the control plane would need remote Docker access, which means
either exposing the socket (catastrophic) or holding root-equivalent
credentials for every host (nearly as bad).

### 3. Management commands come from an enumerated set (implemented as a contract)

`agents/linux/internal/controller.CommandType` enumerates every action an
agent will ever perform. The agent **rejects any command type it does not
recognise**. There is no `exec`, no `run`, no generic passthrough, and no
command type that accepts a caller-supplied command line.

The set has grown beyond the original three Docker commands to cover systemd
and session control; current membership is in
[`REMOTE_MANAGEMENT.md §3`](REMOTE_MANAGEMENT.md#3-command-set). Growing an
enumerated set is not opening it — each member maps to one capability, carries
only an agent-discovered target, and arrives with its authorization rule,
audit entry and tests.

A general-purpose command-execution API remains permanently excluded. An
interactive terminal is now a separate, opt-in capability — see
[the capability section](#remote-management-capabilities) and
[ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion).

### 4. The agent initiates every connection (design)

Previously stated as "commands are polled, not pushed". Command delivery now
uses a WebSocket the **agent dials out** and holds open, because polling
cannot carry an interactive terminal.

The security property is unchanged and is the one that matters: the agent
opens every connection and listens on nothing, so property 1 holds exactly.
See [ADR-018](ARCHITECTURE.md#adr-018-agent-initiated-websocket-replaces-command-polling).

### 5. Management capabilities are default-deny (design)

Every capability on every host starts **off**. A freshly enrolled agent
refuses every command, including from a legitimate Administrator, until a
capability is explicitly enabled for that host.

There is no "enable all" switch and no global capability default that applies
to future hosts, so **adding a host can never silently grant control over
it**.

The agent enforces capabilities from its own applied configuration rather than
trusting the controller to have checked. A compromised controller therefore
cannot exceed the capabilities each host was already granted — which is what
keeps the blast radius of a controller compromise bounded.

### 6. Agent-submitted data is untrusted input (design)

Even from an authenticated agent. A compromised host must not escalate through
a crafted payload.

Concretely:

- **Records are attributed server-side**, from the authenticated credential —
  never from a `hostname` field in the payload. Otherwise one compromised agent
  could inject logs attributed to any other host, and thereby fire or suppress
  another host's notification rules.
- Batch sizes, message lengths and request rates are bounded before parsing.
- Rejected batches return a structured error naming the limit, so the agent
  backs off instead of retrying blindly.

---

## Authentication

Password hashing, login and server-side sessions are all **implemented**, and
the initial administrator is created by the local command described in
[first-run bootstrap](#first-run-bootstrap-implemented). There is deliberately
no self-service registration and no password-reset flow yet.

Two distinct identity types, deliberately not conflated:

| | Users | Agents |
| --- | --- | --- |
| Credential | Password → session | Long-lived enrolment credential |
| Hashing | Argon2id *(implemented)* | Argon2id (stored hashed) |
| Transport | `Secure`, `HttpOnly`, `SameSite=Lax` cookie | `Authorization: Bearer` |
| Scope | Fleet-wide, role-limited | **One host only** |
| May issue commands | Yes, if authorized | No |
| May read others' data | Per role | No |

An agent credential authenticates *one host reporting its own data*. It is not
a user credential with a different shape, and cannot be used to read the fleet
or issue commands. This is what contains adversary #2.

### Sessions and login (implemented)

`hcc/security/sessions.py`, `hcc/security/authentication.py`,
`hcc/api/v1/auth.py`. Endpoints: `POST /api/v1/auth/login`,
`POST /api/v1/auth/logout`, `GET /api/v1/auth/me` — under the version prefix
because ADR-006 versions every product route.

| Property | Choice |
| --- | --- |
| Session state | **Server-side row**, not JWT (ADR-028) |
| Token | 256-bit CSPRNG, URL-safe base64 |
| Storage | **Unsalted SHA-256 of the token**, never the token (ADR-027) |
| Transport | `HttpOnly`, `Secure`\*, `SameSite=Lax`, `Path=/`, no `Domain` |
| Absolute lifetime | 12 h default, configurable, never extended |
| Idle timeout | 1 h default, configurable, `0` disables |
| CSRF | Session-bound double-submit token + `Origin` check (ADR-030) |
| Throttling | Per-address and per-username, **before** verification (ADR-029) |

\* `Secure` is derived from the environment — on in production, off elsewhere
so plain-HTTP local development works. Disabling it in production is
**refused at startup**, because session cookies would then travel in clear.

Properties that hold today, each covered by tests:

- **Failures are indistinguishable.** Wrong password, unknown username and
  inactive account return the identical 401 body. An unknown username is still
  verified against a precomputed dummy hash, so the timing does not separate
  them either. A login that distinguishes these is a username oracle.
- **Throttling precedes verification.** Each attempt costs ~64 MiB and ~50 ms
  by design, so a limiter consulted afterwards would still let an attacker
  spend the server's memory.
- **Concurrent Argon2 operations are capped**, bounding peak memory
  independently of the rate limit.
- **Only token hashes are stored.** A database dump yields nothing replayable.
- **Session fixation is impossible.** The token is generated server-side and
  nothing the client sends influences it; any session presented at login is
  revoked before the new one is issued.
- **Logout revokes server-side.** The row is marked, resolution rejects it, and
  replaying the cleared cookie fails. Cookie clearing is a courtesy, not the
  boundary.
- **Deactivating a user takes effect immediately**, because `is_active` is
  re-checked on every request rather than trusted from login time.
- **Resolution is fail-closed.** Absent, malformed, unknown, expired, idle,
  revoked and inactive-user all return "not authenticated", identically, so a
  caller cannot probe whether a captured token is live.
- **CSRF is enforced in the session dependency**, so an authenticated mutating
  route cannot be added without it.
- **No secret reaches a log.** Passwords, session tokens and CSRF tokens are
  absent from log events, error bodies and `repr`, asserted by capturing
  structured events and checking every key and value.

### First-run bootstrap (implemented)

A fresh installation has no accounts. Creating the first administrator is
therefore the one operation that cannot be authorized by an existing
credential, and how it *is* authorized is a security decision in its own
right.

**It is authorized by local access to the controller's database**, proved by
running a command on the controller host or inside its container:

```
hcc bootstrap-admin --username admin
```

The password is prompted for twice with echo off. For automation it is read
from standard input instead, so it never appears in a process listing:

```
hcc bootstrap-admin --username admin --password-stdin < /run/secrets/admin-password
```

#### Why there is no public initial-admin endpoint

The obvious alternative is to expose `POST /api/v1/setup` while the `users`
table is empty. That hands ownership of the deployment to whoever reaches the
port first, and on a homelab the race is real: the control plane is frequently
reachable on the LAN before the operator has finished configuring it, and the
window is as long as the gap between "container started" and "operator opened
the browser". Losing that race is also invisible — the installation simply
looks as though it was already set up.

An empty table is a statement about the current user list, not proof of
anything. Bootstrap is therefore gated on something a network attacker does
not have: the database credentials. That is the same authority needed to read
every password hash and session token anyway, so requiring it grants nothing
new.

#### The durable invariant

Initialization is recorded as a single row in an `installation` table, and
bootstrap is refused while that row exists.

The row — not the user count — is the invariant, so **bootstrap does not
reopen** when the administrator is deactivated, renamed or deleted. Deriving
it from `SELECT count(*) FROM users` would hand ownership to whoever noticed
that the last account had been removed. The row is written once and nothing in
the application updates or deletes it, and it deliberately carries **no
foreign key** to `users`: a cascade would delete it along with the
administrator and silently reopen the door.

#### Concurrency and replay protection

Two mechanisms, and only the second is load-bearing:

1. A **transaction-scoped advisory lock** (`pg_advisory_xact_lock`) serialises
   attempts across processes and connections, so the loser gets a readable
   "already initialized" error rather than an integrity error. Being
   transaction-scoped, it is released on commit, rollback or crash — an
   interrupted attempt leaves no lock to clean up.
2. The **singleton primary key** on `installation`: `id` is a boolean with
   `CHECK (id)`, so `true` is the only storable value and a second insert
   violates the primary key. This holds even if a writer never takes the lock,
   so correctness does not depend on cooperation.

The administrator row and the installation row are written in **one
transaction**, so there is no state in which an administrator exists but the
installation is unmarked, or vice versa. Ten simultaneous attempts against a
real PostgreSQL produce exactly one administrator, asserted by an integration
test.

#### What the command does and does not expose

| Property | Behaviour |
| --- | --- |
| Password as an argument | **Never.** Command lines are world-readable through `/proc` and land in shell history. |
| Password in the environment | **Never.** `/proc/<pid>/environ` is readable by the same user and is inherited by children. A better option exists, so this one is not offered. |
| Password policy | The existing one (ADR-026), reported in full with every violation at once. |
| Username normalization | The existing one (ADR-014), so `Admin` and `admin` are the same account here as everywhere. |
| Output | Username, canonical username, role, timestamp. No password, no hash, no token. |
| Exit codes | `0` created · `1` runtime failure · `2` usage or input error · `3` already initialized — so an idempotent provisioning script can treat `3` as success. |
| Pre-flight | Database connectivity and the presence of the required tables, so "migrations not applied" is reported as that rather than as a driver error. |

### Reverse proxy and HTTPS requirements

This is not optional. The application speaks plain HTTP and expects a
TLS-terminating reverse proxy in front of it (see
[Transport security](#transport-security) below).

**Without HTTPS, session cookies travel in clear and the session mechanism is
fully compromised** — anyone on the path can read the cookie and impersonate
the user.

#### Required settings

| Requirement | Why |
| --- | --- |
| Terminate TLS at the proxy | `Secure` cookies are only meaningful over HTTPS, and in production the application refuses to disable the flag. |
| Send HSTS from the proxy | The application deliberately does not: only the proxy knows whether HTTPS is actually in use. |
| Serve the client same-origin | Removes CORS from the production path entirely and makes `SameSite=Lax` and the `Origin` check straightforward. |
| Set **`HCC_PUBLIC_ORIGIN`** to the origin browsers use, e.g. `https://hcc.example.com` | The application sees plain HTTP internally. Without this it compares a browser's `https://` origin against its own `http://` view and **rejects every state-changing request** — see below. |
| …**or** set `HCC_TRUSTED_PROXIES` to the proxy's address or network | The alternative: the application then believes `X-Forwarded-Proto` from that proxy and derives the origin per request. |
| Set `HCC_TRUSTED_PROXIES` anyway, for client attribution | Without it every client appears as the proxy and they **share one login-throttle budget**, so one attacker can deny login to everyone. |
| Do not expose the port directly | The authentication endpoints are reachable without a session by design. |

A production deployment must set `HCC_PUBLIC_ORIGIN` or `HCC_TRUSTED_PROXIES`.
**Startup fails with neither**, because the application cannot otherwise tell
which origin browsers use, and the failure it would produce at runtime names
the wrong cause.

#### The Origin check, and the defect it had

For state-changing requests, a present `Origin` must match an allowed one.
Allowed origins are, in order of authority:

1. `HCC_PUBLIC_ORIGIN`, when set. **Authoritative** — the request's own view
   is not consulted at all, and no forwarded header can override it. This is
   the recommended production setting precisely because it depends on nothing
   the client sends.
2. `HCC_CORS_ORIGINS`, for a cross-origin development client.
3. The request's own origin, reconstructed from its scheme and host. The
   scheme comes from `X-Forwarded-Proto` **only** when the peer is listed in
   `HCC_TRUSTED_PROXIES`; otherwise from the transport.

Point 3 is what was wrong before milestone 1.5: the scheme was taken straight
from the ASGI scope, so behind a TLS-terminating proxy the comparison was
`https://host` against `http://host` and **every** state-changing request
failed with `csrf_validation_failed` — in the one topology this document
recommends. Logout was the only such endpoint at the time, which is why it
went unnoticed. The regression suite in
`tests/test_origin_csrf_integration.py` now covers each topology explicitly.

Origins are canonicalised on both sides — scheme and host lower-cased, a
default port dropped — so equivalent spellings compare equal rather than
failing a literal string match. A malformed `Origin`, and the opaque origin
`null` sent by sandboxed and `file://` contexts, are refused.

**An absent `Origin` falls back to the CSRF token alone.** Non-browser clients
send none, and browsers always send one for the cross-site requests this
guards against. A deliberate, documented gap — narrow, but a gap.

#### Client attribution behind a proxy

`X-Forwarded-For` is a request header, so any client can set it. Both naive
readings are unsafe:

* trusting it unconditionally lets a caller rotate its apparent address per
  request and bypass per-address throttling entirely;
* ignoring it behind a real proxy makes every client look like the proxy, so
  all users share one budget and one attacker can exhaust it for everyone.

So it is believed **only when the immediate peer is listed in
`HCC_TRUSTED_PROXIES`**, and the chain is then walked from the **right**,
discarding trusted hops, taking the first untrusted address as the client.
Taking the left-most entry — the common mistake — would attribute the request
to whatever the client wrote.

On single-host Compose the proxy is usually a sibling container on the bridge
network, **not loopback**, which is the case most easily got wrong. The
address actually in use is logged at startup as
`request_attribution_configured`, and a production deployment with no trusted
proxy configured logs a warning naming the consequence.

### CORS (implemented)

Same-origin deployment is the preferred shape and needs **no** CORS
configuration: the control plane serves the web client itself, so there is no
cross-origin request to permit.

`HCC_CORS_ORIGINS` exists for the cross-origin development client, and
`allow_credentials` is on because this API authenticates with cookies. That
combination makes **`*` unsafe**: Starlette answers a wildcard with
credentials by echoing whichever origin asked, so any website could read
authenticated responses. A wildcard is therefore **refused at startup** rather
than silently narrowed — an operator who wrote `*` wants something the
application must not provide. `null` is refused for the same reason.

Entries are validated and canonicalised at startup, so a typo'd origin fails
immediately instead of producing a check that silently never matches.

### Password hashing (implemented)

`hcc/security/password.py`. Argon2id through `argon2-cffi`, the reference
binding to the official C implementation. No cryptography is implemented in
this project. Full rationale in
[ADR-025](ARCHITECTURE.md#adr-025-argon2id-via-argon2-cffi-with-rfc-9106-low-memory-parameters)
and [ADR-026](ARCHITECTURE.md#adr-026-password-policy--permissive-nfc-normalized-never-truncated).

| Property | Value |
| --- | --- |
| Algorithm | Argon2id (RFC 9106 §4 recommendation for passwords) |
| Default cost | 64 MiB memory, 3 passes, 4 lanes — RFC 9106 "low memory" profile |
| Digest / salt | 32 bytes / 16 bytes, random per hash |
| Stored form | Full PHC string, parameters included, 97 characters |
| Floors | 19456 KiB, 2 passes, 1 lane (OWASP minimum), enforced in two layers |

Properties that hold today, each covered by tests:

- **Only hashes are stored.** No plaintext is persisted, and the hash is
  verified to contain no plaintext.
- **A unique random salt per hash**, so two accounts sharing a password are
  not identifiable as such from a database dump.
- **Verification fails closed.** `verify()` returns `bool` and never raises,
  for any input — wrong password, malformed hash, wrong algorithm, or a
  non-string. A caller cannot mistake an exception path for success.
- **A malformed stored hash is reported, not swallowed.** It logs at warning
  level with the exception type only, and returns `False`.
- **No secret reaches a log, exception or `repr`.** Policy errors carry field
  names and limits; the only log statements record exception types and
  argument *types*. Asserted by capturing structured log events and checking
  every key and value.
- **Parameter upgrades are possible without a password reset.** The stored
  hash is self-describing, so `needs_rehash()` identifies a stale one and the
  next successful login can re-hash transparently.
- **Over-long passwords are rejected, never truncated**, so two passwords
  sharing a long prefix can never become the same credential.
- **Hashing is offloaded to a worker thread** by the `*_async` methods, so a
  50 ms CPU-bound operation cannot stall the event loop.

Two consequences, both now addressed:

1. **The login endpoint is rate limited** and concurrent Argon2 operations are
   capped. Each attempt costs ~64 MiB and ~50 ms, so an unthrottled
   unauthenticated endpoint would exhaust memory and CPU. Implemented in
   milestone 1.3 (ADR-029).
2. **Handlers use `hash_async` / `verify_async`.** The synchronous methods are
   for CLI and test use.

### Login throttling (implemented)

Each login attempt costs ~64 MiB and ~50 ms by design, and the endpoint is
reachable without a session, so throttling is consulted **before** password
verification. Three budgets, all of which must allow an attempt:

| Budget | Key | Bounds | Default |
| --- | --- | --- | --- |
| `per_address` | source address | one source, against any account | 20 / 5 min |
| `per_address_username` | source **and** account | one source, against one account | 10 / 5 min |
| `per_username` | account | every source, against one account | 100 / 5 min |

#### Why three, and why the proportions are enforced

The previous two-budget design had a verified defect. With a per-username
budget *lower* than the per-address budget, one attacker exhausted a known
account's budget without exhausting their own, and could sustain it
indefinitely — a remote, unauthenticated, permanent denial of service against
a named administrator, reachable with ten requests every five minutes from a
single host.

The shape above removes it:

* **A single source cannot exhaust the global account budget.** It is cut off
  by `per_address_username` first, and because the budgets are consulted in
  order and a refusal returns immediately, a source that has hit its
  per-account limit stops contributing to the global one. One source can
  therefore spend at most `per_address_username` of `per_username`.
* **Reaching the global ceiling takes many distinct sources** — ten, at the
  defaults, which is the distributed credential-stuffing case the global
  budget exists to catch.
* `hcc/config.py` **enforces `per_username > per_address_username` at
  startup**, so the unsafe relationship cannot be reintroduced by
  configuration.

A successful login clears that source's budgets but **not** the global account
budget: one source succeeding says nothing about what other sources are
attempting against the same account, and clearing it would hand a
credential-stuffing attacker a free reset for the price of one working
password.

#### What is kept, and what is not

Kept: bounded password guessing per source, bounded targeted guessing per
source per account, bounded credential stuffing, a bound on distributed
attacks, bounded Argon2 cost, generic failure responses, and **no permanent
lockout** — every budget is a sliding window that refills, and no state marks
an account as locked.

**Stated plainly, two limitations remain:**

1. **A sufficiently distributed attacker can still degrade availability for
   one account**, in windows, by reaching the global ceiling from many
   sources. That is inherent to any per-account limit; the alternative is no
   global budget, which permits unlimited distributed guessing against a
   known administrator. The ratio between the budgets sets how much
   distribution an attacker must actually achieve.
2. **Counters live in one process's memory.** Several uvicorn workers each
   keep their own, multiplying the effective limit, and a restart clears
   them. Redis was rejected as a second stateful service for one feature
   (ADR-001, ADR-003). **Deploy with one worker** until this is addressed.

### Agent enrolment

1. An admin creates a host and receives a **one-time** enrolment token.
2. The token goes to `/etc/hcc-agent/token`, mode `0600`.
3. The agent exchanges it once for a long-lived credential; the token is marked
   consumed.

Design points:

- **The one-time token is separate from the steady-state credential.** The
  short-lived one is what gets pasted into terminals and lands in shell
  history.
- **Both are stored hashed.** A stolen database dump (adversary #5) yields no
  working credentials.
- **Revocation is immediate and server-side.** No waiting for expiry.
- **The token is never a command-line flag** — command lines are world-readable
  via `/proc`. It comes from a file (preferred) or the environment. Enforced by
  `TestNoFlagAcceptsTheEnrolmentToken`.
- **The token file must not be group- or world-readable.** The agent refuses to
  start otherwise (`config.readTokenFile`), rather than accepting a secret that
  is not a secret. Tested across modes `0644`, `0640`, `0604`, `0666`.

---

## Authentication enforcement (implemented)

**Default-deny at the router boundary** (ADR-031). Product routes are mounted
beneath a router-level authentication dependency in `hcc/api/v1/router.py`, so
a route requires a session unless it is deliberately placed on the separate
public router. Forgetting to protect a route is not possible by omission; it
takes an explicit move.

### The complete public surface

Each path is listed with its reason in `hcc.app.PUBLIC_PATHS`, and a test
asserts the running application's behaviour matches that list **in both
directions** — nothing unlisted is reachable, and nothing listed is
unreachable.

**In production, three paths:**

| Path | Why it has to be public |
| --- | --- |
| `GET /healthz` | Liveness. Probed by orchestrators and container runtimes that have no session and frequently cannot present credentials. |
| `GET /readyz` | Readiness. Same reason. |
| `POST /api/v1/auth/login` | Authentication must be reachable before a session exists. |

**Outside production, three more** — FastAPI's own documentation endpoints,
which are **absent in production** because `create_app` passes `docs_url=None`
and `openapi_url=None` there:

| Path | Note |
| --- | --- |
| `GET /openapi.json` | The schema. Needed by the interactive docs and by client generation. |
| `GET /docs` | Swagger UI. |
| `GET /docs/oauth2-redirect` | Mounted by FastAPI alongside `/docs`. |

> These three were **missing from this inventory until milestone 1.5**. The
> regression scan read the OpenAPI schema, and the schema does not describe
> the endpoints that serve the schema — so they were public outside
> production, unlisted and untested. The scan now walks the real routing
> table, and a test asserts both that they are public outside production and
> that they do not exist inside it.

What protects each of them instead of a session:

- The probes do no work beyond a trivial query and **disclose almost nothing**
  (ADR-032): a status and nothing else. No version, no environment, no
  dependency names. Those moved to `GET /api/v1/info`, which needs a session,
  and to the structured log.
- Login is protected by credentials, by three throttling budgets applied
  **before** any password verification, by a cap on concurrent Argon2
  operations, and by a bounded request body.
- The documentation endpoints are absent in production, which is the only
  protection that matters for them.

Everything else — including `GET /api/v1/info`, which used to be public —
returns `401 not_authenticated` without a valid session.

### The regression test

`tests/test_api_authorization.py` enumerates every operation the application
will actually route, calls each without credentials, and requires a 401 unless
the path is allow-listed.

It is **behavioural rather than introspective** on purpose, so it catches a
route mounted anywhere in the application. Since milestone 1.5 it enumerates
from the **routing table** rather than from the OpenAPI schema, because the
schema has two blind spots that an audit demonstrated:

* a route declared `include_in_schema=False` does not appear in it;
* a **WebSocket** route never appears in it, since `paths` describes only HTTP
  operations.

Both would therefore have been exempt from the guarantee while looking
covered — including the agent, log-tail and terminal sockets of Phases 7, 5
and 11. Three self-checks mount deliberately unprotected routes — a plain one,
a hidden one and a WebSocket — and assert the scan detects each, so it cannot
pass by doing nothing.

### Authentication boundaries for WebSockets

No WebSocket endpoint exists yet. The boundary is specified now because two
are coming, they are **not the same**, and conflating them would be a serious
mistake.

| | Browser socket (Phase 5 log tail, Phase 11 terminal) | Agent socket (Phase 7) |
| --- | --- | --- |
| Identity | A **user**, via the session cookie | A **host**, via its bearer credential |
| Credential | `hcc_session` cookie | `Authorization: Bearer` |
| Scope | Fleet-wide, role-limited | One host only |
| `Origin` check | **Required** | Not applicable — not a browser |
| May issue commands | Yes, if authorized | No. Agents *receive* commands |

An agent credential must never be accepted where a user session is expected,
or a compromised host could act as a user; and a user session must never be
accepted on the agent channel, or a browser could impersonate a host. They are
separate authentication domains, as
[Authentication](#authentication) already says for the HTTP surface.

**Browser sockets are session-authenticated, and `Origin` is mandatory.** A
WebSocket handshake is not subject to CORS, so a browser will open a
cross-origin socket and attach cookies; `Origin` is the only signal that
distinguishes a legitimate handshake from cross-site WebSocket hijacking.
Browsers always send it on a handshake, so requiring it costs a real client
nothing. The double-submit CSRF header **cannot** be sent on a handshake at
all, which is why `Origin` carries the weight here rather than supplementing
a token; how a browser socket presents a CSRF token *after* the handshake is a
Phase 5 decision.

This is already enforced rather than merely planned: the shared session
dependency accepts both HTTP and WebSocket connections, and a test mounts a
WebSocket on the real protected router and asserts that an anonymous
handshake, a handshake with no `Origin`, and a handshake from a foreign origin
are all refused before `accept` — while a legitimate authenticated handshake
succeeds. A companion test mounts a deliberately unprotected socket and
asserts the scan detects it.

## Authorization

The **role axis is implemented**; the **capability axis is an interface with
no implementation**, because per-host capability configuration does not exist
yet. Enforced server-side on every request: the web client is untrusted, and
anything it decides can be forged. Hiding a control in the UI is not a
security measure.

Two axes, both of which must permit an action: the user's **role**, and the
target host's enabled **capability** ([the capability section](#remote-management-capabilities)).
A role grants nothing on a host whose capability is off.

| Role | Read monitoring | Manage containers | Manage units | Edit rules | Terminal | Manage agents/users |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| Viewer | ✓ | | | | | |
| Operator | ✓ | ✓ | ✓ | ✓ | | |
| Administrator | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

### Authentication and authorization are separate checks

Deliberately, with distinct failures:

| Situation | Response | Why |
| --- | --- | --- |
| No session, or an expired, revoked or unknown one | `401 not_authenticated` | Logging in again would help. |
| Account deactivated | `401 not_authenticated` | `is_active` is re-checked on every request, so deactivation invalidates the session itself. |
| Valid session, role too low | `403 insufficient_permissions` | Logging in again would **not** help, so the client must not be prompted to. |

A role denial does not reveal which role would have been sufficient:
enumerating what a higher role could do is information an attacker can use.
The specifics — required role, actual role, user id — go to the server log,
where they are actionable.

### Roles are hierarchical by design

`viewer < operator < administrator`, so a minimum-role test is sufficient and
`require_min_role` is the only role primitive. That is not an accident of the
current matrix: granularity is meant to come from the capability axis, not
from inventing roles (ADR-023). A test asserts the ordering property holds, so
a future non-monotonic permission fails there and gets routed to the
capability axis instead of growing the role set.

### The capability axis fails closed

`hcc/security/authorization.py` defines the full interface — a `Capability`
enum mirroring the seven management capabilities, a per-capability minimum
role, and a `CapabilityResolver` protocol — and the application wires an
`Authorizer` with **no resolver**. A capability check therefore denies, logs
an error, and never approximates an answer. An authorization question that
cannot be answered is answered "no".

Every management action passes through, in order:

1. **Authenticated** — valid session.
2. **Authorized** — role permits this action on this host.
3. **Audited** — written to the append-only audit log **before** dispatch, so
   an action that crashes mid-flight is still recorded.
4. **Validated** — target exists on the target host.
5. **Scoped** — queued for exactly one agent.
6. **Expiring** — carries `expires_at`; the agent refuses stale commands, so
   one queued during a network partition cannot execute hours later against
   changed state.
7. **Reported** — the result completes the audit record. A failed command
   always carries a reason; a failure with no reason is not auditable.

---

## Secrets handling

### Never committed (implemented)

- `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `*.pem`,
  `*.key`, `secrets/`.
- Committed `.env.example` files contain only local-development placeholders
  and an explicit warning.
- `alembic.ini` contains **no** database URL; it comes from the environment.
- CI fails the build if a `.env` file or a private key is committed, or if an
  `.env.example` lacks its warning (`hygiene` job).

The credentials in `deploy/compose.yaml` and `apps/server/.env.example`
(`hcc:hcc_local_dev`) are for a throwaway local container bound to loopback.
They are committed so a fresh checkout works with no setup, and are documented
as never to be reused.

### Never logged (implemented)

- `config.Config.String()` **and** `LogValue()` both redact the agent token.
  Both paths are tested — overriding only `String()` would leave the token
  exposed when logged as a structured attribute.
- `/readyz` reports a dependency failure by **exception type**, not message:
  driver messages can contain the connection string, including the password.
  Tested by `test_readiness_does_not_leak_credentials_in_failure_detail`.
- The catch-all exception handler logs the traceback server-side and returns an
  **opaque** 500. Internal details never reach the client. Tested by
  `test_unexpected_exception_does_not_leak_internals`.

### Browser-exposed configuration (implemented)

Only `VITE_`-prefixed variables reach the client, and they are embedded
verbatim in the bundle. `apps/web/.env.example` states this and warns against
putting any secret there.

---

## Transport security

### Control plane

TLS is terminated by a reverse proxy the operator already runs (Caddy,
Traefik, nginx). The application speaks plain HTTP behind it.

**Why not terminate TLS in the application:** homelab users already have a TLS
story — an internal CA, Let's Encrypt via DNS-01, or Tailscale. Imposing a
certificate mechanism would fight their existing setup and produce a worse
result than the proxy they already operate.

Deploying without a reverse proxy means session cookies travel in cleartext.
Do not do it.

### Agent

HTTPS with full certificate verification by default.

`HCC_AGENT_TLS_SKIP_VERIFY=true` disables verification. It exists for the real
case of a homelab running an internal CA not yet installed on a host. When
enabled, the agent logs a `WARN` on **every start** stating that traffic can be
intercepted and naming the variable to unset — not a one-time notice at
configuration time. Tested by `TestRunWarnsWhenTLSVerificationIsDisabled`.

---

## Application hardening (implemented)

| Control | Where | Rationale |
| --- | --- | --- |
| API docs disabled in production | `hcc/app.py` | Widens attack surface for no operator benefit |
| CORS default-deny | `hcc/config.py` | Production is same-origin; empty origin list adds no middleware |
| Strict settings validation | `hcc/config.py` | `extra="forbid"` rejects an unknown constructor argument. That alone was **not** enough: pydantic-settings silently *ignores* an environment variable matching no field, so a typo'd `HCC_SESSION_LIFETIME_SECOND` left the default in force while the operator believed otherwise — and the environment is the only configuration channel this deployment uses. `unknown_environment_variables` now refuses to start on any unrecognised `HCC_` variable, naming it |
| Immutable settings | `frozen=True` | No runtime mutation of security-relevant configuration |
| Fail-fast startup | `load_settings_or_exit` | Invalid configuration exits with code 2 and a readable diagnostic |
| Uniform error envelope | `hcc/errors.py` | No endpoint leaks a differently shaped error with more detail |
| Request correlation | `hcc/middleware.py` | Every log line carries a request ID; opaque 500s remain traceable |
| `pool_pre_ping` | `hcc/db.py` | Recycles connections a restarted database dropped |
| Bandit lint rules (`S`) | `pyproject.toml` | Flags common insecure patterns in CI |
| Security headers | `hcc/middleware.py` | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` on every response, including error responses. An unhandled 500 is produced outside the middleware stack, so `hcc/errors.py` applies the same set there explicitly — asserted by a test against the real ASGI stack |
| Content-Security-Policy | `hcc/middleware.py` | Production only. A policy strict enough to be worth having breaks the Vite dev server, and one weakened for development is one nobody trusts. `script-src 'self'` with no `unsafe-inline`; `frame-ancestors 'none'`; `object-src 'none'` |
| No HSTS from the application | deliberate | Only the TLS-terminating proxy knows whether HTTPS is in use. Sending it from behind a plain-HTTP proxy would either do nothing or lock a development host out of HTTP |
| No `WWW-Authenticate: Basic` | `hcc/errors.py` | A bare Basic challenge makes browsers pop a native credential dialog over the SPA |
| Cookie `Secure` enforced in production | `hcc/config.py` | Startup fails rather than silently sending session cookies in clear |

### Agent process hardening

The agent runs as a dedicated unprivileged user, **not root**. The reference
systemd unit in [`agents/linux/README.md`](../agents/linux/README.md) applies
`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`RestrictAddressFamilies`, `RestrictNamespaces` and `MemoryDenyWriteExecute`.

**Docker group membership is root-equivalent.** In Phase 6 the agent needs it
to reach the socket. This is documented to the operator plainly rather than
glossed over: adding the agent user to `docker` grants root-equivalent power on
that host.

Each capability requires its own narrow grant, listed per capability in
[`REMOTE_MANAGEMENT.md §6`](REMOTE_MANAGEMENT.md#6-host-privileges). Two rules:

- Grants are **per capability, never blanket**. `systemd.control` uses a polkit
  rule for specific units, not `manage-units`. Raw device access for SMART
  prefers a device ACL over adding the agent user to `disk`.
- Some hardening directives in the reference unit must be relaxed for specific
  capabilities. Each relaxation is documented with what it costs, rather than
  the unit quietly shipping with fewer protections.

Network checks (Phase 9) use `CAP_NET_RAW` for ICMP, not full root.

---

## Remote management capabilities

> Full model: [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md). This is the
> security summary.

Capabilities are independent, per host, and all default to off. Read and
control are always separate; enabling one never implies another.

| Capability | Risk if enabled | Mitigation |
| --- | --- | --- |
| `docker.read` | Container names and log contents exposed | Role-gated; logs follow the log pipeline's rules |
| `docker.control` | Service outage; **root-equivalent via the Docker socket** | Role-gated, audited, per-host; the root-equivalence is stated at the point of enabling |
| `systemd.read` | Unit names reveal what runs on the host | Role-gated |
| `systemd.control` | Can stop `sshd` or the agent itself | Per-unit allow-list, empty by default; compiled-in protective list for `sshd`, `systemd-*`, `dbus`, `network*`; re-authentication |
| `sessions.read` | Session inventory exposed | Role-gated |
| `sessions.control` | Attaching inherits the session's privileges, **including root** | Administrator-adjacent; see open question 5 in [ARCHITECTURE §14](ARCHITECTURE.md#14-open-design-questions) — the control half may not ship |
| `terminal` | Interactive shell access to the host | Four independent gates; see below |

### The terminal capability, assessed honestly

This is the most dangerous thing the product will do. Gates, all required:

1. `terminal` enabled on that specific host.
2. Administrator role — it cannot be granted to an Operator.
3. Re-authentication immediately before the session opens.
4. An active session cookie over TLS.

Boundaries: runs as the agent's **unprivileged** user; no `sudo` or `su` (the
agent user is not in `sudoers`); fixed shell path, not caller-supplied; a
minimal constructed environment that does **not** inherit the agent's own
environment, so the agent credential is not exposed to the session; idle and
absolute timeouts; bounded concurrency; revocable mid-session; multiplexed
over the agent's existing outbound connection with **no new port and no SSH
daemon**.

**Recording has no off switch.** Every session records who, which host, when,
and the full input/output stream. An unrecorded interactive session on a
production host is the accountability gap this platform exists to close.
Recordings are themselves sensitive — they contain whatever was typed — so
they are Administrator-only, have their own retention with a non-zero minimum,
and their deletion is audited.

**Residual risk.** An attacker with a stolen Administrator session who can
re-authenticate gains interactive unprivileged shell access to hosts where the
capability is on. Nothing in the design removes that; the design bounds it.
And where `docker.control` is also enabled, the session can reach the Docker
socket and is therefore effectively root — the UI says so at the point of
enabling, and this combination is the one to think hardest about.

The trade-off being made is not "terminal versus nothing". It is "audited,
unprivileged, opt-in terminal" versus "operators keep separate SSH access this
platform cannot see and which is often root-capable".

## External collector binaries

Some modules — SMART, several RAID controllers, screen/tmux — have no kernel
interface and must invoke `smartctl`, `nvme`, `mdadm`, a vendor CLI, `screen`
or `tmux`.

This sits close to the permanently-excluded "arbitrary command execution", so
the distinction is load-bearing rather than rhetorical. **There is no input
path by which a caller chooses what runs.** Specifically:

| Control | Why it matters |
| --- | --- |
| Compiled-in allow-list of executables | An operator cannot point a module at a different binary |
| Fixed argument vectors | Only an agent-discovered device path is substituted; no user string reaches an argument |
| No shell, ever — `exec` with an argv slice | Removes the quoting and injection surface entirely |
| Read-only subcommands only | The one exception, `smart_selftest`, is a separate module with its own capability |
| Absolute path resolution, never `PATH` | A writable directory earlier in `PATH` cannot hijack the call |
| Timeout, output cap, one invocation per device | Bounds a hung or chatty tool |
| Never root | Raw device access comes from a narrow grant or device ACL, not `sudo` |
| Module-level audit of the resolved `argv` | What ran is recoverable after the fact |

Arbitrary execution means the caller picks the command. This does not. See
[ADR-021](ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list)
and
[`MONITORING_MODULES.md §4.3`](MONITORING_MODULES.md#43-external-binaries--the-one-genuinely-new-risk).

**Residual risk, stated.** Subprocess execution is a larger attack surface
than reading `/proc`. A vulnerability in `smartctl` itself becomes reachable
on hosts where SMART monitoring is enabled — which it would also be for anyone
running `smartctl` from cron, but the platform is now the thing invoking it.
A module that cannot meet every control above does not ship.

## Configuration as an attack surface

Configuration is operator-authored and controller-delivered, which makes it
worth treating as a surface in its own right.

- **Configuration cannot weaken an invariant.** There is no setting that opens
  an inbound port, exposes the Docker socket, disables audit logging, or turns
  the agent into a general command runner. See
  [`PRODUCT_VISION.md §4`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable).
- **Agents validate before applying**, and an invalid document never replaces
  the last known good one — so a bad or tampered configuration cannot silently
  stop monitoring.
- **ACK carries a checksum**, not just a version, so a truncated or modified
  document cannot be acknowledged as applied.
- **Configuration is per host and scoped by credential.** One agent cannot
  fetch another host's configuration, which would otherwise leak the fleet's
  capability map to a single compromised machine.
- **Resource limits are enforced server-side** (interval floors, retention
  ceilings, storage budget), so a configuration cannot be used to exhaust the
  controller's disk. See
  [`DATA_RETENTION.md §6`](DATA_RETENTION.md#6-storage-limits).

## Supply chain

| Component | Dependencies | Mitigation |
| --- | --- | --- |
| Agent | **Zero** third-party (ADR-011) | Entire supply chain is the Go standard library — the strongest guarantee in the system, and it is the component running on monitored hosts |
| Backend | 9 direct | Pinned ranges; grouped Dependabot PRs; `pip-audit` planned |
| Frontend | ~16 direct | Exact pins + lockfile; `npm ci` in CI; `npm audit` reports 0 vulnerabilities |
| CI actions | Pinned by major | Dependabot-updated |

The agent's zero-dependency rule is a security decision, not minimalism for its
own sake: it is the component with elevated privileges on hosts the operator
cares about, so a compromised transitive dependency must not be able to reach
them.

---

## Known gaps

Stated plainly rather than implied. Several of these must close before the
authentication endpoints face an untrusted network.

1. **No self-service registration, and no user management.** The initial
   administrator is created by `hcc bootstrap-admin`
   ([first-run bootstrap](#first-run-bootstrap-implemented)); there is no way
   to create a *second* account yet, and no API for listing, editing or
   deactivating accounts. Milestone 1.7.
2. **Role authorization has no production call site yet.** The dependencies
   are implemented and tested, and every product route requires
   authentication — but no current endpoint needs more than "logged in",
   because user and host management do not exist. The first real use arrives
   with user management.
3. **The capability axis is not implemented.** Its interface exists and fails
   closed, so nothing can accidentally rely on it. Per-host capability
   configuration lands with host management.
4. **Rate limiting is per process.** Several uvicorn workers each keep their
   own counters, multiplying the effective limit by the worker count, and a
   restart clears them. **Deploy with one worker until this is addressed**
   (ADR-029).
5. **A distributed attacker can still degrade login availability for one
   account.** Reaching the global per-account budget requires many distinct
   sources — ten at the defaults — and no single source can do it
   ([login throttling](#login-throttling-implemented)). The residual exposure
   is inherent to having a per-account limit at all; the alternative permits
   unlimited distributed guessing. There is no permanent lockout.
6. **Timing-based user enumeration is narrowed, not eliminated.** An unknown
   username is verified against a dummy hash so the Argon2 cost is paid
   either way, but one database query versus none remains measurable in
   principle. The rate limiter bounds how many samples an attacker can
   collect. Closing it fully would require constant-time work across the whole
   request, which is not achievable in a Python web application and is not
   claimed.
7. **`Origin` is only checked when present, on HTTP.** Absent `Origin` falls
   back to the CSRF token alone, because non-browser clients send none.
   Browsers always send it for the cross-site requests this guards against, so
   the gap is narrow — but it is a gap. It does **not** apply to WebSocket
   handshakes, where `Origin` is required.
8. **No audit log.** Login, logout, revocation and bootstrap are logged as
   structured events, but there is no append-only audit table yet.
   Milestone 1.14.
9. **Sessions are not listed or revocable in the UI.**
   `SessionService.revoke_all_for_user` exists and is tested, but nothing calls
   it. Milestone 10.1.
10. **No password change or reset flow**, so a compromised password cannot be
    rotated by its owner — and the bootstrap command cannot be used a second
    time to replace it. Recovery today means issuing SQL against the database,
    which is the same authority bootstrap required. `rehash_if_needed` exists
    for parameter upgrades but is not yet wired into login.
11. **Session pruning is not scheduled.** `prune_expired` exists and is
    tested; the scheduler arrives with the other periodic jobs. Until then the
    table grows with expired rows.
12. **`pip-audit` and `npm audit` are not yet CI-blocking.**
13. **The end-to-end smoke test is not run in CI.** It is the only gate that
    exercises a real uvicorn server, which is where the reverse-proxy origin
    defect would have been visible.
14. **Nothing in the agent, monitoring, capability or terminal sections
    exists.** They are specified because they constrain what is being built,
    not because any of it is live.

## Reporting a vulnerability

Do **not** open a public issue.

Use GitHub's private vulnerability reporting ("Report a vulnerability" under
the Security tab), or email the maintainer. Please include affected version,
reproduction steps, and impact.

This is a volunteer-maintained project; expect an acknowledgement within a week.
Fixes are released as soon as practical, and credit is given unless you prefer
otherwise.
