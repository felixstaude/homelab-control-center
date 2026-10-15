"""The metric schema's column widths, checked against the registry.

No database. Every test here compares two things that are written down in
different files and must agree, and each one exists because the disagreement
is invisible until a real agent reports a real value.

## The failure these are shaped around

``metric_series.unit`` was ``varchar(16)``. Every unit that existed fitted.
Then ``operations_per_second`` — 21 characters — was added to the registry,
and:

* every unit test passed, because none of them touch a database;
* the integration suite passed, because no test had yet stored a series in
  the new unit;
* the first **real** agent reporting **real** disk throughput raised
  ``StringDataRightTruncationError`` inside the WebSocket handler, which the
  agent saw as "the controller closed the connection: protocol_error" and
  retried for ever.

A column width is a contract with the registry, and it is cheap to check.
"""

from __future__ import annotations

from sqlalchemy import String

from hcc.models.metrics import (
    MAX_LABELS_KEY_LENGTH,
    MAX_METRIC_ID_LENGTH,
    MAX_UNIT_LENGTH,
    MetricSeries,
)
from hcc.monitoring.modules import (
    MAX_MODULE_ID_LENGTH,
    MODULE_LIST,
    MetricKind,
    MetricUnit,
)


def _column_length(name: str) -> int:
    """The declared width of a String column, read off the table.

    Read from the table rather than from the module's constants, so a
    constant raised without the column following it is still a failure —
    which is exactly the shape the original defect had.
    """
    column_type = MetricSeries.__table__.columns[name].type
    assert isinstance(column_type, String), f"{name} is {column_type!r}, not a String"
    assert column_type.length is not None, f"{name} has no declared length"
    return int(column_type.length)


def test_every_registry_unit_fits_its_column() -> None:
    """The one that would have caught it.

    Against the *column* rather than against the constant, so a constant
    raised without the column following it is still a failure.
    """
    width = _column_length("unit")
    assert width == MAX_UNIT_LENGTH
    too_long = sorted(unit.value for unit in MetricUnit if len(unit.value) > width)
    assert not too_long, (
        f"{too_long} are longer than metric_series.unit ({width}). Storing a "
        f"series in one of them raises StringDataRightTruncationError inside "
        f"the agent WebSocket handler, which the agent sees only as a "
        f"protocol error. Widen the column in a migration."
    )


def test_every_registry_kind_fits_its_column() -> None:
    width = _column_length("kind")
    too_long = sorted(kind.value for kind in MetricKind if len(kind.value) > width)
    assert not too_long, f"{too_long} are longer than metric_series.kind ({width})"


def test_every_module_identifier_fits_its_column() -> None:
    width = _column_length("module")
    assert width == MAX_MODULE_ID_LENGTH
    too_long = sorted(module.id for module in MODULE_LIST if len(module.id) > width)
    assert not too_long, f"{too_long} are longer than metric_series.module ({width})"


def test_every_metric_identifier_fits_its_column() -> None:
    width = _column_length("metric")
    assert width == MAX_METRIC_ID_LENGTH
    too_long = sorted(
        f"{module.id}.{metric.id}"
        for module in MODULE_LIST
        for metric in module.metrics
        if len(metric.id) > width
    )
    assert not too_long, f"{too_long} are longer than metric_series.metric ({width})"


def test_a_plausible_label_set_fits_the_canonical_key() -> None:
    """``labels_key`` carries the unique constraint, so an overflow is a
    constraint violation rather than a truncated display value.

    Checked against the worst case the registry can actually produce: the
    longest label value any module declares a key for. A filesystem mount
    point is bounded at 64 characters by the agent
    (``collect.MaxMountPathChars``) precisely so that the rendered key stays
    inside this column.
    """
    longest_keys = max(
        (metric.label_keys for module in MODULE_LIST for metric in module.metrics),
        key=lambda keys: sum(len(key) for key in keys),
        default=(),
    )
    # 64 is hcc.agents.protocol.MAX_LABEL_VALUE_LENGTH, the bound the
    # controller already enforces on an incoming sample.
    worst_case = sum(len(key) + 1 + 64 + 1 for key in longest_keys)
    assert worst_case <= MAX_LABELS_KEY_LENGTH, (
        f"the widest label set the registry allows renders to {worst_case} "
        f"characters, over metric_series.labels_key ({MAX_LABELS_KEY_LENGTH})"
    )
