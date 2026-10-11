"""Security primitives.

Framework-free building blocks for authentication and authorization. Nothing
in this package imports FastAPI or touches HTTP: these are callable from a
request handler, a CLI command, a migration or a test alike.

Phase 1, milestone 1.2 provides password hashing only. Sessions, tokens and
authorization arrive in later milestones.
"""

from hcc.security.password import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    Argon2Parameters,
    PasswordError,
    PasswordHasher,
    PasswordPolicyError,
    build_password_hasher,
    normalize_password,
    validate_password,
)

__all__ = [
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "Argon2Parameters",
    "PasswordError",
    "PasswordHasher",
    "PasswordPolicyError",
    "build_password_hasher",
    "normalize_password",
    "validate_password",
]
