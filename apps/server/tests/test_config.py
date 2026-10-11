"""Settings validation tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from hcc.config import Settings

VALID_DSN = "postgresql://user:pw@db.internal:5432/hcc"


def _settings(**overrides: Any) -> Settings:
    """Build Settings with a valid DSN, overriding individual fields."""
    base: dict[str, Any] = {"database_url": VALID_DSN}
    base.update(overrides)
    return Settings(**base)


def test_database_url_is_required() -> None:
    # A missing database URL must fail at startup, not at first query.
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert any(error["loc"] == ("database_url",) for error in exc_info.value.errors())


def test_sqlalchemy_url_injects_asyncpg_driver() -> None:
    # Operators write a plain postgresql:// URL; SQLAlchemy needs the driver.
    assert _settings().sqlalchemy_url() == f"postgresql+asyncpg://{VALID_DSN.split('://')[1]}"


def test_sqlalchemy_url_is_idempotent_for_driver_qualified_input() -> None:
    settings = _settings(database_url="postgresql+asyncpg://user:pw@db:5432/hcc")
    assert settings.sqlalchemy_url().count("asyncpg") == 1
    assert settings.sqlalchemy_url().startswith("postgresql+asyncpg://")


def test_unknown_environment_variable_is_rejected() -> None:
    # extra="forbid" turns a typo'd variable into a startup failure instead of
    # a setting that silently does nothing.
    with pytest.raises(ValidationError):
        _settings(not_a_real_setting="x")


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_port_must_be_in_range(port: int) -> None:
    with pytest.raises(ValidationError):
        _settings(port=port)


def test_api_prefix_must_be_absolute() -> None:
    with pytest.raises(ValidationError):
        _settings(api_prefix="api")


def test_api_prefix_trailing_slash_is_normalised() -> None:
    assert _settings(api_prefix="/api/").api_prefix == "/api"


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(log_level="TRACE")


def test_settings_are_immutable() -> None:
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.port = 9999  # type: ignore[misc]


def test_is_production_reflects_environment() -> None:
    assert _settings(environment="production").is_production is True
    assert _settings(environment="development").is_production is False
