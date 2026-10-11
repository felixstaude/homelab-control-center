"""Database engine, session factory and declarative base.

The control plane is the only writer to the database (ADR-003). Sessions are
created per request and always closed, including on error.

Async SQLAlchemy is used throughout — see ADR-004. The engine is created lazily
at application startup rather than at import time, so that importing a module
never opens a socket and tests can run without a database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from hcc.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hcc.config import Settings

logger = get_logger(__name__)

# Explicit naming convention so Alembic autogenerate produces stable,
# predictable constraint names instead of database-assigned ones. Without this,
# migrations that drop a constraint cannot reliably name it.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    Phase 0 defines no models deliberately — the schema arrives with the
    features that need it (Phase 1 onwards). The base and its metadata exist so
    that Alembic has a target to compare against.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Database:
    """Owns the engine and session factory for the process lifetime.

    Held on ``app.state`` rather than in a module global so that tests can
    construct an isolated instance, and so the shutdown path has an explicit
    object to dispose.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database.connect() must be called before use")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database.connect() must be called before use")
        return self._session_factory

    def connect(self) -> None:
        """Create the engine and session factory.

        Does not open a connection — asyncpg connects lazily on first use. The
        readiness probe is what actually proves the database is reachable.
        """
        if self._engine is not None:
            return

        engine_kwargs: dict[str, Any] = {
            "echo": self._settings.database_echo,
            "pool_pre_ping": True,  # recycle connections a restarted DB dropped
            "pool_size": self._settings.database_pool_size,
            "max_overflow": self._settings.database_max_overflow,
        }

        self._engine = create_async_engine(self._settings.sqlalchemy_url(), **engine_kwargs)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,  # keeps objects usable after commit
            autoflush=False,
        )
        logger.info(
            "database_engine_created",
            pool_size=self._settings.database_pool_size,
            max_overflow=self._settings.database_max_overflow,
        )

    async def disconnect(self) -> None:
        """Dispose the engine and close pooled connections."""
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._session_factory = None
        logger.info("database_engine_disposed")

    async def check_connection(self) -> None:
        """Verify the database answers a trivial query.

        Raises the underlying driver exception on failure — the caller decides
        how to report it. Used by the readiness probe.
        """
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, rolling back on error and always closing.

        Commits are the caller's responsibility: a request handler knows
        whether its unit of work succeeded, this wrapper does not.
        """
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                # Re-raised after rollback; the exception handlers log it.
                await session.rollback()
                raise
