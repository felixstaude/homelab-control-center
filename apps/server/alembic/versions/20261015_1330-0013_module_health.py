"""Why a chart is empty when collection is on and the agent is connected.

Phase M of Operation Horizon, and a defect it found rather than a feature it
planned.

``hcc.agents.protocol.TelemetryEntry`` has always carried an ``error`` field.
Its own description reads: "Why this collection produced nothing. Present
*with* an empty sample list: a module that has been failing for an hour is
information an operator needs." The controller parsed that field, validated
it, bounded it — and then never read it. ``MetricsService.ingest`` looked at
``entry.samples`` and nothing else.

So a removed disk, a stopped Docker daemon, or a ``systemctl`` that started
refusing produced an enabled module with no data, no explanation and no end,
and the only evidence was a warning line in the agent's log on the host
nobody was looking at.

This table is where that field lands.

## One row per host per module

Upserted on ``(host_id, module_id)`` by the ingest path, which is the hot
path — so the write is one statement per batch, folded into the same
transaction as the samples. A batch that is rolled back leaves no health
row claiming a collection that was never stored.

## Not a column on ``host_module_overrides``

That table is what the operator *wants*; this one is what is *happening*. A
module inheriting the global default has no override row at all and still has
health, and resetting an override deletes a row that must not take the
failure history with it.

## Every timestamp is the controller's clock

Unlike ``metric_series.last_sample_at``, which is the agent's. A host whose
clock is wrong is one of the conditions this table reports, and a diagnostic
timestamped by the thing being diagnosed is not a diagnostic.

## Nullable timestamps, and a CHECK that keeps a pair together

``last_error`` and ``last_error_at`` are either both set or both null. A row
with one of them is a row the interface cannot render honestly — "failing, at
an unknown moment" and "failed at 14:03 for no stated reason" are both worse
than the absence the constraint forbids them from becoming.

## Why this is safe on a running installation

A new table. Nothing reads it until the code that writes it is deployed, and
an installation that has this migration and an older image simply has an
empty table.

## The downgrade drops it

The data is observational and regenerates within one collection interval of
the next agent heartbeat, so dropping it loses an hour of failure history at
worst. Stated rather than silently done.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_module_health"
down_revision: str | None = "0012_rollout_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The module set at the moment this migration was written. Spelled out
#: rather than imported from the registry, for the reason 0011 gives: a
#: migration describes the database at one point in history, and one that
#: read today's registry would change meaning every time a module is added.
_MODULES = (
    "cpu",
    "memory",
    "swap",
    "load",
    "uptime",
    "filesystem",
    "diskio",
    "network",
    "sensors",
    "docker",
    "systemd",
)

#: Matches ``hcc.agents.protocol.MAX_COLLECTOR_ERROR_LENGTH``. Two bounds for
#: one value: the protocol refuses a longer frame, and this is what the
#: column can physically hold if the protocol one is ever relaxed.
_MAX_ERROR_LENGTH = 256


def _values(identifiers: tuple[str, ...]) -> str:
    return ", ".join(f"'{identifier}'" for identifier in identifiers)


def upgrade() -> None:
    op.create_table(
        "host_module_health",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("host_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=_MAX_ERROR_LENGTH), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_host_module_health"),
        # Deleting a host removes its health: a collector's state on a host
        # that no longer exists is not a fact about anything.
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name="fk_host_module_health_host_id_hosts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("host_id", "module_id", name="uq_host_module_health_host_module"),
        sa.CheckConstraint(
            f"module_id IN ({_values(_MODULES)})",
            name="ck_host_module_health_module_id_is_known",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_host_module_health_failures_are_not_negative",
        ),
        sa.CheckConstraint(
            "(last_error IS NULL) = (last_error_at IS NULL)",
            name="ck_host_module_health_an_error_has_a_time_and_a_time_has_an_error",
        ),
    )
    op.create_index("ix_host_module_health_host_id", "host_module_health", ["host_id"])

    # The same trigger every other table with an `updated_at` uses, so a row
    # updated by a raw UPSERT that does not name the column still gets a new
    # timestamp. Without it, `updated_at` would record the row's creation and
    # the interface would show a module that last failed ten minutes ago as
    # last heard from three weeks ago.
    op.execute(
        "CREATE TRIGGER trg_host_module_health_set_updated_at "
        "BEFORE UPDATE ON host_module_health "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_host_module_health_set_updated_at ON host_module_health")
    op.drop_index("ix_host_module_health_host_id", table_name="host_module_health")
    op.drop_table("host_module_health")
