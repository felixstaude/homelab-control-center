"""Server-side session model.

Phase 1, milestone 1.3. Sessions are **server-side state**: the cookie carries
an opaque random token and nothing else, and every authorization decision is
made from this table.

## Why not JWT

The architecture does not call for it, and for this product a stateless token
is the wrong trade. The deciding property is **revocation**: logging out, or
disabling a user, must take effect immediately
(``docs/AGENT_ARCHITECTURE.md`` makes the same promise for agent credentials).
A self-contained JWT is valid until it expires, so immediate revocation
requires a server-side deny-list — at which point every request consults the
database anyway and the statelessness has bought nothing while adding
signature verification, key rotation and a much larger footgun surface.

Stateless tokens pay off at a scale this product explicitly does not target:
many services verifying tokens without a shared datastore. Here there is one
control plane and one database (ADR-001), so a session row is simpler, smaller
and revocable. See ADR-028.

## What is stored

Only the **SHA-256 hash** of the token, never the token itself. A database
dump, backup or leaked log therefore yields nothing replayable. See
:mod:`hcc.security.tokens` for why SHA-256 rather than Argon2.

The CSRF token *is* stored in plaintext, deliberately. It is not a credential
on its own: an attacker holding it can do nothing without the session cookie,
which is hashed. Storing it plaintext is what lets the server hand the same
value back to a legitimate client on ``/auth/me``, which is how the
double-submit check works at all. See ADR-030.

## Expiry has two independent bounds

* ``expires_at`` — absolute. A session dies this long after being created, no
  matter how active it was. Bounds the damage from a stolen cookie.
* ``last_seen_at`` + an idle timeout — relative. A session abandoned on a
  shared machine stops working without the user having to log out.

Both are enforced on every lookup, and either can expire a session. The idle
timeout is configurable and can be switched off; the absolute lifetime cannot.
"""

from __future__ import annotations

import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. See hcc/models/user.py.
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hcc.db import Base
from hcc.models.user import User
from hcc.security.tokens import TOKEN_HASH_LENGTH, TOKEN_LENGTH

#: Hex SHA-256 is fixed-width, so the column is too.
TOKEN_HASH_COLUMN_LENGTH: Final = TOKEN_HASH_LENGTH

#: The CSRF token is stored in its encoded form.
CSRF_TOKEN_COLUMN_LENGTH: Final = TOKEN_LENGTH

#: User agents are recorded for the session list a later milestone will show,
#: truncated because the header is attacker-controlled and unbounded.
USER_AGENT_MAX_LENGTH: Final = 256


class Session(Base):
    """An authenticated browser session.

    A row exists from login until logout, revocation or expiry. Rows are kept
    after expiry so that a later audit can answer "when did this session end,
    and why" rather than finding the evidence deleted; a maintenance job prunes
    them past a retention window.
    """

    __tablename__ = "sessions"

    __table_args__ = (
        # The lookup on every authenticated request is by token hash, so this
        # index is the hot path, and uniqueness makes a hash collision or a
        # double-insert a database error rather than an ambiguous query.
        Index("uq_sessions_token_hash", "token_hash", unique=True),
        # Listing or revoking a user's sessions filters by user.
        Index("ix_sessions_user_id", "user_id"),
        # The pruning job scans by expiry.
        Index("ix_sessions_expires_at", "expires_at"),
        CheckConstraint(
            f"char_length(token_hash) = {TOKEN_HASH_COLUMN_LENGTH}",
            name="token_hash_is_sha256_hex",
        ),
        CheckConstraint(
            f"char_length(csrf_token) = {CSRF_TOKEN_COLUMN_LENGTH}",
            name="csrf_token_length",
        ),
        # A session that expires before it was created is nonsense, and would
        # most likely mean a clock or arithmetic bug.
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        # Generated in Python, as elsewhere (ADR-016).
        default=uuid.uuid4,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # Deleting a user removes their sessions: a session pointing at a
        # deleted user could otherwise authenticate a principal that no longer
        # exists. Deactivation, not deletion, is the normal path — see
        # `User.is_active`.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(TOKEN_HASH_COLUMN_LENGTH),
        nullable=False,
        doc="Hex SHA-256 of the session token. The token itself is never stored.",
    )

    csrf_token: Mapped[str] = mapped_column(
        String(CSRF_TOKEN_COLUMN_LENGTH),
        nullable=False,
        doc=(
            "Double-submit CSRF token, stored in plaintext because it must be "
            "handed back to the client. Useless without the session cookie."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Absolute expiry. Computed at creation; never extended.",
    )

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc=(
            "Last time this session was used. Updated at most once per "
            "configured interval, so an active session does not cause a "
            "database write on every request."
        ),
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When the session was explicitly revoked, by logout or by an "
            "administrator. Null means never revoked. The row is kept so that "
            "the reason a session stopped working remains answerable."
        ),
    )

    created_ip: Mapped[str | None] = mapped_column(
        String(45),  # an IPv6 address with a zone index fits
        nullable=True,
        doc="Client address at login, for the session list and for audit.",
    )

    created_user_agent: Mapped[str | None] = mapped_column(
        String(USER_AGENT_MAX_LENGTH),
        nullable=True,
        doc="Client user agent at login, truncated. Attacker-controlled input.",
    )

    user: Mapped[User] = relationship(lazy="joined")
    """The owning user.

    Eagerly joined: resolving a session always needs the user, so a lazy load
    would mean a second query on every authenticated request — and, under
    async SQLAlchemy, a lazy load outside an await boundary raises.
    """

    def __repr__(self) -> str:
        """Debug representation with no token material.

        Neither the token hash nor the CSRF token appears. The hash is not
        directly replayable, but it is the lookup key: printing it into a log
        would let anyone reading that log confirm whether a captured token is
        live.
        """
        return (
            f"Session(id={self.id!r}, user_id={self.user_id!r}, "
            f"expires_at={self.expires_at!r}, revoked={self.revoked_at is not None})"
        )
