"""Agent-facing endpoints: enrollment, and the authenticated connection.

Phase 1, milestone 1.8.

## Paths

    POST      /api/v1/agents/enroll
    WEBSOCKET /api/v1/agents/connect

## Neither of these is a browser endpoint, and neither is public

They sit on their own router, mounted beside ``_public``, ``_protected`` and
``_admin`` rather than inside any of them, because they are authenticated by a
**different kind of credential**: a per-host secret, not a session.

That is not a loophole in the default-deny arrangement (ADR-031) — it is the
arrangement applied honestly. Mounting these under ``_protected`` would require
a browser session, which an agent on a Debian box does not have and must never
be given. Mounting them under ``_public`` would mean "no credential required",
which is false. So they carry a router-level dependency of their own that
resolves an agent credential and refuses without one, and the default-deny
scan sees a 401 exactly as it does for the session-gated routes.

Three rules hold across both, and each is an explicit non-negotiable from
``docs/AGENT_ARCHITECTURE.md``:

* **A browser session is never accepted here.** The dependency reads one
  header and nothing else; a session cookie on an agent request is ignored, so
  a stolen cookie cannot be replayed as an agent and a stolen agent credential
  cannot be replayed as a user.
* **The credential is never in the URL.** It arrives in ``Authorization:
  Bearer``, on the WebSocket handshake as well as the HTTP call. A query
  parameter would put a long-lived secret in the access log of every reverse
  proxy between the agent and the controller, and in the controller's own.
* **The host's identity comes from the credential.** There is no ``host_id``
  parameter on either endpoint and no such field in the protocol, so an agent
  has nothing to claim to be. That is the server-side attribution rule
  (``docs/ARCHITECTURE.md`` §7.3) expressed as an absence rather than as a
  check that could be forgotten.

## Why the handshake is refused before ``accept``

An unauthenticated handshake gets a **denial response** — a real HTTP 401 with
the standard error envelope — rather than being accepted and then closed with a
reason frame. Two reasons, and the first is the one that decided it:

1. A socket that reaches ``accept`` is open, however briefly. The
   default-deny regression test treats an accepted anonymous handshake as a
   failure, and it is right to: "accepted, then closed" is one bug away from
   "accepted".
2. An agent's WebSocket client surfaces a handshake rejection as an HTTP
   status, which is a perfectly good thing to log. It does not need a frame to
   understand 401.

A refusal that happens *after* a valid credential has been presented — a
disabled host — is different, and is handled the other way: the socket is
accepted and a :class:`~hcc.agents.protocol.Close` frame explains why. There is
nothing left to withhold from a caller that has already proved it holds that
host's credential, and the agent genuinely needs to tell "my credential is
wrong" apart from "the operator switched this host off".
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Depends, WebSocket, status
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from hcc.agents.protocol import (
    FRAME_BURST,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    MAX_FRAMES_PER_SECOND,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Close,
    CloseReason,
    HeartbeatAck,
    ProtocolError,
    parse_agent_frame,
)
from hcc.agents.registry import ConnectionRegistry, LiveConnection
from hcc.api.deps import SessionDep, get_database, request_id_of

# Imported at runtime, not under TYPE_CHECKING: FastAPI resolves endpoint
# annotations with `get_type_hints`, so a type that exists only for the type
# checker is unresolvable at route-construction time — and the failure is
# quiet rather than loud. `Database` behind a `Depends` was read as a *query
# parameter* called "database", so the socket refused every handshake with
# "field required" instead of authenticating it. Same constraint as
# hcc/api/deps.py, and the same trap.
from hcc.db import Database
from hcc.errors import AgentCredentialRejectedError, AppError, NotAuthenticatedError
from hcc.logging import get_logger
from hcc.services.enrollment import (
    AgentAuthenticationError,
    AgentEnrollmentService,
    EnrollmentRefusedError,
)

if TYPE_CHECKING:
    from hcc.services.enrollment import AuthenticatedAgent

logger = get_logger(__name__)

#: Mounted by :mod:`hcc.api.v1.router` on its own router, which carries the
#: agent-credential dependency. Nothing here may be reached with a browser
#: session, and nothing here is public.
router = APIRouter(prefix="/agents", tags=["agents"])

#: The scheme the credential header must use. Checked case-insensitively,
#: because HTTP header values are not required to preserve case and an agent
#: written against a different library should not fail on ``bearer``.
BEARER_PREFIX: Final = "bearer "

#: Longest ``Authorization`` value read before it is rejected. A credential is
#: a fixed 43 characters; the allowance is for the scheme and whitespace, and
#: the bound is what stops a megabyte of header doing any work.
MAX_AUTHORIZATION_LENGTH: Final = 256

#: How often an open connection's authorization is re-checked against the
#: database. An administrator who revokes a credential expects the agent cut
#: off now, not at its next reconnection — and the request that revoked it may
#: have been served by a different process, so there is nothing to notify.
#: One indexed read per connection per interval; see
#: :meth:`hcc.services.enrollment.AgentEnrollmentService.is_still_authorized`.
AUTHORIZATION_RECHECK_SECONDS: Final = 15


# ---------------------------------------------------------------------------
# The agent credential dependency
# ---------------------------------------------------------------------------


def agent_credential(connection: HTTPConnection) -> str:
    """Extract the presented credential from ``Authorization: Bearer``.

    Takes :class:`~starlette.requests.HTTPConnection`, the common base of
    ``Request`` and ``WebSocket``, so one dependency is correct on both the
    enrollment call and the socket handshake. A dependency annotated
    ``Request`` raises ``TypeError`` on a WebSocket route — see
    :mod:`hcc.api.deps`.

    **This raising before body validation is load-bearing**, and it is why the
    credential is a header rather than a body field. A FastAPI dependency that
    raises short-circuits the request, so a credential-less ``POST`` to the
    enrollment endpoint is a 401 rather than a 422 about a missing field. That
    is both the correct answer and what keeps the endpoint inside the
    default-deny guarantee without an entry in the public allow-list.

    Raises:
        NotAuthenticatedError: no credential, or not a bearer value. Identical
            for every shape of failure, so a caller cannot probe the format.
    """
    header = connection.headers.get("authorization")
    if not header or len(header) > MAX_AUTHORIZATION_LENGTH:
        raise NotAuthenticatedError
    if not header[: len(BEARER_PREFIX)].lower() == BEARER_PREFIX:
        raise NotAuthenticatedError
    presented = header[len(BEARER_PREFIX) :].strip()
    if not presented:
        raise NotAuthenticatedError
    return presented


AgentCredentialDep = Annotated[str, Depends(agent_credential)]


def get_enrollment_service(connection: HTTPConnection) -> AgentEnrollmentService:
    """Return the process-wide agent enrollment service."""
    service: AgentEnrollmentService = connection.app.state.agent_enrollment_service
    return service


def get_connection_registry(connection: HTTPConnection) -> ConnectionRegistry:
    """Return this process's agent connection registry.

    Per process, deliberately and with stated consequences — see
    :mod:`hcc.agents.registry`.
    """
    registry: ConnectionRegistry = connection.app.state.agent_connections
    return registry


EnrollmentServiceDep = Annotated[AgentEnrollmentService, Depends(get_enrollment_service)]

#: Exposed for :mod:`hcc.api.v1.hosts`, so an administrative request that
#: revokes a credential or disables a host can ask this process to close the
#: socket at once rather than leaving the agent connected until its next
#: heartbeat. Best effort by design — see :mod:`hcc.agents.registry`.
ConnectionRegistryDep = Annotated[ConnectionRegistry, Depends(get_connection_registry)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    """What an agent tells the controller about itself while enrolling.

    Every field is optional and advisory. There is deliberately no
    ``host_id``, no ``hostname`` and no ``host_name``: the host is decided by
    the enrollment credential, and a field naming a host would be a field an
    agent could use to claim a different one.
    """

    model_config = {"extra": "forbid"}

    agent_version: str | None = Field(
        default=None,
        max_length=64,
        description="The agent's own version string. Advisory, never trusted.",
    )
    platform: str | None = Field(
        default=None,
        max_length=64,
        description="Reported os/arch, e.g. 'linux/amd64'. Advisory.",
    )


class EnrollResponse(BaseModel):
    """The agent's new identity.

    ``credential`` is the long-lived agent credential and this is the only
    time it crosses the network in this direction. The agent writes it to a
    file only it can read; the controller keeps only its digest.
    """

    host_id: str = Field(description="The host this agent is now the agent for.")
    host_name: str = Field(
        description=(
            "Display name, so the agent's log and `status` output name the host "
            "an operator recognises rather than a UUID."
        )
    )
    credential: str = Field(
        description=(
            "The long-lived agent credential, in plaintext. Store it with "
            "restrictive permissions; it is not retrievable again."
        )
    )
    protocol_version: int = Field(description="The protocol version this controller speaks.")
    heartbeat_interval_seconds: int = Field(
        description="How often the controller expects a heartbeat once connected."
    )
    superseded_previous: bool = Field(
        description=(
            "Whether this enrollment revoked a credential the host already "
            "had. True means another agent has just been cut off — reported so "
            "the agent's own log says it took over rather than joined."
        )
    )

    def __repr__(self) -> str:
        """Representation with the credential redacted.

        Pydantic models are rendered in validation errors and tracebacks, so
        the default would print the plaintext.
        """
        return (
            f"EnrollResponse(host_id={self.host_id!r}, host_name={self.host_name!r}, "
            f"credential=<redacted>)"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


@router.post(
    "/enroll",
    summary="Exchange a one-time enrollment credential for an agent credential",
    response_model=EnrollResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"description": "No credential presented, or it is not usable."},
    },
)
async def enroll(
    db: SessionDep,
    enrollment: EnrollmentServiceDep,
    credential: AgentCredentialDep,
    connection: HTTPConnection,
    payload: EnrollRequest | None = None,
) -> EnrollResponse:
    """Claim a host, once, with the credential an administrator issued.

    The enrollment credential is spent by this call and cannot be used again —
    including by a retry of this request. An agent whose network dropped
    *after* the controller committed but before the response arrived has lost
    its credential and must be issued another; that is the cost of single use,
    and the alternative — a credential usable twice — is a credential that can
    be replayed by whoever read it over somebody's shoulder.

    **200, not 201.** Nothing addressable was created: the host already
    existed, and what came back is a credential rather than a resource with a
    URL. A 201 would imply a ``Location``, and there is none — there is no
    endpoint that returns this credential again.

    Every refusal is a 401 with one message. Expired, already used, revoked and
    never-existed are indistinguishable to the caller on purpose; the audit
    trail records which it was, because an administrator can act on that and an
    attacker holding a stolen credential cannot.
    """
    try:
        enrolled = await enrollment.enroll(
            db,
            credential=credential,
            agent_version=payload.agent_version if payload else None,
            platform=payload.platform if payload else None,
            request_id=request_id_of(connection),
        )
    except EnrollmentRefusedError as exc:
        # 401, not 403: the credential is what failed, and a client that
        # obtained a fresh one would succeed. Mapped to the shared envelope so
        # an agent parses the same error shape everything else returns.
        raise AgentCredentialRejectedError(str(exc)) from exc

    # The unit of work: the enrollment credential is consumed, the previous
    # agent credential revoked, the new one issued and the lifecycle moved,
    # all together. A partial commit here would leave a host that had spent
    # its credential and gained nothing.
    await db.commit()

    return EnrollResponse(
        host_id=str(enrolled.host_id),
        host_name=enrolled.host_name,
        credential=enrolled.credential,
        protocol_version=enrolled.protocol_version,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        superseded_previous=enrolled.superseded_previous,
    )


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


class _FrameBudget:
    """A token bucket bounding one connection's frame rate.

    Per connection rather than per host, so one agent opening sockets in a
    loop is bounded by :data:`hcc.agents.registry.MAX_CONNECTIONS` rather than
    by this, and a well-behaved agent on a busy host is never throttled by a
    neighbour.

    A monotonic clock, not the wall clock: a system time correction — NTP
    stepping backwards, which happens on machines that have just booted —
    would otherwise either grant an unbounded refill or stall the bucket
    entirely.
    """

    __slots__ = ("_last", "_tokens")

    def __init__(self) -> None:
        self._tokens = float(FRAME_BURST)
        self._last = time.monotonic()

    def spend(self) -> bool:
        """Consume one frame's budget, or return ``False`` if there is none."""
        now = time.monotonic()
        self._tokens = min(
            float(FRAME_BURST),
            self._tokens + (now - self._last) * MAX_FRAMES_PER_SECOND,
        )
        self._last = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


def _close_reason(raw: str | None) -> CloseReason:
    """Map a registry-supplied reason onto the protocol vocabulary.

    Falls back to :attr:`CloseReason.SUPERSEDED`, which is what an entry with
    no reason means: the only path that signals without setting one is a newer
    connection registering over an older one.

    Validated rather than cast, because the registry's field is a plain string
    — it has to be, since the registry must not depend on the protocol module
    — and a typo there would otherwise become a frame the agent cannot parse.
    """
    if raw is None:
        return CloseReason.SUPERSEDED
    try:
        return CloseReason(raw)
    except ValueError:
        logger.warning("agent_close_reason_unrecognised", reason=raw)
        return CloseReason.SUPERSEDED


def _refuse_handshake(*, status_code: int, code: str, message: str) -> AppError:
    """Build the error that refuses a handshake before ``accept``.

    **Raised, not sent.** The application's registered ``AppError`` handler
    turns it into the standard error envelope, and Starlette delivers that as
    a WebSocket *denial response* — so an agent gets a real HTTP status and a
    body in the same shape every other endpoint returns, and no socket is ever
    opened.

    Deliberately not :meth:`starlette.websockets.WebSocket.send_denial_response`,
    which was the first attempt. That method requires the ASGI server to
    advertise the ``websocket.http.response`` extension and raises
    ``RuntimeError`` when it does not — turning a clean refusal into a crash on
    any server that has not implemented an optional extension. Raising instead
    goes through the same path the credential dependency already uses, which
    works regardless and keeps one mechanism rather than two.
    """
    return AppError(message, code=code, status_code=status_code)


@router.websocket("/connect")
async def connect(
    socket: WebSocket,
    credential: AgentCredentialDep,
    enrollment: EnrollmentServiceDep,
    database: Annotated[Database, Depends(get_database)],
    registry: Annotated[ConnectionRegistry, Depends(get_connection_registry)],
) -> None:
    """The agent's outbound, authenticated, long-lived connection.

    **The agent dials out** (ADR-018). The controller never connects to a host,
    which is what makes this work for a dedicated server behind NAT with no
    inbound ports open and no SSH from the controller.

    The sequence, and why each step is where it is:

    1. The credential is resolved by the router's dependency, so an
       unauthenticated handshake is refused with a 401 denial response and no
       socket is ever opened.
    2. The protocol version is negotiated from the ``X-HCC-Protocol-Version``
       header, before ``accept``. An agent speaking a version this controller
       does not understand is told so rather than having its frames parsed
       under the wrong assumptions.
    3. The credential is authenticated and the connection recorded, **also
       before accept**, so the host reads as connected the moment the socket is
       open rather than after the first heartbeat.
    4. The socket is accepted, registered, and any previous connection for the
       same host is signalled to close.
    5. Frames are read with a heartbeat timeout, a size bound and a rate
       budget, and authorization is re-checked on an interval.
    6. On any exit, the connection is cleared — but only if it is still the
       current one. A superseded connection's cleanup must not clear its
       replacement's state, which is what the epoch guard is for.

    This handler owns its own database sessions rather than taking the
    request-scoped one. A WebSocket lives for hours; holding a pooled
    connection and an open transaction for its whole life would exhaust the
    pool at a handful of agents, and every write here is a short independent
    unit of work in any case.
    """
    request_id = request_id_of(socket)
    declared = socket.headers.get("x-hcc-protocol-version")
    protocol_version = PROTOCOL_VERSION
    if declared is not None:
        try:
            protocol_version = int(declared)
        except ValueError:
            protocol_version = -1
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            logger.warning("agent_handshake_refused", reason="unsupported_protocol")
            raise _refuse_handshake(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=CloseReason.UNSUPPORTED_PROTOCOL.value,
                message=(
                    "This controller speaks protocol version(s) "
                    f"{', '.join(str(v) for v in SUPPORTED_PROTOCOL_VERSIONS)}. "
                    "Upgrade or downgrade the agent to match."
                ),
            )

    if registry.is_full:
        # A bounded, visible refusal rather than an unbounded acceptance. The
        # agent retries on its own backoff, so a fleet arriving at once after a
        # controller restart settles instead of killing the process.
        logger.warning("agent_handshake_refused", reason="capacity")
        raise _refuse_handshake(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="capacity",
            message="This controller is holding its maximum number of agent connections.",
        )

    agent: AuthenticatedAgent | None = None
    async with database.session_factory() as db:
        try:
            agent = await enrollment.authenticate(
                db,
                credential=credential,
                protocol_version=protocol_version,
                request_id=request_id,
            )
            await db.commit()
        except AgentAuthenticationError as exc:
            await db.rollback()
            if exc.reason is CloseReason.HOST_DISABLED:
                # Post-authentication, so there is nothing left to withhold —
                # and the agent needs to tell "my credential is wrong" apart
                # from "the operator switched this host off". Accepted so the
                # reason arrives as a frame it can log and branch on.
                await socket.accept()
                await socket.send_text(
                    Close(
                        reason=CloseReason.HOST_DISABLED,
                        detail="This host is disabled. Enable it to allow the agent to connect.",
                    ).model_dump_json()
                )
                await socket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            raise _refuse_handshake(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code=CloseReason.UNAUTHORIZED.value,
                message=(
                    "The agent credential is not usable. Re-enroll this host "
                    "from the control plane."
                ),
            ) from exc

    await socket.accept()

    live = LiveConnection(
        host_id=agent.host_id,
        connection_id=agent.connection_id,
        epoch=agent.epoch,
        closing=asyncio.Event(),
    )
    superseded = registry.register(live)
    if superseded is not None:
        # Signalled, not closed from here: closing a socket from outside the
        # task reading it races with that read, and the resulting exception
        # would be reported as a protocol error on a connection that was in
        # fact ending cleanly.
        superseded.reason = CloseReason.SUPERSEDED.value
        superseded.closing.set()

    budget = _FrameBudget()
    last_recheck = time.monotonic()
    closing_reason: CloseReason | None = None
    closing_detail: str | None = None

    try:
        while True:
            receive = asyncio.create_task(socket.receive_text())
            shutdown = asyncio.create_task(live.closing.wait())
            done, pending = await asyncio.wait(
                {receive, shutdown},
                timeout=HEARTBEAT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                # Awaited so the cancellation is observed here rather than
                # surfacing later as "task exception was never retrieved".
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            if not done:
                closing_reason = CloseReason.IDLE_TIMEOUT
                closing_detail = f"no heartbeat within {HEARTBEAT_TIMEOUT_SECONDS}s"
                break

            if shutdown in done:
                # The close was decided elsewhere in this process — a newer
                # connection superseding this one, or an administrator
                # revoking a credential or disabling the host. The reason
                # travels on the registry entry, because flattening all three
                # into "superseded" would tell the agent's operator to go
                # looking for a duplicate agent that does not exist.
                closing_reason = _close_reason(live.reason)
                closing_detail = None
                break

            raw = receive.result()

            if not budget.spend():
                closing_reason = CloseReason.RATE_LIMITED
                closing_detail = f"more than {MAX_FRAMES_PER_SECOND} frames per second"
                break

            try:
                heartbeat = parse_agent_frame(raw)
            except ProtocolError as exc:
                closing_reason = exc.reason
                closing_detail = exc.detail
                break

            async with database.session_factory() as db:
                applied = await enrollment.record_heartbeat(
                    db,
                    agent=agent,
                    agent_version=heartbeat.agent_version,
                    platform=heartbeat.platform,
                )
                await db.commit()

            if not applied:
                # The epoch guard made this write a no-op, so this connection
                # is no longer the host's current one. *Why* is not obvious
                # from that fact alone, and getting it wrong misleads an
                # operator: a revocation also clears `current_connection_id`,
                # so the guard fires for a revoked credential exactly as it
                # does for a genuine supersession.
                #
                # Live verification caught this reporting a revoked agent as
                # "superseded — a newer connection for this host was
                # accepted", which is false and sends whoever reads the
                # agent's log looking for a duplicate agent that does not
                # exist. So the real reason is established before the
                # connection is closed: one indexed read, on a path taken once
                # per connection rather than once per heartbeat.
                async with database.session_factory() as db:
                    refusal = await enrollment.is_still_authorized(db, agent=agent)
                if refusal is not None:
                    closing_reason = refusal
                    closing_detail = "authorization was withdrawn"
                else:
                    closing_reason = CloseReason.SUPERSEDED
                    closing_detail = "a newer connection for this host was accepted"
                break

            await socket.send_text(
                HeartbeatAck(
                    interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                    server_time=datetime.now(UTC).isoformat(),
                ).model_dump_json()
            )

            now = time.monotonic()
            if now - last_recheck >= AUTHORIZATION_RECHECK_SECONDS:
                last_recheck = now
                async with database.session_factory() as db:
                    refusal = await enrollment.is_still_authorized(db, agent=agent)
                if refusal is not None:
                    closing_reason = refusal
                    closing_detail = "authorization was withdrawn"
                    break
    except WebSocketDisconnect:
        logger.info(
            "agent_connection_disconnected",
            host_id=str(agent.host_id),
            connection_id=str(agent.connection_id),
        )
    except Exception:
        # Logged with the traceback and then closed. An unhandled exception
        # here would leave the connection recorded in the database with no
        # socket behind it, which derives as stale rather than as a lie — but
        # stale on a host that is fine is still a false alarm, so the cleanup
        # below must run on every path.
        logger.exception(
            "agent_connection_failed",
            host_id=str(agent.host_id),
            connection_id=str(agent.connection_id),
        )
        closing_reason = CloseReason.PROTOCOL_ERROR
        closing_detail = "the controller ended the connection"
    finally:
        registry.unregister(live)
        async with database.session_factory() as db:
            await enrollment.record_disconnect(db, agent=agent)
            await db.commit()

        if closing_reason is not None:
            # Best effort: the socket may already be gone, and failing to
            # deliver a courtesy frame must not mask the reason the connection
            # ended.
            with contextlib.suppress(Exception):
                await socket.send_text(
                    Close(reason=closing_reason, detail=closing_detail).model_dump_json()
                )
        with contextlib.suppress(Exception):
            await socket.close(code=status.WS_1000_NORMAL_CLOSURE)
