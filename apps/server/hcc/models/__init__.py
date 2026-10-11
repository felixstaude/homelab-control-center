"""ORM model registry.

Phase 0 defines no tables. The schema is introduced alongside the features
that need it, starting with hosts and agent registration in Phase 1 — adding
speculative tables now would violate engineering rule 3.

This package exists as the single import point for Alembic autogenerate:
``alembic/env.py`` imports it, so every model module re-exported here is
registered on ``Base.metadata``. New model modules must be imported below or
autogenerate will not see them.
"""

from hcc.db import Base

__all__ = ["Base"]
