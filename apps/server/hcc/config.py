"""Application configuration.

All configuration is read from the environment (or a local ``.env`` file during
development) and validated once at startup. There are no hardcoded credentials
and no runtime mutation of settings: if the environment is invalid the process
fails immediately with a readable error rather than degrading later.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogFormat = Literal["console", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Validated application settings.

    Every field maps to an ``HCC_``-prefixed environment variable, e.g.
    ``HCC_DATABASE_URL``. See ``.env.example`` for the full documented set.
    """

    model_config = SettingsConfigDict(
        env_prefix="HCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    # --- Core -------------------------------------------------------------
    environment: Environment = "development"
    debug: bool = False

    # --- HTTP -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_prefix: str = "/api"

    # Browser origins permitted to call the API. Empty by default: the
    # production deployment serves the web client from the same origin and
    # therefore needs no CORS exception. Development adds the Vite dev server.
    cors_origins: tuple[str, ...] = ()

    # --- Database ---------------------------------------------------------
    # No default. A missing database URL is a configuration error, not
    # something to paper over with a localhost guess that fails confusingly
    # later.
    database_url: PostgresDsn
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_echo: bool = False

    # --- Logging ----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    @field_validator("api_prefix")
    @classmethod
    def _validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("api_prefix must start with '/'")
        return value.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def sqlalchemy_url(self) -> str:
        """Return the database URL with the asyncpg driver enforced.

        Operators write a plain ``postgresql://`` URL — the form every other
        Postgres tool accepts. SQLAlchemy needs the driver named explicitly to
        select its async dialect, so it is normalised here rather than pushed
        onto whoever writes the environment file.
        """
        url = str(self.database_url)
        _, separator, rest = url.partition("://")
        if not separator:  # pragma: no cover - PostgresDsn guarantees a scheme
            raise ValueError(f"malformed database URL: {url!r}")
        return f"postgresql+asyncpg://{rest}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that validation happens exactly once. Tests clear the cache via
    ``get_settings.cache_clear()`` to install their own environment.
    """
    return Settings()


def load_settings_or_exit() -> Settings:
    """Load settings, or print a readable diagnostic and exit non-zero.

    Used by entrypoints. A Pydantic traceback is the wrong thing to show an
    operator who simply forgot to set ``HCC_DATABASE_URL``.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        print("Invalid configuration — refusing to start:\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            variable = f"HCC_{location.upper()}"
            print(f"  {variable}: {error['msg']}", file=sys.stderr)
        print("\nSee apps/server/.env.example for the expected values.", file=sys.stderr)
        raise SystemExit(2) from exc
