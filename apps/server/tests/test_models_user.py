"""User model unit tests.

No database. These cover the model's Python-level behaviour: username
normalization, the ``username_canonical`` invariant, role coercion, hash
validation, and that the representation does not leak the hash.

Database-enforced behaviour (uniqueness, NOT NULL, CHECK constraints, server
defaults, the ``updated_at`` trigger) is covered in
``test_models_user_integration.py`` against real PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import DateTime, Enum, UniqueConstraint

from hcc.db import Base
from hcc.models import User, UserRole, normalize_username
from hcc.models.user import (
    PASSWORD_HASH_MAX_LENGTH,
    USER_ROLE_ENUM_NAME,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
)

# A realistically shaped Argon2id hash. Not a real credential.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHR2YWx1ZQ$" + "a" * 43


def make_user(**overrides: object) -> User:
    """Build a valid User, overriding individual fields."""
    values: dict[str, object] = {
        "username": "test-user",
        "password_hash": FAKE_HASH,
    }
    values.update(overrides)
    return User(**values)


# ---------------------------------------------------------------------------
# normalize_username
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("admin", "admin"),
        ("Admin", "admin"),
        ("ADMIN", "admin"),
        ("  admin  ", "admin"),
        ("\tadmin\n", "admin"),
        # U+FF21.. FULLWIDTH LATIN "ADMIN". NFKC folds full-width Latin onto
        # ASCII, so a full-width lookalike cannot coexist with the real name.
        ("\uff21\uff24\uff2d\uff29\uff2e", "admin"),
        # casefold, unlike lower(), expands the German sharp s.
        ("STRAßE", "strasse"),
        ("Straße", "strasse"),
        # Accents are preserved: they are a genuine distinction, not a fold.
        ("Félix", "félix"),
        ("felix.staude", "felix.staude"),
        ("user_123", "user_123"),
    ],
)
def test_normalize_username_canonical_forms(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


def test_normalize_username_is_idempotent() -> None:
    # Applying normalization to an already-normalized value must not change
    # it, or a stored canonical value could stop matching a freshly computed
    # one and uniqueness would silently break.
    for raw in [
        "Admin",
        "\uff21\uff24\uff2d\uff29\uff2e",  # fullwidth ADMIN
        "STRAßE",
        "Félix",
        "  Mixed-Case  ",
    ]:
        once = normalize_username(raw)
        assert normalize_username(once) == once


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\t\n", "\u00a0\u00a0\u00a0"],
)
def test_normalize_username_rejects_blank(raw: str) -> None:
    with pytest.raises(ValueError, match=r"empty|at least"):
        normalize_username(raw)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        # Written as escapes on purpose: these characters are invisible, so a
        # literal here would be unreviewable in a diff.
        ("ad min", "whitespace"),
        # U+00A0 NO-BREAK SPACE. NFKC maps it to a plain space, so without the
        # whitespace check this would normalize to "ad min" and collide with a
        # deliberately registered "ad min".
        ("ad\u00a0min", "whitespace"),
        ("ad\tmin", "whitespace"),
        # U+200B ZERO WIDTH SPACE and U+200E LEFT-TO-RIGHT MARK are invisible:
        # two distinct canonical names would render identically.
        ("ad\u200bmin", "control characters"),
        ("admin\u200e", "control characters"),
        # U+0000 NUL, which must never reach a text column.
        ("ad\x00min", "control characters"),
    ],
)
def test_normalize_username_rejects_confusable_characters(raw: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        normalize_username(raw)


def test_normalize_username_enforces_minimum_length_after_normalization() -> None:
    # Length is checked on the canonical form, so padding cannot smuggle a
    # too-short name through.
    with pytest.raises(ValueError, match="at least"):
        normalize_username("  ab  ")


def test_normalize_username_enforces_maximum_length() -> None:
    assert len(normalize_username("x" * USERNAME_MAX_LENGTH)) == USERNAME_MAX_LENGTH
    with pytest.raises(ValueError, match="at most"):
        normalize_username("x" * (USERNAME_MAX_LENGTH + 1))


def test_normalize_username_boundary_lengths_are_accepted() -> None:
    assert normalize_username("x" * USERNAME_MIN_LENGTH)
    assert normalize_username("x" * USERNAME_MAX_LENGTH)


def test_normalize_username_length_is_measured_after_casefold_expansion() -> None:
    # "ß" casefolds to "ss", so a 32-character input can exceed the limit.
    # Measuring before folding would let an over-long value reach the column.
    raw = "ß" * USERNAME_MAX_LENGTH
    with pytest.raises(ValueError, match="at most"):
        normalize_username(raw)


# ---------------------------------------------------------------------------
# username / username_canonical invariant
# ---------------------------------------------------------------------------


def test_assigning_username_derives_canonical() -> None:
    user = make_user(username="FelixStaude")

    assert user.username == "FelixStaude"
    assert user.username_canonical == "felixstaude"


def test_username_display_form_is_stripped_but_case_preserved() -> None:
    user = make_user(username="  FelixStaude  ")

    assert user.username == "FelixStaude"
    assert user.username_canonical == "felixstaude"


def test_reassigning_username_updates_canonical() -> None:
    # The invariant must hold after mutation, not only at construction.
    user = make_user(username="first-name")
    assert user.username_canonical == "first-name"

    user.username = "SecondName"

    assert user.username == "SecondName"
    assert user.username_canonical == "secondname"


def test_invalid_username_raises_at_assignment_not_at_flush() -> None:
    # Failing here keeps the traceback pointing at the offending code rather
    # than at the session, which is where a flush-time error would surface.
    with pytest.raises(ValueError, match="at least"):
        make_user(username="ab")

    user = make_user()
    with pytest.raises(ValueError, match="whitespace"):
        user.username = "has space"


def test_direct_canonical_assignment_of_blank_is_rejected() -> None:
    user = make_user()
    with pytest.raises(ValueError, match="derived from username"):
        user.username_canonical = ""


# ---------------------------------------------------------------------------
# role
# ---------------------------------------------------------------------------


def test_role_defaults_are_not_applied_before_flush() -> None:
    # Column defaults are applied by SQLAlchemy at INSERT, not at construction.
    # Asserting this prevents a test elsewhere from wrongly concluding the
    # default is missing.
    assert make_user().role is None


@pytest.mark.parametrize("role", list(UserRole))
def test_every_role_is_assignable(role: UserRole) -> None:
    assert make_user(role=role).role is role


@pytest.mark.parametrize(
    ("value", "expected"),
    [("viewer", UserRole.VIEWER), ("operator", UserRole.OPERATOR), ("admin", UserRole.ADMIN)],
)
def test_role_strings_are_coerced_to_the_enum(value: str, expected: UserRole) -> None:
    assert make_user(role=value).role is expected


@pytest.mark.parametrize("value", ["superuser", "ADMIN", "Admin", "", "root", "owner"])
def test_unknown_role_is_rejected_with_a_readable_error(value: str) -> None:
    # Including the permitted values in the message means the caller does not
    # have to go and read the enum.
    with pytest.raises(ValueError, match="invalid role") as exc_info:
        make_user(role=value)

    message = str(exc_info.value)
    for permitted in UserRole:
        assert permitted.value in message


def test_role_values_are_lowercase_strings() -> None:
    # The database enum stores values, not member names. If a member's value
    # were uppercased, the stored data would disagree with the API.
    for role in UserRole:
        assert role.value == role.value.lower()


def test_user_role_is_a_closed_set_of_three() -> None:
    # Guards the authorization model: adding a role must be a deliberate
    # change that updates this test and ships a migration.
    assert [role.value for role in UserRole] == ["viewer", "operator", "admin"]


# ---------------------------------------------------------------------------
# password_hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_password_hash_is_rejected(value: str) -> None:
    # A row with an empty hash is a security hole if any later comparison
    # treats "" as a match.
    with pytest.raises(ValueError, match="non-empty"):
        make_user(password_hash=value)


def test_overlong_password_hash_is_rejected() -> None:
    with pytest.raises(ValueError, match="at most"):
        make_user(password_hash="a" * (PASSWORD_HASH_MAX_LENGTH + 1))


def test_realistic_argon2id_hash_fits_the_column() -> None:
    assert len(FAKE_HASH) <= PASSWORD_HASH_MAX_LENGTH
    assert make_user(password_hash=FAKE_HASH).password_hash == FAKE_HASH


# ---------------------------------------------------------------------------
# representation
# ---------------------------------------------------------------------------


def test_repr_omits_the_password_hash() -> None:
    # These objects reach log lines and exception context, and a hash is
    # offline-crackable material.
    user = make_user(username="felix", role=UserRole.ADMIN, password_hash=FAKE_HASH)

    rendered = repr(user)

    assert FAKE_HASH not in rendered
    assert "argon2" not in rendered
    assert "password" not in rendered


def test_repr_includes_identifying_fields() -> None:
    user = make_user(username="felix", role=UserRole.ADMIN)
    rendered = repr(user)

    assert "felix" in rendered
    assert "admin" in rendered.lower()


# ---------------------------------------------------------------------------
# table definition
# ---------------------------------------------------------------------------


def test_users_table_is_registered_on_the_metadata() -> None:
    # If this fails, hcc/models/__init__.py no longer imports the module and
    # Alembic autogenerate would emit a drop for the table.
    assert "users" in Base.metadata.tables


def test_id_is_a_uuid_primary_key_generated_in_python() -> None:
    column = Base.metadata.tables["users"].c.id

    assert column.primary_key
    assert isinstance(column.default.arg(None), uuid.UUID)  # type: ignore[union-attr]


def test_timestamps_are_timezone_aware() -> None:
    # A naive timestamp column silently loses the offset, which makes
    # "when did this happen" unanswerable across a DST boundary. The
    # isinstance check also asserts the column really is a DateTime.
    table = Base.metadata.tables["users"]
    for name in ("created_at", "updated_at", "last_login_at"):
        column_type = table.c[name].type
        assert isinstance(column_type, DateTime), f"{name} must be a DateTime"
        assert column_type.timezone is True, f"{name} must be timezone-aware"


def test_only_the_genuinely_optional_columns_are_nullable() -> None:
    """Pin the nullable set, so a new nullable column is a deliberate choice.

    Both members earn it by representing a state that genuinely has no value,
    rather than by being convenient to leave empty:

    * ``last_login_at`` — null means "has never signed in", which must not be
      rendered as a date.
    * ``display_name`` — null means "no separate label", so callers render the
      username. Distinct from the empty string, which a CHECK constraint
      forbids precisely so the two states cannot be confused (milestone 1.7).

    Everything else is NOT NULL, and a new nullable column should have to
    justify itself here before it exists.
    """
    nullable = {column.name for column in Base.metadata.tables["users"].columns if column.nullable}
    assert nullable == {"last_login_at", "display_name"}


def test_role_column_uses_the_named_postgres_enum() -> None:
    # The migration drops this type by name on downgrade; a rename here
    # without updating the migration would break the downgrade.
    column_type = Base.metadata.tables["users"].c.role.type
    assert isinstance(column_type, Enum)
    assert column_type.name == USER_ROLE_ENUM_NAME


def _unique_column_groups() -> set[tuple[str, ...]]:
    """Column tuples covered by a UniqueConstraint on the users table."""
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["users"].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_username_canonical_is_unique() -> None:
    assert ("username_canonical",) in _unique_column_groups()


def test_username_itself_is_not_unique() -> None:
    # Uniqueness belongs to the canonical column. A unique constraint on the
    # display form would wrongly permit "Admin" alongside "admin".
    assert ("username",) not in _unique_column_groups()
