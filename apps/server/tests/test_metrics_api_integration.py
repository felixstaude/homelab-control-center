"""The metric read endpoints, over HTTP, against real PostgreSQL.

Phase 3, §20 and §21. The service's query behaviour is covered in
``tests/test_telemetry_integration.py``; what is tested here is the part a
browser sees, and every test is a form of one question: **can this API make the
interface show a number that is not true?**

The four ways it could, and the four groups below:

* **Zero instead of unknown.** A host with no samples must return an empty
  list, not a list of zeroes — §21. An interface handed ``0`` has no way to
  know it means "no data", and "CPU 0%" is a claim about a machine.
* **Stale instead of current.** A reading from three days ago is not the
  current CPU usage. It is absent from ``latest`` rather than returned without
  comment, and the response states the window it applied.
* **One host's data under another's name.** A series id is a sequential
  ``BIGINT``, so the host is checked on every range query.
* **A resolution the caller did not expect.** Which tier answered is reported,
  because "this is hourly data" is needed to read a flat-looking graph.

Authorization is asserted too: these are reads on ``_protected``, so every
authenticated role may call them and an anonymous caller may not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from hcc.api.v1.metrics import (
    DEFAULT_FRESHNESS_SECONDS,
    MAX_FRESHNESS_SECONDS,
    MAX_RANGE_DAYS,
    MIN_FRESHNESS_SECONDS,
)
from hcc.models.user import UserRole
from hcc.monitoring.modules import MODULE_IDS
from hcc.services.metrics import MAX_QUERY_POINTS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

pytestmark = pytest.mark.integration

HOSTS = "/api/v1/hosts"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def metrics_path(host_id: str, leaf: str) -> str:
    return f"{HOSTS}/{host_id}/metrics/{leaf}"


async def admin_host(client: AsyncClient, seed_user: SeedUser, sign_in: SignIn) -> tuple[str, str]:
    """An administrator session and one host. Returns ``(host_id, csrf_token)``."""
    await seed_user(client, username="admin", role=UserRole.ADMIN)
    token = await sign_in(client, "admin")
    host_id: str = (
        await client.post(HOSTS, json={"name": "homeserver"}, headers=csrf(token))
    ).json()["host_id"]
    return host_id, token


async def write_samples(
    client: AsyncClient,
    host_id: str,
    points: list[tuple[datetime, float]],
    *,
    module: str = "cpu",
    metric: str = "utilisation",
    unit: str = "percent",
) -> int:
    """Insert a series and raw samples directly, returning the series id.

    Not through the agent socket: the ingestion path rejects timestamps outside
    its skew and backfill bounds, which is correct for an agent and makes it
    impossible to arrange a *stale* reading — the state half these tests are
    about. The read path is what is under test, so its input is arranged
    directly.
    """
    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        series_id: int = (
            await db.execute(
                text(
                    "INSERT INTO metric_series "
                    "(host_id, module, metric, unit, kind, labels, labels_key) "
                    "VALUES (:host_id, :module, :metric, :unit, 'gauge', '{}', '') "
                    "ON CONFLICT (host_id, module, metric, labels_key) DO UPDATE "
                    "SET unit = EXCLUDED.unit RETURNING id"
                ).bindparams(host_id=uuid.UUID(host_id), module=module, metric=metric, unit=unit)
            )
        ).scalar_one()
        newest: datetime | None = None
        for at, value in points:
            await db.execute(
                text(
                    "INSERT INTO metric_samples_raw (series_id, recorded_at, value) "
                    "VALUES (:series_id, :at, :value) ON CONFLICT DO NOTHING"
                ).bindparams(series_id=series_id, at=at, value=value)
            )
            newest = at if newest is None or at > newest else newest
        if newest is not None:
            await db.execute(
                text(
                    "UPDATE metric_series SET last_sample_at = :at WHERE id = :series_id"
                ).bindparams(at=newest, series_id=series_id)
            )
        await db.commit()
    return series_id


# ---------------------------------------------------------------------------
# Nothing is invented: empty is empty, not zero
# ---------------------------------------------------------------------------


async def test_a_host_with_no_samples_returns_no_values_not_zeroes(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§21. A list of zeroes would render as "CPU 0%", which is a claim."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)

        response = await client.get(metrics_path(host_id, "latest"))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["values"] == []
        # And the window is stated, so the interface can say "no data in the
        # last 5 minutes" rather than the weaker "no data".
        assert body["freshness_seconds"] == DEFAULT_FRESHNESS_SECONDS
        assert body["host_id"] == host_id


async def test_a_host_with_no_series_reports_every_module_as_empty(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """ "This module has no data" and "this module does not exist" differ."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)

        body = (await client.get(metrics_path(host_id, "status"))).json()

        assert body["sample_count"] == 0
        assert body["series_count"] == 0
        assert body["last_sample_at"] is None
        # Against the registry rather than a copy of it: the property worth
        # asserting is "the endpoint reports every module the controller
        # knows", and a pinned list asserts instead that somebody remembered
        # to edit two files. test_monitoring_config.py is where the registry's
        # own contents are pinned.
        assert {module["module"] for module in body["modules"]} == set(MODULE_IDS)
        for module in body["modules"]:
            assert module["series_count"] == 0
            assert module["last_sample_at"] is None
            # Named, so the interface never has to invent a label.
            assert module["module_name"]


async def test_an_empty_range_returns_no_points_rather_than_a_flat_line(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§22: a gap must not be interpolated into something that implies data."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        series_id = await write_samples(client, host_id, [])

        body = (
            await client.get(
                metrics_path(host_id, "range"),
                params={
                    "series_id": series_id,
                    "start": "2026-10-01T00:00:00Z",
                    "end": "2026-10-02T00:00:00Z",
                },
            )
        ).json()

        assert body["points"] == []
        assert body["truncated"] is False


async def test_a_gap_in_a_series_stays_a_gap(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Two clusters of samples with an hour of nothing between them.

    The response must contain the samples and nothing in between — no
    zero-filled buckets, no straight line across the hole. The interface draws
    a break because the data has one.
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        points = [(base + timedelta(seconds=i * 30), 10.0) for i in range(4)]
        points += [(base + timedelta(hours=1, seconds=i * 30), 20.0) for i in range(4)]
        series_id = await write_samples(client, host_id, points)

        body = (
            await client.get(
                metrics_path(host_id, "range"),
                params={
                    "series_id": series_id,
                    "start": base.isoformat(),
                    "end": (base + timedelta(hours=2)).isoformat(),
                    "resolution": "raw",
                },
            )
        ).json()

        assert len(body["points"]) == 8
        moments = [datetime.fromisoformat(point["at"]) for point in body["points"]]
        # The hole is present as an absence: consecutive points straddle it.
        gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(moments)]
        assert max(gaps) == pytest.approx(timedelta(minutes=58, seconds=30).total_seconds())


# ---------------------------------------------------------------------------
# Stale is not current
# ---------------------------------------------------------------------------


async def test_a_stale_reading_is_absent_from_latest(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The whole design of the endpoint.

    A three-day-old CPU reading returned without comment would be presented as
    the current one, and nothing in the response would say otherwise.
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        stale = datetime.now(UTC) - timedelta(days=3)
        await write_samples(client, host_id, [(stale, 73.5)])

        body = (await client.get(metrics_path(host_id, "latest"))).json()

        assert body["values"] == []
        # But the series itself is still discoverable, so the interface can say
        # "last seen three days ago" instead of pretending the host never
        # reported.
        series = (await client.get(metrics_path(host_id, "series"))).json()["series"]
        assert len(series) == 1
        assert series[0]["last_sample_at"] is not None


async def test_a_fresh_reading_is_returned_with_its_timestamp(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        recent = datetime.now(UTC) - timedelta(seconds=30)
        await write_samples(client, host_id, [(recent, 73.5)])

        body = (await client.get(metrics_path(host_id, "latest"))).json()

        assert len(body["values"]) == 1
        value = body["values"][0]
        assert value["value"] == pytest.approx(73.5)
        assert datetime.fromisoformat(value["recorded_at"]) == recent
        assert value["series"]["module"] == "cpu"
        assert value["series"]["metric"] == "utilisation"
        assert value["series"]["unit"] == "percent"
        # The registry's human-readable names, for the interface.
        assert value["series"]["module_name"]
        assert value["series"]["metric_name"]


async def test_the_freshness_window_can_be_widened_by_the_caller(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A deliberate choice to see older data is allowed, and reported.

    The point is that it is *stated*: the response carries the window, so a
    value shown came with the claim "within this many seconds" rather than with
    an implied "now".
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        older = datetime.now(UTC) - timedelta(minutes=30)
        await write_samples(client, host_id, [(older, 42.0)])

        default = (await client.get(metrics_path(host_id, "latest"))).json()
        assert default["values"] == []

        widened = (
            await client.get(metrics_path(host_id, "latest"), params={"freshness_seconds": 3600})
        ).json()
        assert len(widened["values"]) == 1
        assert widened["freshness_seconds"] == 3600


@pytest.mark.parametrize("freshness", [MIN_FRESHNESS_SECONDS - 1, MAX_FRESHNESS_SECONDS + 1, 0, -5])
async def test_a_freshness_window_outside_the_bounds_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, freshness: int
) -> None:
    """Not silently clamped. §7: invalid settings are not quietly ignored."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)

        response = await client.get(
            metrics_path(host_id, "latest"), params={"freshness_seconds": freshness}
        )

        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# A series id is not authorization
# ---------------------------------------------------------------------------


async def test_another_hosts_series_is_not_readable_through_this_host(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The ids are sequential, so incrementing one must not cross hosts.

    404, the same answer as a series that does not exist: a distinct error
    would confirm the id is real.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        first = (await client.post(HOSTS, json={"name": "one"}, headers=csrf(token))).json()[
            "host_id"
        ]
        second = (await client.post(HOSTS, json={"name": "two"}, headers=csrf(token))).json()[
            "host_id"
        ]
        series_id = await write_samples(client, first, [(datetime.now(UTC), 10.0)])

        response = await client.get(
            metrics_path(second, "range"),
            params={
                "series_id": series_id,
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
            },
        )

        assert response.status_code == 404, response.text
        # And the same for an id that never existed, so the two are
        # indistinguishable.
        missing = await client.get(
            metrics_path(second, "range"),
            params={
                "series_id": series_id + 10_000,
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
            },
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == response.json()["error"]["code"]


@pytest.mark.parametrize("leaf", ["latest", "series", "status"])
async def test_a_metric_query_for_an_unknown_host_is_a_404(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, leaf: str
) -> None:
    """Not an empty series, which would look like a host that collected nothing."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        await sign_in(client, "admin")

        response = await client.get(metrics_path(str(uuid.uuid4()), leaf))

        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# The resolution is reported, and the bounds are enforced
# ---------------------------------------------------------------------------


async def test_the_tier_that_answered_is_reported(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """ "This is hourly data" is needed to read a flat-looking graph.

    The tier follows from the range and the point budget, so the windows here
    are chosen to force each answer. Ten minutes at one sample a second is 600
    points, inside the default budget of 1,000, so raw answers. An hour is
    3,600 and would not fit — which is itself the behaviour being relied on.
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        series_id = await write_samples(client, host_id, [(base, 10.0)])

        short = (
            await client.get(
                metrics_path(host_id, "range"),
                params={
                    "series_id": series_id,
                    "start": base.isoformat(),
                    "end": (base + timedelta(minutes=10)).isoformat(),
                },
            )
        ).json()
        assert short["resolution"] == "raw"

        # A sixty-day window cannot be served at raw resolution within the
        # point budget, so a coarser tier answers — and says so.
        long = (
            await client.get(
                metrics_path(host_id, "range"),
                params={
                    "series_id": series_id,
                    "start": (base - timedelta(days=60)).isoformat(),
                    "end": base.isoformat(),
                },
            )
        ).json()
        assert long["resolution"] in {"5m", "1h"}
        assert long["resolution"] != "raw"


async def test_a_range_longer_than_the_bound_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The planning cost grows with the partitions a query touches."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        series_id = await write_samples(client, host_id, [(datetime.now(UTC), 1.0)])

        response = await client.get(
            metrics_path(host_id, "range"),
            params={
                "series_id": series_id,
                "start": "2020-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["max_range_days"] == MAX_RANGE_DAYS


async def test_an_unknown_resolution_is_refused_with_the_valid_ones(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        series_id = await write_samples(client, host_id, [(datetime.now(UTC), 1.0)])

        response = await client.get(
            metrics_path(host_id, "range"),
            params={
                "series_id": series_id,
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
                "resolution": "30s",
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["resolutions"] == ["raw", "5m", "1h"]


@pytest.mark.parametrize("field", ["start", "end"])
async def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, field: str
) -> None:
    """A browser in another zone would otherwise get a plausible, wrong window.

    Refused rather than assumed, because assuming is how that bug ships.
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        series_id = await write_samples(client, host_id, [(datetime.now(UTC), 1.0)])
        params: dict[str, Any] = {
            "series_id": series_id,
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-10-02T00:00:00Z",
        }
        params[field] = "2026-10-01T12:00:00"  # No zone.

        response = await client.get(metrics_path(host_id, "range"), params=params)

        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["parameter"] == field


async def test_a_point_budget_above_the_bound_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """An unbounded point count is a way to ask for the whole table."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        series_id = await write_samples(client, host_id, [(datetime.now(UTC), 1.0)])

        response = await client.get(
            metrics_path(host_id, "range"),
            params={
                "series_id": series_id,
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
                "max_points": MAX_QUERY_POINTS + 1,
            },
        )

        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Status answers the question a chart cannot
# ---------------------------------------------------------------------------


async def test_status_counts_samples_and_names_the_newest(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        await write_samples(
            client, host_id, [(base + timedelta(minutes=i), float(i)) for i in range(5)]
        )
        await write_samples(
            client,
            host_id,
            [(base + timedelta(minutes=10), 2048.0)],
            module="memory",
            metric="used_bytes",
            unit="bytes",
        )

        body = (await client.get(metrics_path(host_id, "status"))).json()

        assert body["sample_count"] == 6
        assert body["series_count"] == 2
        assert datetime.fromisoformat(body["last_sample_at"]) == base + timedelta(minutes=10)

        by_module = {module["module"]: module for module in body["modules"]}
        assert by_module["cpu"]["series_count"] == 1
        assert by_module["memory"]["series_count"] == 1
        # Still listed, still empty — not omitted.
        assert by_module["swap"]["series_count"] == 0
        assert by_module["swap"]["last_sample_at"] is None


async def test_the_series_list_includes_a_series_that_has_stopped_reporting(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """ "No CPU data" and "CPU data until Tuesday" are different answers."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        long_ago = datetime.now(UTC) - timedelta(days=30)
        await write_samples(client, host_id, [(long_ago, 5.0)])

        body = (await client.get(metrics_path(host_id, "series"))).json()

        assert len(body["series"]) == 1
        assert datetime.fromisoformat(body["series"][0]["last_sample_at"]) == long_ago
        assert body["series"][0]["series_id"] > 0


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("leaf", ["latest", "series", "status"])
async def test_an_anonymous_caller_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, leaf: str
) -> None:
    """On ``_protected``, so default-deny applies without the route saying so."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        await client.post("/api/v1/auth/logout", headers=csrf(await sign_in(client, "admin")))
        client.cookies.clear()

        response = await client.get(metrics_path(host_id, leaf))

        assert response.status_code == 401, response.text


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN])
async def test_every_authenticated_role_may_read_metrics(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
) -> None:
    """A viewer who cannot see a chart cannot use a monitoring product."""
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        await write_samples(client, host_id, [(datetime.now(UTC), 12.0)])

        await seed_user(client, username="reader", role=role)
        await sign_in(client, "reader")

        for leaf in ("latest", "series", "status"):
            response = await client.get(metrics_path(host_id, leaf))
            assert response.status_code == 200, f"{leaf}: {response.text}"


async def test_a_real_range_response_arrives_compressed(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The measured reason this endpoint is on the compression allow-list.

    ``tests/test_compression.py`` asserts the middleware's behaviour against a
    stand-in route, which proves the path pattern and the negotiation but not
    that it matches the route as mounted. This is the real route, with real
    samples, through the real application: if the path ever changes shape, the
    pattern stops matching and the saving disappears silently. That is the
    failure this catches.

    Measured at the interface's own budget of 600 points, a chart is 59.5 kB
    uncompressed and 6.8 kB gzipped — timestamps five minutes apart being about
    the most repetitive thing a JSON encoder can emit.
    """
    async with api_factory() as client:
        host_id, _ = await admin_host(client, seed_user, sign_in)
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        # Enough points for the response to clear the compression threshold,
        # one second apart so they stay at raw resolution within the budget.
        series_id = await write_samples(
            client,
            host_id,
            [(base + timedelta(seconds=n), 17.0 + (n % 97) * 0.37) for n in range(600)],
        )
        params: dict[str, str | int] = {
            "series_id": series_id,
            "start": base.isoformat(),
            "end": (base + timedelta(minutes=10)).isoformat(),
        }

        compressed = await client.get(
            metrics_path(host_id, "range"), params=params, headers={"Accept-Encoding": "gzip"}
        )
        verbatim = await client.get(
            metrics_path(host_id, "range"), params=params, headers={"Accept-Encoding": "identity"}
        )

    assert compressed.status_code == 200, compressed.text
    assert compressed.headers["content-encoding"] == "gzip"
    # A client that does not ask is not given it, which is the other half of
    # honouring Accept-Encoding.
    assert "content-encoding" not in verbatim.headers
    # httpx decompresses, so matching bodies is the round-trip assertion: a
    # truncated chart would fail in the browser rather than here.
    assert compressed.json() == verbatim.json()
    assert len(compressed.json()["points"]) == 600


async def test_the_enrollment_credential_is_never_compressed(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The boundary the allow-list exists for, on the real endpoint.

    ``hcc/api/v1/hosts.py`` documents the enrollment response as the only one
    in the API containing a secret. Compressing a response that holds both a
    secret and attacker-influenced text leaks the secret through its length —
    so this asserts, against the route that actually returns a credential,
    that no ``Content-Encoding`` is applied to it however the client asks.
    """
    async with api_factory() as client:
        host_id, token = await admin_host(client, seed_user, sign_in)

        response = await client.post(
            f"{HOSTS}/{host_id}/enrollment",
            headers={**csrf(token), "Accept-Encoding": "gzip, deflate, br"},
        )

    assert response.status_code == 201, response.text
    # The credential is there, so this is the secret-bearing response and not
    # an error that happens to be uncompressed.
    assert response.json()["credential"]
    assert "content-encoding" not in response.headers


async def test_no_write_endpoint_exists_for_metrics(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§2.4 and §32: a browser session must not be able to fabricate history.

    Metrics arrive through the authenticated agent connection and nowhere else.
    Asserted against the method table rather than by trying one URL, so a
    future write route cannot be added here unnoticed.
    """
    async with api_factory() as client:
        schema = (await client.get("/api/v1/../../openapi.json")).json()

    metric_paths = {path: spec for path, spec in schema["paths"].items() if "/metrics/" in path}
    assert metric_paths, "the metric endpoints are missing from the schema"
    for path, spec in metric_paths.items():
        assert set(spec) == {"get"}, f"{path} accepts more than GET: {sorted(spec)}"
