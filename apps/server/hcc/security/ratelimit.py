"""In-process rate limiting for the authentication path.

Framework-free: no FastAPI, no database. Thread-safe, because the login path
runs partly in a worker thread pool.

## Why this exists at all

An Argon2id verification costs ~64 MiB of RAM and ~50 ms of CPU *by design*
(:mod:`hcc.security.password`). An unauthenticated endpoint that performs one
per request is therefore a denial-of-service amplifier: a few hundred
concurrent requests would exhaust memory on the kind of hardware this product
targets.

So the limiter is checked **before** password verification, never after. The
ordering is the point — a limiter consulted after the expensive work still
lets an attacker spend the server's memory.

A second, independent bound caps *concurrent* verifications
(:class:`hcc.security.password.PasswordHasher` holds a semaphore). Rate
limiting bounds attempts over time; the semaphore bounds peak memory at any
instant. Neither subsumes the other: ten requests per second is survivable
spread out and is not survivable all at once.

## Algorithm: fixed window with a sliding carry

A sliding-window log would be exact but stores a timestamp per attempt, which
is unbounded memory under attack — the thing being defended against. A plain
fixed window is cheap but allows a burst of ``2 x limit`` across a window
boundary.

This uses a weighted two-window estimate: the current window's count plus the
previous window's count scaled by how much of the previous window still falls
inside the look-back period. Memory is two integers per key, and the boundary
burst is gone. The estimate is slightly conservative near a boundary, which is
the right direction to err for a login endpoint.

## Keys: both address and username

Attempts are limited per **client address** and per **username**, with
separate budgets, and both must allow an attempt.

Either alone is insufficient:

* Per-address only lets a botnet spread a password-spray attack across many
  addresses, each under the limit, all targeting one account.
* Per-username only lets one host enumerate many usernames freely, and lets an
  attacker lock a known user out of their own account by burning their budget
  from elsewhere.

Usernames are keyed by their **canonical** form, so ``Admin`` and ``admin``
share a budget rather than offering two.

## Known limitation: single process

State lives in this process's memory. Two consequences, both documented in
``docs/SECURITY.md`` rather than glossed over:

* Running several uvicorn workers gives each its own counters, so the
  effective limit is multiplied by the worker count.
* A restart clears all counters.

This is acceptable for the single-container deployment the project targets
(ADR-001) and is deliberately not solved with Redis, which would be a second
stateful service for one feature. If multi-worker deployment ever becomes
supported, the limiter moves to PostgreSQL or the deployment pins one worker.
See ADR-029.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Final

#: Keys are evicted once idle for this multiple of the window. Without
#: eviction, an attacker cycling addresses would grow the table without bound
#: — the same denial of service the limiter exists to prevent.
_IDLE_EVICTION_WINDOWS: Final = 4

#: Eviction runs at most this often, so a burst does not pay for a sweep on
#: every single request.
_EVICTION_INTERVAL_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of consulting the limiter."""

    allowed: bool
    #: Seconds until the caller may retry. Zero when allowed. Surfaced to the
    #: client as ``Retry-After`` so a well-behaved client can back off instead
    #: of hammering.
    retry_after_seconds: int = 0
    #: Which budget refused the attempt, for logging. Never sent to a client:
    #: telling an attacker whether they hit the address or the username limit
    #: reveals whether that username is being targeted by others.
    scope: str = ""


@dataclass(slots=True)
class _Counter:
    """Two-window attempt counts for one key."""

    window_start: float
    current: int = 0
    previous: int = 0
    last_seen: float = field(default=0.0)


class RateLimiter:
    """Fixed-window-with-carry limiter, keyed by arbitrary strings.

    Thread-safe. One instance holds one budget — the login path uses two, one
    for addresses and one for usernames.
    """

    __slots__ = ("_counters", "_last_eviction", "_limit", "_lock", "_window_seconds")

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        if window_seconds < 1:
            raise ValueError(f"window_seconds must be at least 1, got {window_seconds}")

        self._limit = limit
        self._window_seconds = float(window_seconds)
        self._counters: dict[str, _Counter] = {}
        # A plain lock, not an asyncio one: the limiter is consulted from both
        # the event loop and worker threads, and the critical section is a few
        # arithmetic operations.
        self._lock = threading.Lock()
        self._last_eviction = 0.0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return int(self._window_seconds)

    def check(self, key: str, *, scope: str = "") -> RateLimitDecision:
        """Record an attempt against ``key`` and decide whether to allow it.

        Counts the attempt whether or not it is allowed, so that a client
        which keeps trying stays blocked rather than recovering by exceeding
        the limit.
        """
        now = time.monotonic()

        with self._lock:
            self._evict_idle_locked(now)
            counter = self._counters.get(key)

            if counter is None:
                counter = _Counter(window_start=now, last_seen=now)
                self._counters[key] = counter
            else:
                self._roll_window_locked(counter, now)

            estimate = self._weighted_count_locked(counter, now)
            counter.last_seen = now

            if estimate >= self._limit:
                # Still counted: continuing to hammer must not shorten the
                # penalty.
                counter.current += 1
                elapsed = now - counter.window_start
                retry_after = max(1, int(self._window_seconds - elapsed) + 1)
                return RateLimitDecision(
                    allowed=False, retry_after_seconds=retry_after, scope=scope
                )

            counter.current += 1
            return RateLimitDecision(allowed=True)

    def reset(self, key: str) -> None:
        """Forget a key's attempts.

        Called after a *successful* login, so that a user who mistyped their
        password several times is not left throttled once they get it right.
        Only ever called on success, so it cannot be used to clear a budget
        without knowing the password.
        """
        with self._lock:
            self._counters.pop(key, None)

    def clear(self) -> None:
        """Forget everything. For tests and administrative reset."""
        with self._lock:
            self._counters.clear()
            self._last_eviction = 0.0

    def _roll_window_locked(self, counter: _Counter, now: float) -> None:
        elapsed = now - counter.window_start
        if elapsed < self._window_seconds:
            return
        if elapsed < self._window_seconds * 2:
            # Exactly one window has passed: today's count becomes yesterday's.
            counter.previous = counter.current
            counter.current = 0
            counter.window_start += self._window_seconds
        else:
            # More than two windows idle: nothing to carry forward.
            counter.previous = 0
            counter.current = 0
            counter.window_start = now

    def _weighted_count_locked(self, counter: _Counter, now: float) -> float:
        """Current count plus the still-relevant share of the previous window."""
        elapsed = now - counter.window_start
        carry_fraction = max(0.0, 1.0 - elapsed / self._window_seconds)
        return counter.current + counter.previous * carry_fraction

    def _evict_idle_locked(self, now: float) -> None:
        if now - self._last_eviction < _EVICTION_INTERVAL_SECONDS:
            return
        self._last_eviction = now

        cutoff = now - self._window_seconds * _IDLE_EVICTION_WINDOWS
        stale = [key for key, counter in self._counters.items() if counter.last_seen < cutoff]
        for key in stale:
            del self._counters[key]


@dataclass(frozen=True, slots=True)
class LoginThrottle:
    """The pair of budgets guarding the login endpoint.

    Both must allow an attempt. See the module docstring for why one alone is
    insufficient.
    """

    per_address: RateLimiter
    per_username: RateLimiter

    def check(self, *, address: str, username_canonical: str) -> RateLimitDecision:
        """Consult both budgets. Must be called **before** password verification.

        The address budget is checked first, so a distributed attack on one
        username cannot be used to exhaust that username's budget while each
        individual address is already over its own.
        """
        address_decision = self.per_address.check(address, scope="address")
        if not address_decision.allowed:
            return address_decision

        return self.per_username.check(username_canonical, scope="username")

    def reset(self, *, address: str, username_canonical: str) -> None:
        """Clear both budgets after a successful login."""
        self.per_address.reset(address)
        self.per_username.reset(username_canonical)

    def clear(self) -> None:
        """Forget everything. For tests and administrative reset."""
        self.per_address.clear()
        self.per_username.clear()
