"""Host model unit tests.

No database. These cover the Python-level behaviour: name normalization, the
``name_canonical`` invariant, lifecycle coercion, the lifecycle state machine,
the connectivity derivation, and that no representation prints token material.

Database-enforced behaviour (uniqueness, CHECK constraints, server defaults,
the partial unique index, the ``updated_at`` trigger) is covered in
``test_models_host_integration.py`` against real PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import DateTime, String

from hcc.db import Base
from hcc.models import (
    HEARTBEAT_GRACE_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HOST_LIFECYCLE_TRANSITIONS,
    HOST_LIFECYCLE_VALUES,
    AgentCredential,
    Host,
    HostConnectivity,
    HostEnrollment,
    HostLifecycle,
    HostLifecycleError,
    canonical_host_name,
    check_lifecycle_transition,
    derive_connectivity,
    normalize_host_name,
)
from hcc.models.host import HOST_NAME_MAX_LENGTH
from hcc.security.tokens import TOKEN_HASH_LENGTH

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)

# A realistically shaped digest. Not derived from any real token.
FAKE_DIGEST = "a" * TOKEN_HASH_LENGTH


def make_host(**overrides: object) -> Host:
    values: dict[str, object] = {"name": "homeserver"}
    values.update(overrides)
    return Host(**values)


# ---------------------------------------------------------------------------
# normalize_host_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("homeserver", "homeserver"),
        # Case is preserved in the stored form, unlike a username.
        ("HomeServer", "HomeServer"),
        ("  homeserver  ", "homeserver"),
        ("\thomeserver\n", "homeserver"),
        # Interior spaces are legitimate: "backup nas" is a reasonable name.
        ("backup nas", "backup nas"),
        # Runs collapse, so two names cannot differ invisibly.
        ("backup   nas", "backup nas"),
        ("backup\tnas", "backup nas"),
        # A newline is whitespace, so it collapses like a tab rather than
        # being refused — the danger it poses (a broken log line, one name
        # appearing to be two) is removed by the fold. Drawing the line
        # anywhere else would mean refusing \n while accepting \t.
        ("home\nserver", "home server"),
        ("home\r\nserver", "home server"),
        # U+00A0 NO-BREAK SPACE: whitespace to Python, and on screen
        # indistinguishable from a space, so it must not be able to
        # produce a second host that looks like the first.
        ("home\xa0server", "home server"),
        # NFKC folds full-width Latin onto ASCII, so a lookalike cannot
        # coexist with the real name.
        ("\uff28\uff4f\uff53\uff54", "Host"),  # fullwidth "Host"
        # A hyphenated FQDN-ish name is the common case and must survive.
        ("db-01.rack2.lan", "db-01.rack2.lan"),
    ],
)
def test_normalization_folds_and_collapses(raw: str, expected: str) -> None:
    assert normalize_host_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "\xa0"])
def test_an_empty_or_whitespace_only_name_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_host_name(raw)


@pytest.mark.parametrize(
    "raw",
    [
        # NUL. Truncates the name in anything that reaches a C string.
        "home\x00server",
        # U+0007 BELL — a control character that is not whitespace, so the
        # collapse leaves it in place.
        "home\x07server",
        # U+202E RIGHT-TO-LEFT OVERRIDE. Renders a name backwards, so
        # "homeserver" can be made to display as something else entirely.
        "home\u202eserver",
        # U+200B ZERO WIDTH SPACE. Not whitespace to Python, so it survives
        # the collapse and would give two names that look identical.
        "home\u200bserver",
        # U+00AD SOFT HYPHEN: invisible until the line wraps.
        "home\xadserver",
    ],
)
def test_control_characters_that_folding_cannot_make_safe_are_refused(raw: str) -> None:
    """The ones a collapse cannot neutralise.

    Whitespace controls are handled by collapsing them — see
    ``test_normalization_folds_and_collapses``. These are different in kind: a
    NUL truncates, an override reverses, a zero-width character is invisible,
    and none of that is fixed by replacing the character with a space.
    """
    with pytest.raises(ValueError, match="control characters"):
        normalize_host_name(raw)


def test_a_name_over_the_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="at most 64 characters"):
        normalize_host_name("h" * (HOST_NAME_MAX_LENGTH + 1))


def test_a_name_exactly_at_the_limit_is_accepted() -> None:
    at_limit = "h" * HOST_NAME_MAX_LENGTH
    assert normalize_host_name(at_limit) == at_limit


def test_the_length_limit_applies_after_normalization() -> None:
    """Padding must not count against the limit.

    A name at the limit surrounded by spaces is valid, because the stored form
    is what the column holds. Checking before the strip would reject it.
    """
    padded = "   " + "h" * HOST_NAME_MAX_LENGTH + "   "
    assert len(normalize_host_name(padded)) == HOST_NAME_MAX_LENGTH


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("homeserver", "HOMESERVER"),
        ("HomeServer", "homeserver"),
        ("Straße", "STRASSE"),
        ("backup nas", "Backup   NAS"),
    ],
)
def test_the_canonical_form_makes_variants_one_host(left: str, right: str) -> None:
    """Registering both spellings is an operator error, not two hosts.

    Two rows differing only in case would render identically in a list, and an
    operator would then be unable to tell which one their agent enrolled
    against.
    """
    assert canonical_host_name(left) == canonical_host_name(right)


# ---------------------------------------------------------------------------
# The name_canonical invariant
# ---------------------------------------------------------------------------


def test_the_constructor_derives_the_canonical_name() -> None:
    host = make_host(name="  HomeServer  ")

    assert host.name == "HomeServer"
    assert host.name_canonical == "homeserver"


def test_renaming_a_host_moves_the_canonical_name_with_it() -> None:
    """The two columns cannot drift.

    Without this, a rename would leave the old canonical value carrying the
    unique constraint: the host would still collide with its former name and
    not with its current one.
    """
    host = make_host(name="homeserver")

    host.name = "Backup NAS"

    assert host.name == "Backup NAS"
    assert host.name_canonical == "backup nas"


def test_an_invalid_rename_is_refused_at_assignment() -> None:
    host = make_host(name="homeserver")

    with pytest.raises(ValueError, match="control characters"):
        host.name = "bad\x00name"


def test_the_canonical_name_cannot_be_set_directly() -> None:
    host = make_host(name="homeserver")

    with pytest.raises(ValueError, match="derived from name"):
        host.name_canonical = ""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_a_new_host_is_registered_not_enrolled() -> None:
    """Creating a record must never imply an agent has claimed it.

    If the default were ``enrolled``, the interface would show a host as
    enrolled before any agent existed — and the operator would have no way to
    tell a real enrollment from a default.

    Asserted on the column rather than on an instance: SQLAlchemy applies
    column defaults at INSERT, not at construction. The value really arriving
    in the row is covered in ``test_models_host_integration.py``.
    """
    column = Base.metadata.tables["hosts"].c.lifecycle

    assert column.default.arg == HostLifecycle.REGISTERED.value  # type: ignore[union-attr]
    assert "registered" in str(column.server_default.arg)  # type: ignore[union-attr]


def test_column_defaults_are_not_applied_before_flush() -> None:
    # Stated so a test elsewhere cannot wrongly conclude a default is missing.
    # Read through getattr because the annotations say these are non-optional,
    # which is true of a persisted row and not of a pending one.
    host = make_host()
    defaulted = ("lifecycle", "is_enabled", "connection_epoch")

    assert [name for name in defaulted if getattr(host, name) is not None] == []


def test_a_lifecycle_enum_member_is_stored_as_its_value() -> None:
    host = make_host(lifecycle=HostLifecycle.ENROLLED)

    assert host.lifecycle == "enrolled"
    assert isinstance(host.lifecycle, str)


@pytest.mark.parametrize("value", ["bogus", "ENROLLED", "", "connected", 7, None])
def test_an_unknown_lifecycle_is_refused_at_assignment(value: object) -> None:
    """Caught at assignment, not at flush.

    A CHECK violation surfaces from the driver at commit, pointing at the
    session rather than at the line that set the bad value.
    """
    with pytest.raises(ValueError, match="invalid host lifecycle"):
        make_host(lifecycle=value)


def test_the_lifecycle_values_tuple_matches_the_enum() -> None:
    """The CHECK constraint and the migration are written from this tuple."""
    assert HOST_LIFECYCLE_VALUES == ("registered", "enrolled", "revoked")


# ---------------------------------------------------------------------------
# The lifecycle state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # An agent claims a fresh record.
        (HostLifecycle.REGISTERED, HostLifecycle.ENROLLED),
        # An administrator revokes the credential.
        (HostLifecycle.ENROLLED, HostLifecycle.REVOKED),
        # Re-enrollment after a revocation — a reinstalled machine.
        (HostLifecycle.REVOKED, HostLifecycle.ENROLLED),
        # Re-enrolling an already-enrolled host: the old credential is revoked
        # and a new one issued in one transaction, and the lifecycle does not
        # move. Permitted so that path does not have to special-case itself.
        (HostLifecycle.ENROLLED, HostLifecycle.ENROLLED),
    ],
)
def test_a_permitted_transition_is_accepted(current: HostLifecycle, target: HostLifecycle) -> None:
    assert check_lifecycle_transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target", "why"),
    [
        (
            HostLifecycle.REGISTERED,
            HostLifecycle.REVOKED,
            "there is no credential to revoke, so this would display as "
            "'access taken away' when nothing happened",
        ),
        (
            HostLifecycle.ENROLLED,
            HostLifecycle.REGISTERED,
            "'registered' means no agent has ever claimed this record",
        ),
        (
            HostLifecycle.REVOKED,
            HostLifecycle.REGISTERED,
            "that would erase the fact that an agent once held a credential",
        ),
        (
            HostLifecycle.REGISTERED,
            HostLifecycle.REGISTERED,
            "a no-op move signals a caller that has lost track of the state",
        ),
        (
            HostLifecycle.REVOKED,
            HostLifecycle.REVOKED,
            "revoking twice means the first revocation was not observed",
        ),
    ],
)
def test_a_forbidden_transition_is_refused(
    current: HostLifecycle, target: HostLifecycle, why: str
) -> None:
    assert why  # documentation, carried in the parameter list
    with pytest.raises(HostLifecycleError, match="cannot move from"):
        check_lifecycle_transition(current, target)


def test_the_refusal_names_what_is_permitted_instead() -> None:
    """So the caller's log says what to do, not merely that it failed."""
    with pytest.raises(HostLifecycleError, match="permitted from 'registered': enrolled"):
        check_lifecycle_transition(HostLifecycle.REGISTERED, HostLifecycle.REVOKED)


@pytest.mark.parametrize("bogus", ["connected", "", "ENROLLED"])
def test_an_unknown_state_in_a_transition_is_a_plain_value_error(bogus: str) -> None:
    """Not a ``HostLifecycleError``.

    The API maps ``HostLifecycleError`` to a conflict — "the host is not in a
    state where this is possible". An unknown state name is a programming
    error, and reporting it as a conflict would hide a bug behind a 409.
    """
    with pytest.raises(ValueError, match="is not a valid HostLifecycle") as caught:
        check_lifecycle_transition(bogus, HostLifecycle.ENROLLED)
    assert not isinstance(caught.value, HostLifecycleError)


def test_every_lifecycle_state_has_a_transition_row() -> None:
    """A state missing from the table would raise ``KeyError`` on any move."""
    assert set(HOST_LIFECYCLE_TRANSITIONS) == set(HostLifecycle)


# ---------------------------------------------------------------------------
# Connectivity derivation
#
# This is the function that decides whether the dashboard claims a host is up.
# ---------------------------------------------------------------------------


def test_a_host_that_never_connected_is_offline() -> None:
    assert (
        derive_connectivity(current_connection_id=None, last_heartbeat_at=None, now=NOW)
        is HostConnectivity.OFFLINE
    )


def test_a_cleanly_disconnected_host_is_offline_however_recent_its_heartbeat() -> None:
    """Clearing the connection is the controller's own assertion.

    It wrote that on a clean disconnect, so it is more trustworthy than the
    heartbeat timestamp, which is merely the last thing that arrived.
    """
    assert (
        derive_connectivity(
            current_connection_id=None, last_heartbeat_at=NOW - timedelta(seconds=1), now=NOW
        )
        is HostConnectivity.OFFLINE
    )


def test_a_recent_heartbeat_on_a_live_connection_is_connected() -> None:
    assert (
        derive_connectivity(
            current_connection_id=uuid.uuid4(),
            last_heartbeat_at=NOW - timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS),
            now=NOW,
        )
        is HostConnectivity.CONNECTED
    )


def test_a_single_missed_heartbeat_does_not_flap_the_status() -> None:
    """The grace period is three intervals, not one.

    A GC pause or a one-second network stall must not move a healthy host to
    stale, because a status that flaps is a status operators learn to ignore.
    """
    assert (
        derive_connectivity(
            current_connection_id=uuid.uuid4(),
            last_heartbeat_at=NOW - timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS * 2),
            now=NOW,
        )
        is HostConnectivity.CONNECTED
    )


def test_the_grace_boundary_is_inclusive() -> None:
    """Exactly at the grace period is still connected; one second past is not.

    Pinned because an off-by-one here is invisible in the interface and shows
    up only as a host that flips status every few minutes.
    """
    boundary = NOW - timedelta(seconds=HEARTBEAT_GRACE_SECONDS)
    connection = uuid.uuid4()

    assert (
        derive_connectivity(current_connection_id=connection, last_heartbeat_at=boundary, now=NOW)
        is HostConnectivity.CONNECTED
    )
    assert (
        derive_connectivity(
            current_connection_id=connection,
            last_heartbeat_at=boundary - timedelta(seconds=1),
            now=NOW,
        )
        is HostConnectivity.STALE
    )


def test_a_controller_crash_derives_as_stale_never_as_connected() -> None:
    """The failure this whole scheme exists to prevent.

    A stored ``is_connected`` boolean would still read ``true`` here, because
    the code that clears it never ran — and the dashboard would report healthy
    connectivity for a fleet connected to nothing.
    """
    assert (
        derive_connectivity(
            current_connection_id=uuid.uuid4(),
            last_heartbeat_at=NOW - timedelta(hours=9),
            now=NOW,
        )
        is HostConnectivity.STALE
    )


def test_a_connection_with_no_heartbeat_yet_resolves_towards_stale() -> None:
    """Connected is a claim that needs evidence."""
    assert (
        derive_connectivity(current_connection_id=uuid.uuid4(), last_heartbeat_at=None, now=NOW)
        is HostConnectivity.STALE
    )


def test_a_heartbeat_from_the_future_is_treated_as_recent_not_as_stale() -> None:
    """Clock skew on the host must not present as an outage.

    The controller stamps the heartbeat with its own clock, so this should not
    arise; if it ever does, the honest answer is "we heard from it", not an
    alert about a host that is fine.
    """
    assert (
        derive_connectivity(
            current_connection_id=uuid.uuid4(),
            last_heartbeat_at=NOW + timedelta(minutes=5),
            now=NOW,
        )
        is HostConnectivity.CONNECTED
    )


def test_the_grace_period_is_overridable_for_tests_and_configuration() -> None:
    assert (
        derive_connectivity(
            current_connection_id=uuid.uuid4(),
            last_heartbeat_at=NOW - timedelta(seconds=10),
            now=NOW,
            grace_seconds=5,
        )
        is HostConnectivity.STALE
    )


def test_the_host_method_agrees_with_the_function() -> None:
    host = make_host()
    host.current_connection_id = uuid.uuid4()
    host.last_heartbeat_at = NOW - timedelta(seconds=5)

    assert host.connectivity(NOW) is HostConnectivity.CONNECTED

    host.last_heartbeat_at = NOW - timedelta(hours=1)
    assert host.connectivity(NOW) is HostConnectivity.STALE


def test_connectivity_is_independent_of_administrative_state() -> None:
    """Two axes, not one overloaded field.

    A disabled host whose agent is still attached is connected *and* disabled,
    and the interface has to be able to say both — "disabled" is what the
    operator wants, "connected" is what is true.
    """
    host = make_host(is_enabled=False)
    host.current_connection_id = uuid.uuid4()
    host.last_heartbeat_at = NOW

    assert host.connectivity(NOW) is HostConnectivity.CONNECTED


def test_connectivity_is_independent_of_lifecycle() -> None:
    host = make_host(lifecycle=HostLifecycle.REVOKED)
    host.current_connection_id = uuid.uuid4()
    host.last_heartbeat_at = NOW

    assert host.connectivity(NOW) is HostConnectivity.CONNECTED


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_a_new_host_defaults_to_enabled_with_no_connection() -> None:
    """Again on the columns, because defaults apply at INSERT.

    ``is_enabled`` defaulting to true is the right way round: an operator who
    registers a host wants it to connect, and a default of false would make
    every enrollment fail with nothing to point at.
    """
    columns = Base.metadata.tables["hosts"].c

    assert columns.is_enabled.default.arg is True  # type: ignore[union-attr]
    assert columns.connection_epoch.default.arg == 0  # type: ignore[union-attr]
    # No default at all: a new host has no connection, and a default here
    # would mean inventing one.
    assert columns.current_connection_id.default is None
    assert columns.current_connection_id.nullable


def test_a_new_host_reports_no_agent_facts() -> None:
    """Null, not a placeholder.

    ``AGENT_ARCHITECTURE.md`` §9 and product principle 2: an unreported value
    must be distinguishable from a measured one, so these stay null until an
    agent actually says something.
    """
    host = make_host()

    assert host.agent_version is None
    assert host.protocol_version is None
    assert host.platform is None
    assert host.agent_started_at is None
    assert host.last_heartbeat_at is None
    assert host.last_connected_at is None
    assert host.last_disconnected_at is None


# ---------------------------------------------------------------------------
# Representations must not leak token material
# ---------------------------------------------------------------------------


def test_the_enrollment_representation_omits_the_digest() -> None:
    """The digest is not replayable, but it is the lookup key.

    Printing it would let anyone reading a log confirm whether a token they
    captured is the live one.
    """
    enrollment = HostEnrollment(
        host_id=uuid.uuid4(),
        token_hash=FAKE_DIGEST,
        expires_at=NOW + timedelta(minutes=15),
    )

    text = repr(enrollment)

    assert FAKE_DIGEST not in text
    assert "token" not in text.lower()
    # Still useful for debugging: the state is there without the secret.
    assert "consumed=False" in text
    assert "revoked=False" in text


def test_the_credential_representation_omits_the_digest() -> None:
    credential = AgentCredential(host_id=uuid.uuid4(), token_hash=FAKE_DIGEST)

    text = repr(credential)

    assert FAKE_DIGEST not in text
    assert "token" not in text.lower()
    assert "revoked=False" in text


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["hosts", "host_enrollments", "agent_credentials"])
def test_every_host_table_is_registered_on_the_metadata(table: str) -> None:
    # If this fails, hcc/models/__init__.py no longer imports the module and
    # Alembic autogenerate would emit a drop for the table.
    assert table in Base.metadata.tables


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        (
            "hosts",
            (
                "created_at",
                "updated_at",
                "last_connected_at",
                "last_disconnected_at",
                "last_heartbeat_at",
                "agent_started_at",
            ),
        ),
        ("host_enrollments", ("created_at", "expires_at", "consumed_at", "revoked_at")),
        ("agent_credentials", ("created_at", "revoked_at", "last_used_at")),
    ],
)
def test_timestamps_are_timezone_aware(table: str, columns: tuple[str, ...]) -> None:
    """A naive column silently loses the offset.

    "When did we last hear from this host" then becomes unanswerable across a
    DST boundary, and an expiry compared against a naive timestamp either
    raises or is wrong by the offset — a token that outlives its expiry.
    """
    for name in columns:
        column_type = Base.metadata.tables[table].c[name].type
        assert isinstance(column_type, DateTime), f"{table}.{name} must be a DateTime"
        assert column_type.timezone, f"{table}.{name} must be timezone-aware"


@pytest.mark.parametrize("table", ["hosts", "host_enrollments", "agent_credentials"])
def test_ids_are_uuid_primary_keys_generated_in_python(table: str) -> None:
    column = Base.metadata.tables[table].c.id

    assert column.primary_key
    assert isinstance(column.default.arg(None), uuid.UUID)  # type: ignore[union-attr]


@pytest.mark.parametrize("table", ["host_enrollments", "agent_credentials"])
def test_a_secret_bearing_table_stores_a_digest_of_the_expected_width(table: str) -> None:
    """Narrow enough that a plaintext token could not fit.

    The width is not cosmetic: a 64-character column cannot hold the 86-plus
    characters of a base64url token, so a bug that tried to store the secret
    itself fails at the database instead of succeeding quietly.
    """
    column = Base.metadata.tables[table].c.token_hash

    assert isinstance(column.type, String)
    assert column.type.length == TOKEN_HASH_LENGTH
    assert not column.nullable


@pytest.mark.parametrize("table", ["host_enrollments", "agent_credentials"])
def test_no_column_could_hold_a_plaintext_token(table: str) -> None:
    """A guard on the shape of the table, not on one code path.

    Every path that writes a token has to go through one of these columns, so
    "there is nowhere to put it" is a stronger statement than "no current
    caller puts it there" — which is what a review of the call sites would
    give.
    """
    for column in Base.metadata.tables[table].c:
        name = column.name
        assert "token" not in name or name == "token_hash", (
            f"{table}.{name} looks like it could hold token material"
        )
        assert "secret" not in name
        assert "plaintext" not in name


def test_the_host_representation_carries_the_state_an_operator_needs() -> None:
    host = make_host(name="homeserver", lifecycle=HostLifecycle.ENROLLED, is_enabled=False)

    text = repr(host)

    assert "homeserver" in text
    assert "enrolled" in text
    assert "is_enabled=False" in text
