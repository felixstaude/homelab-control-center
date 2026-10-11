"""Installation record — the durable first-run invariant.

Phase 1, milestone 1.5. Exactly one row ever exists in this table. Its
presence means "this installation has been initialized", and that fact is
what closes the bootstrap path.

## Why a table, and not ``SELECT count(*) FROM users``

Bootstrap must become unavailable after the first successful initialization
and **must not become available again** because the administrator was later
deactivated, renamed, or deleted. An empty ``users`` table is a statement
about the current user list, not about whether the installation was ever
claimed — so deriving the invariant from it would hand ownership of a
production deployment to whoever noticed that the last account had been
removed.

This row is therefore the invariant. It is written once, in the same
transaction as the first administrator, and nothing in the application ever
deletes or updates it.

## Why the primary key is a constant

``id`` is a boolean with a ``CHECK (id)`` constraint, so ``true`` is the only
value the column can hold and the primary key makes it unique. A second
``INSERT`` is therefore refused by the database rather than by application
logic:

    INSERT INTO installation (id, ...) VALUES (true, ...)
    -- second concurrent writer: duplicate key value violates unique
    --                           constraint "pk_installation"

That is the atomicity guarantee. Two concurrent bootstrap attempts cannot
both succeed, whatever the application does, and whatever order the
processes run in — the loser blocks on the index until the winner commits and
then fails. :mod:`hcc.security.bootstrap` additionally takes a PostgreSQL
advisory lock so that the loser gets a readable "already initialized" error
instead of an integrity error, but correctness does not depend on the lock
being taken.

## Why there is no foreign key to ``users``

``initialized_by_user_id`` records which account was created, for forensics.
It is deliberately **not** a foreign key: an ``ON DELETE CASCADE`` would
delete this row along with the administrator and silently reopen bootstrap,
which is the exact failure mode the table exists to prevent. An
``ON DELETE SET NULL`` would be safe but still implies the reference is
maintained, which it is not — this is an immutable historical record of what
happened at initialization, not a live pointer.
"""

from __future__ import annotations

import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. Same constraint as hcc/models/user.py.
from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from hcc.db import Base

#: How the installation was initialized. Recorded so an operator can tell a
#: CLI bootstrap from any future mechanism without reading logs.
BOOTSTRAP_METHOD_MAX_LENGTH: Final = 32

#: The only permitted value of the primary key.
SINGLETON_ID: Final = True


class Installation(Base):
    """The single row recording that this installation has been initialized.

    Created by :mod:`hcc.security.bootstrap` and never modified afterwards.
    """

    __tablename__ = "installation"

    __table_args__ = (
        # `id` can only ever be true, so the primary key permits exactly one
        # row. This is the enforcement, not a convention.
        CheckConstraint("id", name="singleton"),
        CheckConstraint(
            f"char_length(bootstrap_method) BETWEEN 1 AND {BOOTSTRAP_METHOD_MAX_LENGTH}",
            name="bootstrap_method_length",
        ),
    )

    id: Mapped[bool] = mapped_column(
        Boolean,
        primary_key=True,
        default=SINGLETON_ID,
        server_default=text("true"),
        doc="Always true. The CHECK constraint plus the primary key allow one row.",
    )

    initialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When the first administrator was created. Never updated.",
    )

    initialized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "The administrator created at bootstrap. Deliberately NOT a "
            "foreign key — see the module docstring. Nullable so a future "
            "mechanism that initializes without creating a user remains "
            "representable."
        ),
    )

    bootstrap_method: Mapped[str] = mapped_column(
        String(BOOTSTRAP_METHOD_MAX_LENGTH),
        nullable=False,
        doc="How initialization happened, e.g. 'cli'.",
    )

    def __repr__(self) -> str:
        return (
            f"Installation(initialized_at={self.initialized_at!r}, "
            f"bootstrap_method={self.bootstrap_method!r})"
        )
