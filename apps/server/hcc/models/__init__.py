"""ORM model registry.

This package is the single import point for Alembic autogenerate:
``alembic/env.py`` imports it, so every model module re-exported here is
registered on ``Base.metadata``. **New model modules must be imported below**,
or autogenerate will not see them — and worse, will emit drop statements for
tables it believes have been removed.
"""

from hcc.db import Base
from hcc.models.audit import (
    ACTION_MAX_LENGTH,
    OUTCOME_MAX_LENGTH,
    AuditAction,
    AuditEvent,
    AuditOutcome,
)
from hcc.models.host import (
    AGENT_VERSION_MAX_LENGTH,
    HEARTBEAT_GRACE_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HOST_DESCRIPTION_MAX_LENGTH,
    HOST_ENVIRONMENT_MAX_LENGTH,
    HOST_LIFECYCLE_TRANSITIONS,
    HOST_LIFECYCLE_VALUES,
    HOST_NAME_MAX_LENGTH,
    HOST_NAME_MIN_LENGTH,
    PLATFORM_MAX_LENGTH,
    AgentCredential,
    Host,
    HostConnectivity,
    HostEnrollment,
    HostLifecycle,
    HostLifecycleError,
    canonical_host_name,
    check_lifecycle_transition,
    derive_connectivity,
    normalize_host_name,
)
from hcc.models.installation import Installation
from hcc.models.session import Session
from hcc.models.user import (
    DISPLAY_NAME_MAX_LENGTH,
    PASSWORD_HASH_MAX_LENGTH,
    USER_ROLE_ENUM_NAME,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    User,
    UserRole,
    fold_username,
    normalize_display_name,
    normalize_username,
)

__all__ = [
    "ACTION_MAX_LENGTH",
    "AGENT_VERSION_MAX_LENGTH",
    "DISPLAY_NAME_MAX_LENGTH",
    "HEARTBEAT_GRACE_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HOST_DESCRIPTION_MAX_LENGTH",
    "HOST_ENVIRONMENT_MAX_LENGTH",
    "HOST_LIFECYCLE_TRANSITIONS",
    "HOST_LIFECYCLE_VALUES",
    "HOST_NAME_MAX_LENGTH",
    "HOST_NAME_MIN_LENGTH",
    "OUTCOME_MAX_LENGTH",
    "PASSWORD_HASH_MAX_LENGTH",
    "PLATFORM_MAX_LENGTH",
    "USERNAME_MAX_LENGTH",
    "USERNAME_MIN_LENGTH",
    "USER_ROLE_ENUM_NAME",
    "AgentCredential",
    "AuditAction",
    "AuditEvent",
    "AuditOutcome",
    "Base",
    "Host",
    "HostConnectivity",
    "HostEnrollment",
    "HostLifecycle",
    "HostLifecycleError",
    "Installation",
    "Session",
    "User",
    "UserRole",
    "canonical_host_name",
    "check_lifecycle_transition",
    "derive_connectivity",
    "fold_username",
    "normalize_display_name",
    "normalize_host_name",
    "normalize_username",
]
