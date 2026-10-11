"""Rate limiter unit tests.

No database, no HTTP, and **no sleeping**. Window rollover is driven by
monkeypatching the monotonic clock rather than by waiting, so the tests are
fast and cannot flake on a loaded CI runner — the same reason the password
tests carry no timing assertions.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from hcc.security.ratelimit import LoginThrottle, RateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Replace the limiter's monotonic clock with one the test controls.

    Returns a function that advances it. Patching the module's ``time``
    reference keeps the limiter's own code untouched.
    """
    now = [1000.0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("hcc.security.ratelimit.time.monotonic", lambda: now[0], raising=True)
    return advance


class TestConstruction:
    @pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (5, 0), (5, -1)])
    def test_nonsensical_configuration_is_rejected(self, limit: int, window: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            RateLimiter(limit=limit, window_seconds=window)

    def test_configuration_is_reported(self) -> None:
        limiter = RateLimiter(limit=7, window_seconds=90)
        assert limiter.limit == 7
        assert limiter.window_seconds == 90


class TestBasicLimiting:
    def test_allows_up_to_the_limit(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=3, window_seconds=60)

        assert [limiter.check("a").allowed for _ in range(3)] == [True, True, True]

    def test_refuses_beyond_the_limit(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            limiter.check("a")

        decision = limiter.check("a")
        assert decision.allowed is False
        assert decision.retry_after_seconds > 0

    def test_keys_have_independent_budgets(self, clock: Callable[[float], None]) -> None:
        # Otherwise one noisy client would lock out everyone else.
        limiter = RateLimiter(limit=2, window_seconds=60)
        for _ in range(2):
            limiter.check("first")

        assert limiter.check("first").allowed is False
        assert limiter.check("second").allowed is True

    def test_refused_attempts_still_count(self, clock: Callable[[float], None]) -> None:
        # A client that keeps hammering must not recover faster than one that
        # backs off; if refusals were uncounted, the carry into the next
        # window would under-report the attack.
        limiter = RateLimiter(limit=2, window_seconds=60)
        for _ in range(10):
            limiter.check("a")

        clock(60.1)
        # The previous window's 10 attempts carry over, so the next window
        # starts already over budget.
        assert limiter.check("a").allowed is False

    def test_retry_after_is_at_least_one_second(self, clock: Callable[[float], None]) -> None:
        # A Retry-After of 0 invites an immediate retry, which is the opposite
        # of the intended effect.
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.check("a")

        assert limiter.check("a").retry_after_seconds >= 1


class TestWindowBehaviour:
    def test_budget_recovers_after_two_windows(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=2, window_seconds=60)
        for _ in range(2):
            limiter.check("a")
        assert limiter.check("a").allowed is False

        # Two full windows with no carry left.
        clock(121)
        assert limiter.check("a").allowed is True

    def test_boundary_burst_is_prevented(self, clock: Callable[[float], None]) -> None:
        # The flaw in a plain fixed window: spend the whole budget at the end
        # of one window, then the whole budget again at the start of the next,
        # for 2x the limit in a moment. The weighted carry prevents it.
        limiter = RateLimiter(limit=10, window_seconds=100)

        # Establish the window, then spend the budget near its end.
        limiter.check("a")  # window_start = now
        clock(90)
        for _ in range(9):
            assert limiter.check("a").allowed is True
        assert limiter.check("a").allowed is False

        # Crossing into the next window does not hand back a fresh budget:
        # 10 attempts 10% of a window ago still weigh ~9 against the limit.
        clock(20)
        allowed_in_new_window = sum(limiter.check("a").allowed for _ in range(5))
        assert allowed_in_new_window <= 2, (
            "a plain fixed window would have allowed a full second budget here"
        )

    def test_carry_decays_across_the_window(self, clock: Callable[[float], None]) -> None:
        # The weighted estimate is "attempts over the trailing window", so
        # the previous window's count fades linearly rather than vanishing at
        # the boundary.
        limiter = RateLimiter(limit=10, window_seconds=100)
        for _ in range(10):
            limiter.check("a")

        # 1% into the next window: carry is 10 x 0.99 = 9.9, so exactly one
        # more attempt fits under the limit of 10 before it refuses again.
        clock(101)
        assert limiter.check("a").allowed is True
        assert limiter.check("a").allowed is False

        # 81% into that window: carry is 10 x 0.19 = 1.9 plus the attempts
        # just made, comfortably under budget again.
        clock(80)
        assert limiter.check("a").allowed is True

    def test_long_idle_period_resets_cleanly(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=2, window_seconds=60)
        for _ in range(5):
            limiter.check("a")

        clock(10_000)
        assert limiter.check("a").allowed is True


class TestReset:
    def test_reset_clears_a_key(self, clock: Callable[[float], None]) -> None:
        # Called only after a successful login, so a user who mistyped a few
        # times is not left throttled.
        limiter = RateLimiter(limit=2, window_seconds=60)
        for _ in range(2):
            limiter.check("a")
        assert limiter.check("a").allowed is False

        limiter.reset("a")
        assert limiter.check("a").allowed is True

    def test_reset_leaves_other_keys_alone(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.check("a")
        limiter.check("b")

        limiter.reset("a")

        assert limiter.check("a").allowed is True
        assert limiter.check("b").allowed is False

    def test_reset_of_an_unknown_key_is_harmless(self) -> None:
        RateLimiter(limit=1, window_seconds=60).reset("never-seen")

    def test_clear_forgets_everything(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.check("a")
        limiter.check("b")

        limiter.clear()

        assert limiter.check("a").allowed is True
        assert limiter.check("b").allowed is True


class TestEviction:
    # These tests reach into `_counters` deliberately: eviction has no
    # public surface, and an unbounded table under attack is exactly the
    # failure worth asserting against.
    def test_idle_keys_are_evicted(self, clock: Callable[[float], None]) -> None:
        # Without eviction, an attacker rotating source addresses would grow
        # the table without bound — the denial of service the limiter exists
        # to prevent.
        limiter = RateLimiter(limit=5, window_seconds=60)
        for index in range(100):
            limiter.check(f"key-{index}")
        assert len(limiter._counters) == 100  # noqa: SLF001

        # Past the idle threshold, and past the eviction interval.
        clock(60 * 5)
        limiter.check("fresh")

        assert len(limiter._counters) == 1  # noqa: SLF001
        assert "fresh" in limiter._counters  # noqa: SLF001

    def test_active_keys_survive_eviction(self, clock: Callable[[float], None]) -> None:
        limiter = RateLimiter(limit=100, window_seconds=60)
        limiter.check("active")

        for _ in range(10):
            clock(30)
            limiter.check("active")

        assert "active" in limiter._counters  # noqa: SLF001

    def test_eviction_does_not_run_on_every_call(self, clock: Callable[[float], None]) -> None:
        # Sweeping on every request would make the limiter itself the hot spot
        # under attack.
        limiter = RateLimiter(limit=1000, window_seconds=1)
        limiter.check("old")

        clock(10)  # well past the idle threshold for a 1s window...
        limiter.check("new")
        # ...but not past the eviction interval, so no sweep happened yet.
        assert "old" in limiter._counters  # noqa: SLF001


class TestThreadSafety:
    def test_concurrent_checks_do_not_exceed_the_limit(self) -> None:
        # The limiter is consulted from the event loop and from worker
        # threads, so a lost update would let the limit be exceeded.
        limiter = RateLimiter(limit=50, window_seconds=300)
        allowed: list[bool] = []
        lock = threading.Lock()

        def hammer() -> None:
            for _ in range(20):
                decision = limiter.check("shared")
                with lock:
                    allowed.append(decision.allowed)

        threads = [threading.Thread(target=hammer) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(allowed) == 200
        # Exactly the limit, never more: 200 attempts against a budget of 50.
        assert sum(allowed) == 50


class TestLoginThrottle:
    @staticmethod
    def build(*, address_limit: int = 10, username_limit: int = 5) -> LoginThrottle:
        return LoginThrottle(
            per_address=RateLimiter(limit=address_limit, window_seconds=300),
            per_username=RateLimiter(limit=username_limit, window_seconds=300),
        )

    def test_both_budgets_must_allow(self, clock: Callable[[float], None]) -> None:
        throttle = self.build(address_limit=100, username_limit=2)

        for _ in range(2):
            assert throttle.check(address="1.2.3.4", username_canonical="alice").allowed

        decision = throttle.check(address="1.2.3.4", username_canonical="alice")
        assert decision.allowed is False
        assert decision.scope == "username"

    def test_address_budget_bounds_a_spray_across_usernames(
        self, clock: Callable[[float], None]
    ) -> None:
        # One host trying many usernames: the per-username budget never fires,
        # so only the per-address budget stops enumeration.
        throttle = self.build(address_limit=3, username_limit=100)

        for index in range(3):
            assert throttle.check(address="1.2.3.4", username_canonical=f"user{index}").allowed

        decision = throttle.check(address="1.2.3.4", username_canonical="user99")
        assert decision.allowed is False
        assert decision.scope == "address"

    def test_username_budget_bounds_a_distributed_attack(
        self, clock: Callable[[float], None]
    ) -> None:
        # Many hosts targeting one account: each address stays under its own
        # budget, so only the per-username budget stops it.
        throttle = self.build(address_limit=100, username_limit=3)

        for index in range(3):
            assert throttle.check(address=f"10.0.0.{index}", username_canonical="admin").allowed

        decision = throttle.check(address="10.0.0.99", username_canonical="admin")
        assert decision.allowed is False
        assert decision.scope == "username"

    def test_address_is_checked_before_username(self, clock: Callable[[float], None]) -> None:
        # Ordering matters: a distributed attack must not be able to burn a
        # victim's username budget from addresses that are already over their
        # own limit.
        throttle = self.build(address_limit=1, username_limit=10)
        throttle.check(address="1.2.3.4", username_canonical="victim")

        decision = throttle.check(address="1.2.3.4", username_canonical="victim")
        assert decision.scope == "address"

        # The victim's username budget was not consumed by the refused
        # attempts, so a legitimate login from elsewhere still works.
        assert throttle.check(address="5.6.7.8", username_canonical="victim").allowed

    def test_reset_clears_both_budgets(self, clock: Callable[[float], None]) -> None:
        throttle = self.build(address_limit=1, username_limit=1)
        throttle.check(address="1.2.3.4", username_canonical="alice")
        assert throttle.check(address="1.2.3.4", username_canonical="alice").allowed is False

        throttle.reset(address="1.2.3.4", username_canonical="alice")

        assert throttle.check(address="1.2.3.4", username_canonical="alice").allowed is True

    def test_clear_forgets_everything(self, clock: Callable[[float], None]) -> None:
        throttle = self.build(address_limit=1, username_limit=1)
        throttle.check(address="1.2.3.4", username_canonical="alice")

        throttle.clear()

        assert throttle.check(address="1.2.3.4", username_canonical="alice").allowed is True
