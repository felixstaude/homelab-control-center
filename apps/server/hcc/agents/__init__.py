"""The agent-facing half of the control plane.

Phase 1, milestone 1.8. Everything in this package serves a caller that is
**not a browser and not a user**: a Go agent on a monitored host, presenting a
per-host credential over a connection it opened itself.

Kept separate from :mod:`hcc.api.v1` and :mod:`hcc.services` because the trust
model is different in kind, not in degree. A browser request carries a session
belonging to a person with a role; an agent connection carries a secret that
authorizes exactly one thing — reporting as one host. Neither kind of
credential is ever accepted in the other's place, and a reader of either
package should never have to work out which they are looking at.

* :mod:`hcc.agents.protocol` — the wire format, versioned, with every bound
  that keeps a message from being a resource exhaustion primitive.
* :mod:`hcc.agents.registry` — which connections this process currently holds,
  and the honest statement of why that is per-process.
"""

from __future__ import annotations
