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

## Keys: three budgets, not two

| Budget | Key | Bounds |
| --- | --- | --- |
| ``per_address`` | source address | one source, against any account |
| ``per_address_username`` | source address **and** username | one source, against one account |
| ``per_username`` | username | every source, against one account |

All three must allow an attempt, and they are consulted in that order.

### Why three, and why the order matters

The obvious two-budget design — per address and per username — has a defect
that was found by audit rather than by reasoning, and it is worth recording
precisely.

With a per-username budget *lower than* the per-address budget, one attacker
exhausts a known account's username budget without exhausting their own
address budget, and can sustain it indefinitely. The victim is then refused
before password verification, from any address, for as long as the attacker
keeps going. That is a remote, unauthenticated, permanent denial of service
against a named administrator — reachable with ten requests every five
minutes from a single host.

The shape here removes it:

* **A single source cannot exhaust the global username budget.** It is cut off
  by ``per_address_username`` first, and because the budgets are consulted in
  order and refusal returns immediately, a source that has hit its per-account
  limit stops contributing to the global one. One source can therefore spend
  at most ``per_address_username`` of ``per_username``.
* **Reaching the global ceiling takes many distinct sources.** At the defaults
  (10 and 100) it takes ten, which is the distributed credential-stuffing case
  the global budget exists to catch.
* ``hcc.config`` enforces ``per_username > per_address_username`` at startup,
  so the unsafe relationship cannot be reintroduced by configuration.

### What is kept

* **Password guessing** is bounded per source by ``per_address``.
* **Targeted guessing** is bounded per source per account by
  ``per_address_username``.
* **Credential stuffing** across many accounts from one source is bounded by
  ``per_address``.
* **Distributed attacks** on one account are bounded by ``per_username``.
* **Argon2 cost** is bounded because every budget is consulted before
  verification.
* **No permanent lockout.** Every budget is a sliding window that refills; no
  state marks an account as locked.

### The residual limitation, stated plainly

A genuinely distributed attacker controlling enough sources can still exhaust
one account's global budget and degrade availability for that account, in
windows. That is inherent to any per-account limit: the alternative is no
global username budget, which permits unlimited distributed guessing against
a known administrator. The trade is bounded degradation over unbounded
guessing, and the ratio between the budgets is what sets how much
distribution an attacker must actually achieve.

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


#: Separator for the composite (address, username) key. A character that
#: cannot appear in a canonical username — `normalize_username` rejects
#: control characters and whitespace, and this is a control character — so two
#: distinct pairs can never collide into one key.
_COMPOSITE_KEY_SEPARATOR: Final = "\x00"


def composite_key(address: str, username_canonical: str) -> str:
    """Build the key for the per-source-per-account budget.

    Exposed so tests can assert on the exact key rather than reconstructing
    the format and drifting from it.
    """
    return f"{address}{_COMPOSITE_KEY_SEPARATOR}{username_canonical}"


@dataclass(frozen=True, slots=True)
class LoginThrottle:
    """The three budgets guarding the login endpoint.

    All must allow an attempt. See the module docstring for why there are
    three, and for the lockout defect this shape exists to prevent.
    """

    per_address: RateLimiter
    per_address_username: RateLimiter
    per_username: RateLimiter

    def check(self, *, address: str, username_canonical: str) -> RateLimitDecision:
        """Consult every budget. Must be called **before** password verification.

        Order is load-bearing, not stylistic. Each budget counts the attempt
        it is asked about, and a refusal returns immediately without
        consulting the ones after it. So:

        * The per-source budget is first, so a source that is over its overall
          limit stops contributing to anything else.
        * The per-source-per-account budget is second, so a source that is
          over its limit against *this* account stops contributing to that
          account's global budget.
        * The global per-account budget is last, and therefore only ever
          accumulates from sources that are still within both of their own
          limits. That is what makes it unreachable by a single source.

        Reordering these would reintroduce the single-source lockout.
        """
        address_decision = self.per_address.check(address, scope="address")
        if not address_decision.allowed:
            return address_decision

        pair_decision = self.per_address_username.check(
            composite_key(address, username_canonical), scope="address_username"
        )
        if not pair_decision.allowed:
            return pair_decision

        return self.per_username.check(username_canonical, scope="username")

    def reset(self, *, address: str, username_canonical: str) -> None:
        """Clear this source's budgets after a successful login.

        The **global** per-username budget is deliberately *not* cleared. A
        successful login from one source says nothing about the failed
        attempts other sources are making against the same account, and
        clearing it would hand an attacker a budget reset for the price of one
        valid credential — which is exactly the position a credential-stuffing
        attacker who has found one working password is in.
        """
        self.per_address.reset(address)
        self.per_address_username.reset(composite_key(address, username_canonical))

    def clear(self) -> None:
        """Forget everything. For tests and administrative reset."""
        self.per_address.clear()
        self.per_address_username.clear()
        self.per_username.clear()
