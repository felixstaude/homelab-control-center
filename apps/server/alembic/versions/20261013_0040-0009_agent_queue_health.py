"""add the agent telemetry queue counters to the host row

Phase 3. Two columns on ``hosts``: ``reported_buffered_items`` and
``reported_dropped_items``.

What §26 asks for, and the smallest thing that answers it. An operator needs
to see a backlog building **before** it starts dropping, and to see that it
dropped — a silent queue hides both, and "the chart has a gap" is then the
only symptom of a queue that overflowed during an outage.

Both are nullable with no default, and that is the point: ``NULL`` means the
agent has not said — one too old to report it, or one that has never
connected — and rendering that as zero would claim an empty queue the
controller has no evidence for. Same rule as every other agent-reported
column on this table.

Stored on the host row rather than as history, for the reason there is no row
per heartbeat (``hcc/models/host.py``): the only consumer is "what is this
agent's queue doing now", and a row per report would be millions a day to
answer it.

``reported_dropped_items`` is ``bigint`` rather than ``integer``. A
four-byte counter would wrap after two billion drops, which a long-lived
agent on a host with a flaky uplink could reach — and a counter that wrapped
would read as a *decrease*, which is the signal this schema uses to mean "the
agent restarted".

Produced entirely by ``alembic revision --autogenerate``; nothing was added by
hand. Reversible with no loss beyond the two columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_agent_queue_health"
down_revision: str | None = "0008_metric_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("reported_buffered_items", sa.Integer(), nullable=True))
    op.add_column("hosts", sa.Column("reported_dropped_items", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "reported_dropped_items")
    op.drop_column("hosts", "reported_buffered_items")
