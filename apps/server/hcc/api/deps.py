"""Shared FastAPI dependencies.

Dependencies resolve collaborators from ``app.state`` rather than from module
globals or a cached singleton. That matters for correctness, not just style:
``create_app(settings)`` accepts injected settings, so a dependency that read
the environment-backed ``get_settings()`` singleton instead would hand handlers
*different* configuration than the one the app was built with. Reading
``app.state`` makes the app the single source of truth and keeps handlers
testable without monkeypatching imports.

Note on imports: the types used inside ``Annotated`` aliases below are imported
at runtime, not under ``TYPE_CHECKING``. FastAPI resolves endpoint annotations
with ``get_type_hints`` against the *endpoint's* module namespace, so a string
forward reference here would fail to resolve in whichever router module uses
the alias.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hcc.config import Settings
from hcc.db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def get_app_settings(request: Request) -> Settings:
    """Return the settings the application was built with."""
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    """Return the process-wide :class:`~hcc.db.Database`."""
    database: Database = request.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped database session."""
    async for session in database.session():
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
