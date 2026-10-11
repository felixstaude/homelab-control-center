"""ORM model registry.

This package is the single import point for Alembic autogenerate:
``alembic/env.py`` imports it, so every model module re-exported here is
registered on ``Base.metadata``. **New model modules must be imported below**,
or autogenerate will not see them — and worse, will emit drop statements for
tables it believes have been removed.
"""

from hcc.db import Base
from hcc.models.installation import Installation
from hcc.models.session import Session
from hcc.models.user import (
    PASSWORD_HASH_MAX_LENGTH,
    USER_ROLE_ENUM_NAME,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    User,
    UserRole,
    normalize_username,
)

__all__ = [
    "PASSWORD_HASH_MAX_LENGTH",
    "USERNAME_MAX_LENGTH",
    "USERNAME_MIN_LENGTH",
    "USER_ROLE_ENUM_NAME",
    "Base",
    "Installation",
    "Session",
    "User",
    "UserRole",
    "normalize_username",
]
