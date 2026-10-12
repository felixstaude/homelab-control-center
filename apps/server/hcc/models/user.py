"""User account model.

Phase 1, milestone 1.1, extended in 1.7 with a mutable display name.

This defines the schema only. Credential verification lives in
:mod:`hcc.security.authentication`, hashing in :mod:`hcc.security.password`,
and the administrative rules that govern changes to these rows in
:mod:`hcc.services.users`.

## Username normalization and uniqueness

Two columns, by design:

* ``username`` preserves exactly what the user typed, so the UI can show
  ``FelixStaude`` rather than ``felixstaude``.
* ``username_canonical`` holds the normalized form and carries the unique
  constraint.

Normalization is NFKC → strip → casefold → NFKC (see :func:`normalize_username`).
This makes ``Admin``, ``admin`` and ``ADMIN`` the same account, which is what
users expect and what prevents an attacker registering a visually
indistinguishable near-duplicate of an existing name.

``username_canonical`` is maintained by a SQLAlchemy ``@validates`` hook on
``username``, so the two can never drift: assigning ``username`` always
recomputes it. This is an application-level invariant rather than a database
generated column because PostgreSQL generated columns may only call immutable
SQL functions, and neither NFKC normalization nor Unicode casefolding is
available in SQL — ``lower()`` alone would be a weaker rule. The control plane
is the only writer to this database (ADR-003), so the application is the right
place to enforce it. See ADR-014.

## The username is immutable; the display name is not

Milestone 1.7 adds ``display_name`` and deliberately leaves ``username``
unchangeable through the API. The two roles were conflated before: the only
human-readable label an account had was the thing it logs in with, so
correcting a typo in someone's name meant changing their credential.

Separating them is what makes "rename a user" a safe operation. Renaming the
*username* is not, and is not offered — see
ADR-041. A username appears in the login throttle's budget keys, in every
structured log line about that account, and in the audit trail; changing it
would silently re-point all three, and a freed-up canonical name could then be
registered by somebody else and inherit the old name's history by eye.

``display_name`` is nullable, and null means "this account has no separate
label" rather than "unknown". Callers render the username in that case, which
is why there is no empty-string state: a blank submission clears the column
back to null rather than storing whitespace that would render as a gap.

## Passwords

Only hashes are stored, never plaintext. This module does not hash anything —
the algorithm is Argon2id, per :mod:`hcc.security.password`.
``password_hash`` is NOT NULL with a non-empty CHECK constraint, so a row can
never exist with an empty hash that some comparison might accidentally accept.
"""

from __future__ import annotations

import enum
import unicodedata
import uuid

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy resolves
# `Mapped[...]` annotations at class-definition time to derive column types, so
# a type-checking-only import makes mapping fail outright. Same constraint as
# FastAPI's dependency annotations — see hcc/api/deps.py.
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    FetchedValue,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from hcc.db import Base


class UserRole(enum.StrEnum):
    """Roles a user account may hold.

    A closed set matching the authorization model in docs/ARCHITECTURE.md §4.3.
    Adding a member requires a migration (``ALTER TYPE user_role ADD VALUE``),
    which is intentional: a new role changes the authorization surface and
    should be a reviewed change rather than a configuration edit. See ADR-013.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


#: Username length bounds, enforced in Python and by a CHECK constraint.
#: The lower bound rejects trivially short names; the upper bound keeps the
#: column narrow enough to index comfortably.
USERNAME_MIN_LENGTH: Final = 3
USERNAME_MAX_LENGTH: Final = 32

#: Upper bound on a display name. Generous enough for a full name with
#: honorifics, narrow enough that the column stays cheap and a rendered table
#: cell stays predictable.
DISPLAY_NAME_MAX_LENGTH: Final = 64

#: Upper bound for the stored hash. An Argon2id encoded hash at the parameters
#: docs/SECURITY.md specifies is around 100 characters; 255 leaves room to
#: change parameters, or algorithm, without a migration.
PASSWORD_HASH_MAX_LENGTH: Final = 255

#: Name of the PostgreSQL enum type backing :class:`UserRole`. Referenced by
#: the migration, which must drop the type explicitly on downgrade.
USER_ROLE_ENUM_NAME: Final = "user_role"

#: Persist the enum's *values* (``"viewer"``) rather than its member names
#: (``"VIEWER"``). SQLAlchemy stores names by default, which would put
#: uppercase strings in the database that disagree with the API representation.
USER_ROLE_ENUM = Enum(
    UserRole,
    name=USER_ROLE_ENUM_NAME,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    native_enum=True,
    create_constraint=False,
)


def fold_username(raw: str) -> str:
    """Apply the canonical folding to ``raw``, without validating the result.

    The four normalization steps :func:`normalize_username` documents, and
    none of its rejections. Split out in milestone 1.7 for the one caller that
    legitimately needs the folding without the policy: the administrative user
    *search*, which folds an operator's partial query so that typing ``Ad``
    matches the stored canonical ``admin``.

    Search must not inherit the policy, because a search term is not a
    username. ``normalize_username`` rejects anything under three characters
    and anything containing a space, so a one-letter query would raise and a
    search for ``van der`` would raise — turning a perfectly reasonable
    keystroke into an error. Folding alone is the part that makes the
    comparison correct; the rejections exist to keep bad names out of the
    table, which a read has no business doing.

    Returns a possibly-empty string. The caller decides what that means.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive, typed as str
        raise TypeError(f"username must be a string, got {type(raw).__name__}")

    folded = unicodedata.normalize("NFKC", raw).strip().casefold()
    return unicodedata.normalize("NFKC", folded)


def normalize_display_name(raw: str | None) -> str | None:
    """Return a stored display name, or ``None`` for "no display name".

    NFKC and a strip, mirroring the username's folding but **without**
    casefolding: a display name is meant to be shown as its owner writes it,
    so collapsing case here would be the same mistake as storing only the
    canonical username (ADR-014).

    An absent, empty or whitespace-only value becomes ``None``. That mapping
    is the reason the column has no empty-string state: an administrator who
    clears the field means "this account has no separate label", and storing
    ``""`` would instead render as a blank cell indistinguishable from a bug.

    Interior whitespace *is* allowed — this is a human name, and "Felix
    Staude" is the normal case. Control and format characters are not: they
    are invisible, so they could make two different names render identically
    in a user list an administrator is relying on to tell accounts apart.

    Raises:
        ValueError: if the result exceeds :data:`DISPLAY_NAME_MAX_LENGTH` or
            contains control characters. Raised rather than truncated, for the
            same reason passwords are never truncated: silently storing
            something other than what was submitted is worse than refusing it.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):  # pragma: no cover - defensive, typed as str
        raise TypeError(f"display_name must be a string, got {type(raw).__name__}")

    candidate = unicodedata.normalize("NFKC", raw).strip()
    if not candidate:
        return None

    for character in candidate:
        # Cc control, Cf format, Cs surrogate, Co private use, Cn unassigned.
        # Space is category Zs and so is deliberately not caught here.
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"display name must not contain control characters (found U+{ord(character):04X})"
            )
    if len(candidate) > DISPLAY_NAME_MAX_LENGTH:
        raise ValueError(
            f"display name must be at most {DISPLAY_NAME_MAX_LENGTH} characters, "
            f"got {len(candidate)}"
        )
    return candidate


def normalize_username(raw: str) -> str:
    """Return the canonical form of a username.

    Steps, in order:

    1. **NFKC** — folds compatibility variants onto a single representation,
       so a full-width rendering of ``admin`` (U+FF41 U+FF44 ...) cannot
       coexist with the ASCII spelling. Applied first because it also maps
       exotic whitespace (such as U+00A0) to a plain space, which step 2
       can then remove.
    2. **strip** — removes surrounding whitespace.
    3. **casefold** — aggressive, locale-independent lowercasing. Preferred
       over ``str.lower()`` because it handles cases ``lower()`` misses, such
       as German ``ß`` → ``ss``.
    4. **NFKC again** — casefolding can produce unnormalized output, so this
       guarantees the result is itself normalized and that the function is
       idempotent.

    Interior whitespace and control characters are then rejected outright.
    Step 1 maps exotic spaces onto U+0020, so without this check a name
    containing a non-breaking space would normalize to one that looks
    identical to a different account's name — precisely the confusable the
    normalization is meant to prevent. Control and format characters are
    rejected for the same reason: they are invisible and would make two
    distinct canonical names render identically.

    Raises:
        ValueError: if the result is empty, contains whitespace or control
            characters, or is outside the permitted length. Raised rather than
            returned so a caller cannot accidentally persist an invalid name.
    """
    canonical = fold_username(raw)

    if not canonical:
        raise ValueError("username must not be empty or whitespace only")

    for character in canonical:
        if character.isspace():
            raise ValueError("username must not contain whitespace")
        # Cc control, Cf format, Cs surrogate, Co private use, Cn unassigned.
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"username must not contain control characters (found U+{ord(character):04X})"
            )
    if len(canonical) < USERNAME_MIN_LENGTH:
        raise ValueError(
            f"username must be at least {USERNAME_MIN_LENGTH} characters "
            f"after normalization, got {len(canonical)}"
        )
    if len(canonical) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"username must be at most {USERNAME_MAX_LENGTH} characters "
            f"after normalization, got {len(canonical)}"
        )
    return canonical


class User(Base):
    """A user account.

    Timestamps are timezone-aware (``TIMESTAMPTZ``) and maintained by the
    database, not by the application — see ADR-015. ``created_at`` and
    ``updated_at`` both default to ``now()``; ``updated_at`` is additionally
    maintained by a trigger, so it is correct even for a bulk ``UPDATE`` that
    never passes through the ORM.
    """

    __tablename__ = "users"

    __table_args__ = (
        # Case-insensitive uniqueness. Backed by an index, so it also serves
        # the login lookup (`WHERE username_canonical = ...`) that milestone
        # 1.2 will need.
        UniqueConstraint("username_canonical", name="uq_users_username_canonical"),
        CheckConstraint(
            f"char_length(username) BETWEEN {USERNAME_MIN_LENGTH} AND {USERNAME_MAX_LENGTH}",
            name="username_length",
        ),
        CheckConstraint(
            "char_length(username_canonical) BETWEEN "
            f"{USERNAME_MIN_LENGTH} AND {USERNAME_MAX_LENGTH}",
            name="username_canonical_length",
        ),
        # A row with an empty hash must be impossible: a later comparison
        # against an empty string is the kind of bug that silently grants
        # access.
        CheckConstraint("char_length(password_hash) > 0", name="password_hash_not_empty"),
        # NULL passes a CHECK, so this bounds the length *and* forbids the
        # empty string without forbidding "no display name". Those are
        # different states and the application maps a blank submission onto
        # the NULL one — see `normalize_display_name`.
        CheckConstraint(
            f"display_name IS NULL OR char_length(display_name) BETWEEN 1 "
            f"AND {DISPLAY_NAME_MAX_LENGTH}",
            name="display_name_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        # Generated in Python rather than by the database so that application
        # code holds the id before the row is flushed, which makes it usable
        # for related inserts in the same unit of work. See ADR-016.
        default=uuid.uuid4,
    )

    username: Mapped[str] = mapped_column(
        String(USERNAME_MAX_LENGTH),
        nullable=False,
        doc="Display form, exactly as the user entered it.",
    )

    username_canonical: Mapped[str] = mapped_column(
        String(USERNAME_MAX_LENGTH),
        nullable=False,
        doc=(
            "Normalized form used for uniqueness and lookup. Derived from "
            "`username`; never assign this directly."
        ),
    )

    display_name: Mapped[str | None] = mapped_column(
        String(DISPLAY_NAME_MAX_LENGTH),
        nullable=True,
        doc=(
            "Human-readable label, independent of the login name. Null means "
            "the account has no separate label and callers should render the "
            "username; it never means 'unknown'."
        ),
    )

    password_hash: Mapped[str] = mapped_column(
        String(PASSWORD_HASH_MAX_LENGTH),
        nullable=False,
        doc="Argon2id hash. Never a plaintext password.",
    )

    role: Mapped[UserRole] = mapped_column(
        USER_ROLE_ENUM,
        nullable=False,
        default=UserRole.VIEWER,
        # Least privilege: a row inserted without an explicit role is a
        # viewer, never an admin.
        server_default=text(f"'{UserRole.VIEWER.value}'::{USER_ROLE_ENUM_NAME}"),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc=(
            "Whether the account may authenticate. Deactivation is preferred "
            "over deletion so that audit records keep referring to a real row."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        # Tells SQLAlchemy the database changes this column on UPDATE, so the
        # ORM expires its cached value instead of serving a stale one. The
        # trigger does the actual work.
        server_onupdate=FetchedValue(),
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "Null until the account authenticates for the first time. Null "
            "means 'never logged in' and must not be rendered as a date."
        ),
    )

    @validates("username")
    def _normalize_and_sync_canonical(self, _key: str, value: str) -> str:
        """Keep ``username_canonical`` in step with ``username``.

        Runs on every assignment, including in the constructor, so the two
        columns cannot drift. Validation errors surface here — at assignment —
        rather than at flush time, where the traceback would point at the
        session instead of the offending code.
        """
        canonical = normalize_username(value)
        stripped = unicodedata.normalize("NFKC", value).strip()
        self.username_canonical = canonical
        return stripped

    @validates("username_canonical")
    def _reject_direct_canonical_assignment(self, _key: str, value: str) -> str:
        """Allow only the value derived from ``username``.

        Guards the invariant from the other direction: setting
        ``username_canonical`` by hand would silently break case-insensitive
        uniqueness for that row.
        """
        if not isinstance(value, str) or not value:
            raise ValueError("username_canonical is derived from username; set username instead")
        return value

    @validates("display_name")
    def _normalize_display_name(self, _key: str, value: str | None) -> str | None:
        """Normalize on every assignment, so the column is never raw input.

        Mirrors the ``username`` hook: validation surfaces where the value was
        set rather than at flush time, where the traceback would point at the
        session instead of the offending code. It also means the blank-to-null
        mapping holds for every writer, not only the service layer.
        """
        return normalize_display_name(value)

    @property
    def label(self) -> str:
        """What to call this account in prose.

        The display name when there is one, the username otherwise. A single
        accessor so that callers cannot each invent their own fallback and
        disagree about whether a blank name renders as an empty string.
        """
        return self.display_name or self.username

    @validates("role")
    def _validate_role(self, _key: str, value: Any) -> UserRole:
        """Coerce and validate the role at assignment time.

        Without this, an unknown string would only fail at flush, as an opaque
        driver error. ``UserRole(value)`` raises a readable ``ValueError``
        naming the bad value.
        """
        if isinstance(value, UserRole):
            return value
        try:
            return UserRole(value)
        except ValueError as exc:
            permitted = ", ".join(member.value for member in UserRole)
            raise ValueError(f"invalid role {value!r}; permitted roles are: {permitted}") from exc

    @validates("password_hash")
    def _validate_password_hash(self, _key: str, value: str) -> str:
        """Reject an empty hash before it reaches the database."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("password_hash must be a non-empty string")
        if len(value) > PASSWORD_HASH_MAX_LENGTH:
            raise ValueError(
                f"password_hash must be at most {PASSWORD_HASH_MAX_LENGTH} characters, "
                f"got {len(value)}"
            )
        return value

    def __repr__(self) -> str:
        """Return a debug representation with the password hash omitted.

        The hash is deliberately absent: these objects end up in log lines and
        exception context, and a password hash is offline-crackable material.
        Mirrors the redaction convention in the agent's config type.
        """
        return (
            f"User(id={self.id!r}, username={self.username!r}, "
            f"role={self.role!r}, is_active={self.is_active!r})"
        )
