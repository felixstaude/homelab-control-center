"""Widen the module allow-lists, and the unit column, for the new modules.

Phases D, E and F of Operation Horizon. No new tables and no new columns: three
CHECK constraints are replaced, each of which hard-codes the set of module
identifiers the registry knew when it was written, and one column is widened.

## The column, and how it was found

``metric_series.unit`` was ``varchar(16)``, which fits every unit that
existed when it was written and does not fit ``operations_per_second`` (21
characters). Nothing caught it: the unit tests do not touch a database, and
the integration suite passed because no test had yet stored a series in the
new unit.

It surfaced the first time a real agent reported real disk throughput, as
``StringDataRightTruncationError`` raised inside the WebSocket handler —
which the agent saw only as "the controller closed the connection:
protocol_error", and retried for ever. Widened to 32 here, with
``test_every_registry_unit_fits_its_column`` as the guard that makes the next
one a failing test instead.

## Why the identifiers are hard-coded in the first place

``hcc/monitoring/modules.py`` is the registry and the constraint is a copy of
it, rendered into SQL at model-definition time. That duplication is
deliberate — a module identifier that is not in the registry must not reach
the database, and the database is the only place that can promise it under
concurrent writers and a future bug — and the cost is exactly this: adding a
module is a migration.

The alternative, an enum type, is worse for the same operation: ``ALTER TYPE
… ADD VALUE`` could not run inside a transaction until PostgreSQL 12 and
still cannot be reverted, so a downgrade would be impossible rather than
merely awkward.

## Why this is safe on a running installation

A CHECK constraint that is *widened* cannot reject a row that is already
there, so ``ALTER TABLE … ADD CONSTRAINT`` validates the existing rows and
passes. The tables are small — one row per enabled module per host — so the
`ACCESS EXCLUSIVE` lock each statement takes is held for microseconds.

``metric_series`` is the exception worth naming: it is large and it is
partitioned's *parent*… except that it is not partitioned at all. The
partitioned tables are ``metric_samples`` and the rollups, which carry no
module column — the module lives on the series row, which a sample
references by integer. So the widened constraint here is validated against a
table with one row per distinct series, not one per sample: thousands, not
hundreds of millions.

## The downgrade narrows, and will refuse if it would lose data

Dropping back to the five-module set is only valid if nothing is using the
four new ones. The downgrade therefore deletes nothing: it re-adds the
narrow constraint, and PostgreSQL refuses the statement if a row violates it.
A migration that silently deleted an operator's monitoring configuration in
order to go backwards would be a worse outcome than a failed downgrade with a
clear error.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_storage_modules"
down_revision: str | None = "0010_update_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The set before this migration, and the set after. Written out rather than
#: imported from the registry: a migration describes the database at one
#: moment in history, and one that read today's registry would change meaning
#: every time a module is added — which is exactly how a migration history
#: stops being a record of anything.
_BEFORE = ("cpu", "memory", "swap", "load", "uptime")
_AFTER = (*_BEFORE, "filesystem", "diskio", "network", "sensors")

#: (table, column, constraint name). The two monitoring-configuration tables
#: name the column `module_id` and the series table names it `module`; that
#: inconsistency predates this migration and renaming a column to tidy it
#: would be a far larger change than the one being made here.
_CONSTRAINTS = (
    ("global_module_defaults", "module_id", "ck_global_module_defaults_module_id_is_known"),
    ("host_module_overrides", "module_id", "ck_host_module_overrides_module_id_is_known"),
    ("metric_series", "module", "ck_metric_series_module_is_known"),
)


def _values(identifiers: tuple[str, ...]) -> str:
    return ", ".join(f"'{identifier}'" for identifier in identifiers)


def _replace(identifiers: tuple[str, ...]) -> None:
    for table, column, name in _CONSTRAINTS:
        # Dropped and re-added rather than altered: PostgreSQL has no
        # `ALTER CONSTRAINT` for a CHECK's expression. One statement per
        # execute, because asyncpg refuses a multi-statement prepared string.
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"CHECK ({column} IN ({_values(identifiers)}))"
        )


#: Wide enough for every unit in the registry with room for one more idea.
#: Checked against the registry by tests/test_metric_models.py.
_UNIT_LENGTH_AFTER = 32
_UNIT_LENGTH_BEFORE = 16


def upgrade() -> None:
    _replace(_AFTER)
    # Widening a varchar does not rewrite the table and does not take a long
    # lock: PostgreSQL treats it as a catalogue change since 9.2.
    op.execute(f"ALTER TABLE metric_series ALTER COLUMN unit TYPE varchar({_UNIT_LENGTH_AFTER})")


def downgrade() -> None:
    _replace(_BEFORE)
    # Narrowing *does* rewrite, and will fail if any stored unit is longer —
    # which is correct. Truncating an operator's recorded units to go
    # backwards would silently change what historical series mean.
    op.execute(f"ALTER TABLE metric_series ALTER COLUMN unit TYPE varchar({_UNIT_LENGTH_BEFORE})")
