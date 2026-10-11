"""Structured logging setup.

One configuration call at startup wires both ``structlog`` and the stdlib
``logging`` module through a single renderer, so that log lines emitted by
uvicorn, SQLAlchemy and application code all come out in the same shape.

Development renders human-readable coloured output; production renders JSON for
ingestion by whatever the operator runs. The event dictionary is identical in
both cases — only the final renderer differs.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from hcc.config import LogFormat, LogLevel

# Loggers that are noisy at INFO and whose useful content we already log
# ourselves via middleware.
_NOISY_LOGGERS = {
    "uvicorn.access": logging.WARNING,
    "uvicorn.error": logging.INFO,
}


def configure_logging(*, level: LogLevel, log_format: LogFormat) -> None:
    """Configure structlog and the stdlib logging bridge.

    Idempotent: calling it twice (for example in a test suite) reconfigures
    rather than stacking handlers.
    """
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.typing.Processor
    if log_format == "json":
        # Render exceptions as a single string field so the JSON stays one
        # line per event.
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, SQLAlchemy, alembic) through the same
    # processor chain so third-party logs match ours.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(noisy_level)


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally pre-bound with context."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_values:
        return logger.bind(**initial_values)
    return logger
