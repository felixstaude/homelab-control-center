"""add monitoring configuration: global defaults, host overrides, revisions

Phase 2. Three tables, described in ``hcc/models/monitoring.py``:

* ``global_module_defaults`` — one row per module, at most. What every host
  does unless told otherwise.
* ``host_module_overrides`` — one row per host and module, at most. ``NULL``
  in a value column means **inherit**, which is the whole point of the table
  being shaped this way rather than storing a merged document per host.
* ``host_config_revisions`` — one immutable, numbered snapshot per version of
  one host's effective configuration.

Plus three columns on ``hosts``: ``reported_config_version``,
``reported_config_checksum`` and ``reported_config_at`` — the agent's live
claim about what it is running, which is not the same fact as the
acknowledgement stored on a revision row. The difference between the two is
the whole of drift detection (``AGENT_ARCHITECTURE.md`` §6.4).

**No module is enabled by this migration, and none can be.** Every ``enabled``
column is created nullable with no server default, so the absence of a row and
the absence of a value both resolve to off in code. That is the product's
central promise (``MONITORING_MODULES.md`` §2) and the reason there is no
``server_default=sa.text('false')`` here to be changed to ``true`` by a later
one-line edit — there is nothing to flip.

Started from ``alembic revision --autogenerate``, which produced all three
tables, every CHECK, both foreign keys and all three indexes correctly.

**Two things were added by hand**, both of them things autogenerate
structurally cannot see:

1. The ``updated_at`` triggers on ``global_module_defaults`` and
   ``host_module_overrides``. Triggers are invisible to autogenerate
   (ADR-015), so without these the columns would be set on INSERT by their
   server defaults and then never change — and ``updated_at`` would silently
   mean "created_at". For the override table that is not cosmetic: a row with
   every value column null is what a *reset* leaves behind, and
   ``updated_at`` is the only record of when the reset happened.
   ``host_config_revisions`` deliberately gets no trigger and has no
   ``updated_at``: a revision's document is immutable.
2. ``if_exists`` on the index drops, matching ``0003_sessions`` and
   ``0006_host_registry``, so a downgrade is idempotent against a
   half-applied upgrade.

Three details that look like boilerplate and are not:

* ``host_config_revisions.document`` is ``text``, not ``jsonb``. The column
  holds the **canonical bytes** the checksum covers and the agent received;
  ``jsonb`` reorders keys and normalises numbers, so a round trip through it
  would not reproduce the digest, and "is this agent running what we think?"
  would become unanswerable (ADR-050). The two ``schedule`` columns *are*
  ``jsonb``, because nothing checksums them.
* ``checksum`` is ``char_length = 71``: ``sha256:`` plus 64 hex characters. A
  bare 64-character digest is refused, so a future algorithm change is visible
  in stored data rather than implied.
* The ``not_both_acknowledged_and_rejected`` CHECK. They are the two outcomes
  of one decision, and a row with both would make "what is this agent
  running?" ambiguous in the one place the answer has to be definite.

The ``module_id`` CHECK constraints enumerate the registry
(``hcc/monitoring/modules.py``) rather than referencing a modules table. The
registry is code: it ships with the controller and is versioned with it, and a
table would have to be kept in step by a migration that could disagree with
the code it mirrors. The cost is that adding a module needs a migration to
widen these constraints — the right amount of friction for a change that also
needs an agent release.

Reversible. The downgrade drops the three tables, the three ``hosts`` columns
and the two triggers, but **not** the shared ``set_updated_at()`` function,
which ``users``, ``hosts`` and ``sessions`` still need. What is lost is what
removing the feature inherently costs: configured defaults, overrides and the
revision history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_monitoring_config"
down_revision: str | None = "0006_host_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `WHEN (OLD.* IS DISTINCT FROM NEW.*)` so that a no-op UPDATE does not move
# `updated_at`, which keeps the column meaning "when this row's data last
# actually changed". Assigning unconditionally also means a client cannot
# backdate a row: any value an UPDATE supplies is overwritten.
CREATE_GLOBAL_DEFAULTS_TRIGGER = """
CREATE TRIGGER trg_global_module_defaults_set_updated_at
BEFORE UPDATE ON global_module_defaults
FOR EACH ROW
WHEN (OLD.* IS DISTINCT FROM NEW.*)
EXECUTE FUNCTION set_updated_at();
"""

CREATE_HOST_OVERRIDES_TRIGGER = """
CREATE TRIGGER trg_host_module_overrides_set_updated_at
BEFORE UPDATE ON host_module_overrides
FOR EACH ROW
WHEN (OLD.* IS DISTINCT FROM NEW.*)
EXECUTE FUNCTION set_updated_at();
"""

DROP_GLOBAL_DEFAULTS_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_global_module_defaults_set_updated_at ON global_module_defaults;"
)

DROP_HOST_OVERRIDES_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_host_module_overrides_set_updated_at ON host_module_overrides;"
)


def upgrade() -> None:
    op.create_table(
        "global_module_defaults",
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "module_id IN ('cpu', 'memory', 'swap', 'load', 'uptime')",
            name=op.f("ck_global_module_defaults_module_id_is_known"),
        ),
        sa.PrimaryKeyConstraint("module_id", name=op.f("pk_global_module_defaults")),
    )

    op.create_table(
        "host_config_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("content_checksum", sa.String(length=71), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_problems", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT (acknowledged_at IS NOT NULL AND rejected_at IS NOT NULL)",
            name=op.f("ck_host_config_revisions_not_both_acknowledged_and_rejected"),
        ),
        sa.CheckConstraint(
            "char_length(checksum) = 71",
            name=op.f("ck_host_config_revisions_checksum_is_prefixed_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(content_checksum) = 71",
            name=op.f("ck_host_config_revisions_content_checksum_is_prefixed_sha256"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_host_config_revisions_version_is_positive")
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_config_revisions_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_config_revisions")),
        sa.UniqueConstraint("host_id", "version", name="uq_host_config_revisions_host_version"),
    )
    op.create_index(
        "ix_host_config_revisions_host_id_version",
        "host_config_revisions",
        ["host_id", "version"],
        unique=False,
    )

    op.create_table(
        "host_module_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "module_id IN ('cpu', 'memory', 'swap', 'load', 'uptime')",
            name=op.f("ck_host_module_overrides_module_id_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_module_overrides_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_module_overrides")),
        sa.UniqueConstraint("host_id", "module_id", name="uq_host_module_overrides_host_module"),
    )
    op.create_index(
        "ix_host_module_overrides_host_id", "host_module_overrides", ["host_id"], unique=False
    )

    # Added by hand: invisible to autogenerate (ADR-015). The
    # `set_updated_at()` function is created by 0002_users.
    op.execute(CREATE_GLOBAL_DEFAULTS_TRIGGER)
    op.execute(CREATE_HOST_OVERRIDES_TRIGGER)

    op.add_column("hosts", sa.Column("reported_config_version", sa.Integer(), nullable=True))
    op.add_column(
        "hosts", sa.Column("reported_config_checksum", sa.String(length=71), nullable=True)
    )
    op.add_column(
        "hosts", sa.Column("reported_config_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("hosts", "reported_config_at")
    op.drop_column("hosts", "reported_config_checksum")
    op.drop_column("hosts", "reported_config_version")

    # The triggers, but not the shared `set_updated_at()` function — `users`,
    # `hosts` and `sessions` still use it.
    op.execute(DROP_HOST_OVERRIDES_TRIGGER)
    op.execute(DROP_GLOBAL_DEFAULTS_TRIGGER)

    op.drop_index(
        "ix_host_module_overrides_host_id", table_name="host_module_overrides", if_exists=True
    )
    op.drop_table("host_module_overrides")
    op.drop_index(
        "ix_host_config_revisions_host_id_version",
        table_name="host_config_revisions",
        if_exists=True,
    )
    op.drop_table("host_config_revisions")
    op.drop_table("global_module_defaults")
