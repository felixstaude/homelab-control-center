"""Update management: release discovery, compatibility, policy and rollout.

The package boundary is deliberate. Everything here answers questions about
*versions and whether to move between them*; nothing here talks to an agent or
owns an HTTP route. That keeps the part with the interesting logic — version
ordering, compatibility conjunctions, policy decisions — testable without a
database, a socket or a clock.

See ``docs/plans/evergreen-update-architecture.md`` for the design and the
threat model it was written against.
"""

from __future__ import annotations
