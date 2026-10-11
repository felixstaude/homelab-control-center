"""Database wiring tests that need no live server.

Live-database behaviour is covered in ``test_db_integration.py``.
"""

from __future__ import annotations

import pytest

from hcc.config import Settings
from hcc.db import NAMING_CONVENTION, Base, Database


def _settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql://u:p@localhost:5432/hcc",
    )


def test_engine_access_before_connect_raises_explicitly() -> None:
    # Fails loudly rather than returning None and producing an
    # AttributeError somewhere far from the cause.
    database = Database(_settings())
    with pytest.raises(RuntimeError, match="connect"):
        _ = database.engine


def test_session_factory_access_before_connect_raises_explicitly() -> None:
    database = Database(_settings())
    with pytest.raises(RuntimeError, match="connect"):
        _ = database.session_factory


def test_connect_is_idempotent() -> None:
    database = Database(_settings())
    database.connect()
    first = database.engine
    database.connect()
    assert database.engine is first


def test_connect_uses_the_asyncpg_dialect() -> None:
    database = Database(_settings())
    database.connect()
    assert database.engine.dialect.driver == "asyncpg"


def test_metadata_uses_an_explicit_naming_convention() -> None:
    # Required for Alembic to emit stable constraint names in migrations.
    assert Base.metadata.naming_convention == NAMING_CONVENTION


def test_models_are_registered_on_the_metadata() -> None:
    # Replaces Phase 0's `test_phase_0_defines_no_tables`, which asserted an
    # empty schema. Its real purpose was to catch a model module that
    # hcc/models/__init__.py forgets to import — autogenerate would then emit
    # a drop for the missing table. This keeps that guarantee as tables are
    # added: importing hcc.models must register every mapped table.
    # Imported inside the test, not at module scope, because the assertion is
    # about the import's side effect on Base.metadata.
    import hcc.models

    assert hcc.models.Base is Base
    assert "users" in Base.metadata.tables
