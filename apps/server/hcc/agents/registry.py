"""Which agent connections this process currently holds.

Phase 1, milestone 1.8. In-memory, per-process, and bounded.

## This is per process, and that is a real limitation

**The registry is not shared across workers.** It is a dictionary in one
Python process, so a controller running two Uvicorn workers has two registries
that cannot see each other — and nothing here pretends otherwise.

What that does and does not break is worth being precise about, because the
tempting mistake is to treat this as the source of truth for connectivity:

* **Connectivity is not affected.** It is derived from the ``hosts`` row —
  ``current_connection_id`` and ``last_heartbeat_at`` — which every worker
  writes to and reads from the same database (ADR-043). A browser asking
  "is homeserver connected?" gets the same answer from either worker.
* **Superseding a connection within one worker works**, because both
  connections are in the same registry.
* **Superseding across workers does not.** Agent reconnects onto worker B
  while worker A still holds its socket; A's registry does not know. The
  epoch guard in :mod:`hcc.services.enrollment` means A's writes become
  no-ops, and A discovers it has been superseded on its next heartbeat or
  authorization re-check and closes. So the state stays correct; the stale
  socket lingers for up to one interval rather than being closed at once.
* **Pushing to an agent across workers does not work.** Phase 2 does push —
  :meth:`ConnectionRegistry.notify` tells a connected agent that a newer
  configuration exists — and a push aimed at a socket another worker holds
  goes nowhere.

  That is a **latency** limitation and not a correctness one, which is the
  whole reason the push was designed as a notification rather than as the
  mechanism. Every heartbeat acknowledgement states the version the controller
  believes is current, so an agent whose notification went to the wrong worker
  notices on its next heartbeat and asks. One dropped push costs up to one
  heartbeat interval; it cannot cost a host running stale configuration
  indefinitely.

  A real channel — PostgreSQL ``LISTEN``/``NOTIFY``, since the dependency is
  already there — would remove even that, and is the thing to add if multiple
  workers ever become the documented deployment. Until then the single-process
  guidance in ADR-046 stands and this is a known, bounded degradation rather
  than an unmeasured one.

So the deployment guidance is **one controller process** for now, stated in
``docs/DEPLOYMENT.md`` rather than enforced in code: a check that refused to
start with ``--workers 2`` would be enforcing a constraint on an operator who
may have good reasons and whose hosts would still report correctly. ADR-046.

## Bounded, because an unbounded registry is a memory leak with extra steps

:data:`MAX_CONNECTIONS` caps how many sockets one process will hold. The cap is
not about hostile agents — each one needs a valid per-host credential — but
about a fleet larger than the process was sized for arriving at once after a
controller restart. Refusing the five hundred and first connection with a
close reason the agent will retry on is a bounded, visible failure; accepting
it is an unbounded one discovered when the process is killed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from hcc.logging import get_logger

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

logger = get_logger(__name__)

#: Most agent connections one controller process will hold at once. Sized well
#: above the fleet this product targets and far below what would exhaust a
#: process; the measured cost per connection is in ``docs/PERFORMANCE.md``.
MAX_CONNECTIONS: Final = 500


#: Outbound notifications one connection may have queued at once.
#:
#: Small on purpose. The queue carries *notifications* — "a newer
#: configuration exists", a few dozen bytes — never documents, so a deep queue
#: would buy nothing: four unread "there is something newer" frames say
#: exactly what one says. Eight is enough that a burst of configuration saves
#: does not drop anything, and small enough that five hundred connections hold
#: four thousand pointers rather than megabytes.
MAX_PENDING_NOTIFICATIONS: Final = 8


@dataclass(slots=True)
class LiveConnection:
    """One accepted agent connection, as this process sees it."""

    host_id: uuid.UUID
    connection_id: uuid.UUID
    epoch: int
    #: Set when the controller decides this connection should end. The reader
    #: task waits on it alongside the socket, so a close decided elsewhere —
    #: a newer connection superseding this one, a revoked credential — takes
    #: effect without waiting for the agent to send anything.
    #:
    #: An event rather than a direct socket close: closing a socket from
    #: outside the task that reads it races with that read, and the resulting
    #: exception surfaces as a protocol error on a connection that was in fact
    #: ending cleanly.
    closing: asyncio.Event
    #: Frames the reader task should send. **Written to by other tasks, read
    #: only by the reader**, which is the point: a WebSocket must be written
    #: by exactly one task, and an HTTP handler that called ``send_text``
    #: directly would interleave with the reader's own writes and corrupt the
    #: stream. Bounded, and the reader is the only consumer.
    #:
    #: Constructed by default rather than by the caller: there is exactly one
    #: correct bound for it and it belongs to this module, so an endpoint that
    #: forgot to pass one would be a bug with no upside. ``asyncio.Queue`` no
    #: longer binds an event loop at construction, so building it here is safe
    #: outside a running loop.
    outbound: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_PENDING_NOTIFICATIONS)
    )
    reason: str | None = field(default=None)


class ConnectionRegistry:
    """The agent connections this process holds.

    Not a cache and not a source of truth. Durable connection state lives in
    the ``hosts`` row; this exists so that *this* process can find a socket it
    is holding in order to close it.
    """

    __slots__ = ("_by_host",)

    def __init__(self) -> None:
        # One entry per host, not a list: a host has one current connection by
        # construction, and a dictionary keyed by host is what makes
        # "supersede whatever was there" a single operation rather than a
        # search that could miss one.
        self._by_host: dict[uuid.UUID, LiveConnection] = {}

    def __len__(self) -> int:
        return len(self._by_host)

    def __iter__(self) -> Iterator[LiveConnection]:
        # Over a copy, because callers iterate in order to close connections,
        # and closing mutates the registry.
        return iter(list(self._by_host.values()))

    @property
    def is_full(self) -> bool:
        """Whether this process has reached :data:`MAX_CONNECTIONS`."""
        return len(self._by_host) >= MAX_CONNECTIONS

    def register(self, connection: LiveConnection) -> LiveConnection | None:
        """Record a connection, returning the one it replaced.

        The returned connection, if any, is **not** closed here. The caller
        signals it, because closing a socket from outside the task that reads
        it races with that read — see :attr:`LiveConnection.closing`.
        """
        previous = self._by_host.get(connection.host_id)
        self._by_host[connection.host_id] = connection
        if previous is not None:
            logger.info(
                "agent_connection_superseded",
                host_id=str(connection.host_id),
                previous_connection_id=str(previous.connection_id),
                connection_id=str(connection.connection_id),
            )
        return previous

    def unregister(self, connection: LiveConnection) -> bool:
        """Remove a connection, if it is still the registered one.

        The identity check matters. A superseded connection's cleanup runs
        *after* its replacement registered, so an unconditional ``del`` would
        remove the live entry and leave this process unable to find the socket
        it is actually holding. Returns whether anything was removed.
        """
        current = self._by_host.get(connection.host_id)
        if current is None or current.connection_id != connection.connection_id:
            return False
        del self._by_host[connection.host_id]
        return True

    def get(self, host_id: uuid.UUID) -> LiveConnection | None:
        """The connection this process holds for a host, if any."""
        return self._by_host.get(host_id)

    def notify(self, host_id: uuid.UUID, frame: str) -> bool:
        """Ask this process to send a frame to a host's agent. Best effort.

        Queued rather than written, for the reason on
        :attr:`LiveConnection.outbound`: only the reader task may write to the
        socket.

        Returns whether the frame was queued. ``False`` means either that this
        process does not hold the socket — another worker does, or the agent is
        offline — or that the queue is full. **Both are acceptable**, and that
        is a design property rather than a concession: the frames sent this way
        are notifications whose content is also stated in every heartbeat
        acknowledgement, so a dropped one is corrected within one heartbeat
        interval. The caller logs the result and does not retry.

        A full queue is logged at warning rather than silently ignored,
        because while one dropped notification is harmless, a queue that is
        *persistently* full means an agent has stopped reading — which is a
        half-open socket the idle timeout should be catching.
        """
        connection = self._by_host.get(host_id)
        if connection is None:
            return False
        try:
            connection.outbound.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning(
                "agent_notification_dropped",
                host_id=str(host_id),
                connection_id=str(connection.connection_id),
                queued=connection.outbound.qsize(),
            )
            return False
        return True

    def close(self, host_id: uuid.UUID, reason: str) -> bool:
        """Ask this process to end a host's connection. Best effort.

        Signals the reader task rather than touching the socket, for the
        reason in :attr:`LiveConnection.closing`.

        **Best effort is the whole contract here**, and it is worth being
        precise about what that buys. This is called by the administrative
        request that revoked a credential or disabled a host, and the operator
        expects the agent cut off *now*:

        * If this process holds the socket — the single-process deployment,
          which is the documented one — the connection closes within
          milliseconds.
        * If another worker holds it, this returns ``False`` and nothing
          happens here. The guarantee is not lost, only delayed: the epoch
          guard turns that connection's next heartbeat into a no-op and the
          endpoint re-checks authorization before closing, so the agent is cut
          off within one heartbeat interval.

        That asymmetry is why the durable check is not replaced by this.
        Relying on the registry alone would make revocation silently
        ineffective on a two-worker deployment; relying on the poll alone
        makes it take up to thirty seconds on every deployment. Both, with
        the database as the one that cannot be wrong.

        Returns whether this process had a connection to signal.
        """
        connection = self._by_host.get(host_id)
        if connection is None:
            return False
        connection.reason = reason
        connection.closing.set()
        logger.info(
            "agent_connection_close_requested",
            host_id=str(host_id),
            connection_id=str(connection.connection_id),
            reason=reason,
        )
        return True
