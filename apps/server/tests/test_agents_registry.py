"""Connection registry unit tests.

No database and no socket. The registry is a dictionary with three operations,
and two of them have a subtlety worth pinning: ``unregister`` is conditional,
and ``register`` returns what it displaced rather than closing it.
"""

from __future__ import annotations

import asyncio
import uuid

from hcc.agents.registry import MAX_CONNECTIONS, ConnectionRegistry, LiveConnection


def connection(host_id: uuid.UUID, epoch: int = 1) -> LiveConnection:
    return LiveConnection(
        host_id=host_id,
        connection_id=uuid.uuid4(),
        epoch=epoch,
        closing=asyncio.Event(),
    )


def test_a_registered_connection_is_findable_by_host() -> None:
    registry = ConnectionRegistry()
    host = uuid.uuid4()
    live = connection(host)

    registry.register(live)

    assert registry.get(host) is live
    assert len(registry) == 1


def test_registering_returns_the_connection_it_displaced() -> None:
    """Returned rather than closed.

    Closing a socket from outside the task that reads it races with that read,
    and the resulting exception would be reported as a protocol error on a
    connection that was in fact ending cleanly. The caller signals it instead.
    """
    registry = ConnectionRegistry()
    host = uuid.uuid4()
    first = connection(host, epoch=1)
    second = connection(host, epoch=2)
    registry.register(first)

    displaced = registry.register(second)

    assert displaced is first
    assert not first.closing.is_set()
    assert registry.get(host) is second
    assert len(registry) == 1


def test_the_first_connection_for_a_host_displaces_nothing() -> None:
    registry = ConnectionRegistry()

    assert registry.register(connection(uuid.uuid4())) is None


def test_unregistering_a_superseded_connection_leaves_the_live_one() -> None:
    """The conditional removal, and the reason it is conditional.

    A superseded connection's cleanup runs *after* its replacement has
    registered. An unconditional delete would remove the live entry and leave
    the process unable to find the socket it is actually holding — so a later
    supersession would not close anything.
    """
    registry = ConnectionRegistry()
    host = uuid.uuid4()
    first = connection(host, epoch=1)
    second = connection(host, epoch=2)
    registry.register(first)
    registry.register(second)

    removed = registry.unregister(first)

    assert removed is False
    assert registry.get(host) is second


def test_unregistering_the_live_connection_removes_it() -> None:
    registry = ConnectionRegistry()
    host = uuid.uuid4()
    live = connection(host)
    registry.register(live)

    assert registry.unregister(live) is True
    assert registry.get(host) is None
    assert len(registry) == 0


def test_unregistering_something_never_registered_is_a_no_op() -> None:
    registry = ConnectionRegistry()

    assert registry.unregister(connection(uuid.uuid4())) is False


def test_hosts_do_not_interfere_with_each_other() -> None:
    registry = ConnectionRegistry()
    first, second = uuid.uuid4(), uuid.uuid4()
    registry.register(connection(first))
    registry.register(connection(second))

    assert len(registry) == 2
    assert registry.get(first) is not registry.get(second)


def test_the_registry_reports_when_it_is_full() -> None:
    """Bounded, because an unbounded registry is a memory leak with extra
    steps. Refusing the next connection is a visible failure; accepting it is
    one discovered when the process is killed.
    """
    registry = ConnectionRegistry()
    for _ in range(MAX_CONNECTIONS - 1):
        registry.register(connection(uuid.uuid4()))

    assert not registry.is_full

    registry.register(connection(uuid.uuid4()))

    assert registry.is_full


def test_iterating_tolerates_removal_during_the_loop() -> None:
    """Callers iterate in order to close connections, and closing mutates the
    registry — so iterating the live dictionary would raise mid-shutdown.
    """
    registry = ConnectionRegistry()
    for _ in range(5):
        registry.register(connection(uuid.uuid4()))

    for live in registry:
        registry.unregister(live)

    assert len(registry) == 0
