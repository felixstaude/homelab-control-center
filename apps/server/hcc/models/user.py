"""User account model.

Phase 1, milestone 1.1. This defines the schema only. Authentication,
sessions, registration and password reset are later milestones and are
deliberately absent — this module contains no credential verification and no
hashing, just the shape of a stored account.

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

## Passwords

Only hashes are stored, never plaintext. This module does not hash anything —
the algorithm (Argon2id, per docs/SECURITY.md) arrives with milestone 1.2.
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
    if not isinstance(raw, str):  # pragma: no cover - defensive, typed as str
        raise TypeError(f"username must be a string, got {type(raw).__name__}")

    folded = unicodedata.normalize("NFKC", raw).strip().casefold()
    canonical = unicodedata.normalize("NFKC", folded)

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
