"""Authorization: who may do what, and where.

Phase 1, milestone 1.4. Framework-free, like the rest of
:mod:`hcc.security` — it imports no FastAPI. The request-facing dependencies
that wrap it live in :mod:`hcc.api.deps`.

## Authentication and authorization are separate

:mod:`hcc.security.sessions` answers *who is calling*. This module answers
*whether they may*. They are deliberately distinct checks with distinct
failures: a missing session is ``401 not_authenticated``, an insufficient role
is ``403 insufficient_permissions``. Collapsing them would make "log in again"
and "you are not allowed" indistinguishable to a client, and would hide the
difference in the logs too.

## Two axes (ADR-023)

```
   WHO: the user's role                AND    WHERE: the target host's
   (Viewer / Operator / Administrator)        enabled capabilities
```

Both must permit an action. The role axis is implemented here in full. The
capability axis has its interface defined and **deliberately no
implementation**, because hosts and their capability configuration do not
exist yet (milestone 1.8 onwards). Asking for a capability check today
**denies**, loudly — see :class:`Authorizer`.

## Roles are hierarchical; capabilities are not

The role set is the closed three from ADR-013 and is strictly ordered:
``viewer < operator < administrator``. Every permission in
``docs/ARCHITECTURE.md`` §4.3 is monotonic in that order, so a minimum-role
test is sufficient and there is no need for per-role sets.

That is not an accident of the current matrix — it is the design. Granularity
is meant to come from the *capability* axis, not from inventing roles or
non-hierarchical role sets (ADR-023). If a future permission is not monotonic
in the role order, it belongs on the capability axis.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from hcc.logging import get_logger
from hcc.models.user import UserRole

if TYPE_CHECKING:
    import uuid

    from hcc.security.sessions import AuthenticatedSession

logger = get_logger(__name__)


class Capability(enum.StrEnum):
    """Per-host management capabilities.

    Mirrors ``docs/REMOTE_MANAGEMENT.md`` §1. Defined now so the authorization
    interface is complete and typed; none of them is enforceable yet, because
    nothing stores per-host capability state.

    Read and control are separate members on purpose: enabling one must never
    imply the other.
    """

    DOCKER_READ = "docker.read"
    DOCKER_CONTROL = "docker.control"
    SYSTEMD_READ = "systemd.read"
    SYSTEMD_CONTROL = "systemd.control"
    SESSIONS_READ = "sessions.read"
    SESSIONS_CONTROL = "sessions.control"
    TERMINAL = "terminal"


#: Role ordering. Higher admits everything a lower role admits.
_ROLE_RANK: Final[dict[UserRole, int]] = {
    UserRole.VIEWER: 0,
    UserRole.OPERATOR: 1,
    UserRole.ADMIN: 2,
}

#: The lowest role that may *ever* exercise each capability, from
#: ``docs/REMOTE_MANAGEMENT.md`` §4. This is only the role half of the
#: decision: the capability must also be enabled on the target host, which is
#: the half that does not exist yet.
#:
#: ``TERMINAL`` is Administrator-only and must stay that way — it is the most
#: dangerous capability in the product (ADR-022).
CAPABILITY_MINIMUM_ROLE: Final[dict[Capability, UserRole]] = {
    Capability.DOCKER_READ: UserRole.VIEWER,
    Capability.DOCKER_CONTROL: UserRole.OPERATOR,
    Capability.SYSTEMD_READ: UserRole.VIEWER,
    Capability.SYSTEMD_CONTROL: UserRole.OPERATOR,
    Capability.SESSIONS_READ: UserRole.VIEWER,
    Capability.SESSIONS_CONTROL: UserRole.OPERATOR,
    Capability.TERMINAL: UserRole.ADMIN,
}


def role_rank(role: UserRole) -> int:
    """Return a role's position in the ordering.

    Raises:
        KeyError: for a role missing from the ordering. Deliberately not a
            default of 0 — an unranked role silently treated as Viewer would
            be a security bug, and adding a role must be a deliberate change
            here too.
    """
    return _ROLE_RANK[role]


def role_satisfies(actual: UserRole, minimum: UserRole) -> bool:
    """Whether ``actual`` is at least ``minimum`` in the role ordering."""
    return role_rank(actual) >= role_rank(minimum)


class InsufficientRoleError(Exception):
    """The caller's role is below what the action requires.

    Translated to ``403 insufficient_permissions`` by the API layer. Carries
    the required and actual roles for the server-side log; the client is told
    only that it is not permitted, because enumerating what a higher role
    could do is information an attacker can use.
    """

    def __init__(self, required: UserRole, actual: UserRole) -> None:
        self.required = required
        self.actual = actual
        super().__init__(f"requires role {required.value}, caller has {actual.value}")


class CapabilityNotEnabledError(Exception):
    """The capability is not enabled on the target host.

    Distinct from :class:`InsufficientRoleError`: the caller's role was
    sufficient, the host simply does not permit this class of action. Both
    become ``403 insufficient_permissions`` for the client — it is equally
    "not permitted" either way — but the server log distinguishes them,
    because "turn this on for that host" and "give this user a higher role"
    are different fixes.
    """

    def __init__(self, capability: Capability, host_id: uuid.UUID) -> None:
        self.capability = capability
        self.host_id = host_id
        super().__init__(f"capability {capability.value} is not enabled on host {host_id}")


class CapabilityNotAvailableError(Exception):
    """A capability check was requested but cannot be evaluated.

    Raised when no :class:`CapabilityResolver` is configured, which is the
    state until per-host capability configuration exists. The API layer
    translates it to a denial, **never** to a success: an authorization
    question that cannot be answered must be answered "no".
    """

    def __init__(self, capability: Capability) -> None:
        self.capability = capability
        super().__init__(
            f"capability {capability.value} cannot be evaluated: per-host "
            "capability configuration is not implemented yet"
        )


@runtime_checkable
class CapabilityResolver(Protocol):
    """Reports whether a capability is enabled on a host.

    The seam for the second authorization axis. An implementation arrives with
    per-host configuration; until then nothing satisfies this protocol and
    capability checks deny.

    An implementation must be **default-deny**: an unknown host, an
    unconfigured capability or an unreachable store all mean "not enabled".
    """

    async def is_enabled(self, host_id: uuid.UUID, capability: Capability) -> bool:
        """Whether ``capability`` is enabled on ``host_id``."""
        ...


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """One authorization question.

    Carries both axes so that a call site states its full requirement in one
    place rather than performing a role check here and a capability check
    somewhere else, where one could be forgotten.
    """

    session: AuthenticatedSession
    minimum_role: UserRole
    #: The capability this action needs, if any. Requires ``host_id``.
    capability: Capability | None = None
    #: The host the action targets. Required when ``capability`` is set.
    host_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.capability is not None and self.host_id is None:
            raise ValueError(
                f"capability {self.capability.value} requires a host_id: a "
                "capability is a property of a specific host"
            )
        if self.host_id is not None and self.capability is None:
            raise ValueError("host_id given without a capability; one implies the other")


class Authorizer:
    """Evaluates authorization requests.

    Stateless apart from its optional resolver, so one instance is shared
    across requests.
    """

    __slots__ = ("_capability_resolver",)

    def __init__(self, capability_resolver: CapabilityResolver | None = None) -> None:
        self._capability_resolver = capability_resolver

    @property
    def supports_capabilities(self) -> bool:
        """Whether the capability axis can be evaluated at all."""
        return self._capability_resolver is not None

    async def authorize(self, request: AuthorizationRequest) -> None:
        """Permit the request, or raise.

        Checks the role first: it is free, and a caller whose role is too low
        must be refused regardless of host configuration. The capability check
        follows only for callers who passed.

        Raises:
            InsufficientRoleError: the caller's role is below the minimum, or
                below the floor the requested capability demands.
            CapabilityNotAvailableError: a capability was requested and no
                resolver is configured. **Denies** — see the class docstring.
        """
        role = request.session.user.role

        if not role_satisfies(role, request.minimum_role):
            raise InsufficientRoleError(request.minimum_role, role)

        if request.capability is None:
            return

        # A capability has its own role floor, independent of the endpoint's.
        # Both apply: an endpoint may demand more than the capability does.
        capability_floor = CAPABILITY_MINIMUM_ROLE[request.capability]
        if not role_satisfies(role, capability_floor):
            raise InsufficientRoleError(capability_floor, role)

        resolver = self._capability_resolver
        if resolver is None:
            # Fail closed. Reachable only if a call site requests a capability
            # before per-host configuration exists, which is a programming
            # error — so it is logged as one, and still denied.
            logger.error(
                "capability_check_unavailable",
                capability=request.capability.value,
                host_id=str(request.host_id),
                user_id=str(request.session.user.id),
            )
            raise CapabilityNotAvailableError(request.capability)

        host_id = request.host_id
        if host_id is None:  # pragma: no cover - __post_init__ guarantees it
            raise CapabilityNotAvailableError(request.capability)

        if not await resolver.is_enabled(host_id, request.capability):
            logger.info(
                "capability_not_enabled",
                capability=request.capability.value,
                host_id=str(host_id),
                user_id=str(request.session.user.id),
            )
            raise CapabilityNotEnabledError(request.capability, host_id)
