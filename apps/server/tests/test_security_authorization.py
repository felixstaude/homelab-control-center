"""Authorization unit tests.

No database, no HTTP. Covers the role ordering, the two-axis request shape,
and that the unimplemented capability axis fails closed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hcc.models.user import User, UserRole
from hcc.security.authorization import (
    CAPABILITY_MINIMUM_ROLE,
    AuthorizationRequest,
    Authorizer,
    Capability,
    CapabilityNotAvailableError,
    CapabilityNotEnabledError,
    CapabilityResolver,
    InsufficientRoleError,
    role_rank,
    role_satisfies,
)

if TYPE_CHECKING:
    from hcc.security.sessions import AuthenticatedSession

FAKE_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHR2YWx1ZQ$" + "a" * 43


@dataclass
class _StubSession:
    """Stands in for AuthenticatedSession, which needs a database row.

    Only ``user`` is read by the authorizer, so a stub keeps these tests free
    of PostgreSQL. Endpoint-level behaviour with real sessions is covered in
    ``test_api_authorization.py``.
    """

    user: User


def session_for(role: UserRole, *, is_active: bool = True) -> AuthenticatedSession:
    user = User(username=f"user-{role.value}", password_hash=FAKE_HASH, role=role)
    user.id = uuid.uuid4()
    user.is_active = is_active
    return _StubSession(user=user)  # type: ignore[return-value]


class _AllowAll:
    """A resolver that enables everything. For testing the wiring only."""

    async def is_enabled(self, host_id: uuid.UUID, capability: Capability) -> bool:
        return True


class _DenyAll:
    """A resolver that enables nothing, which is what a real one defaults to."""

    async def is_enabled(self, host_id: uuid.UUID, capability: Capability) -> bool:
        return False


# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------


class TestRoleOrdering:
    def test_roles_are_strictly_ordered(self) -> None:
        assert role_rank(UserRole.VIEWER) < role_rank(UserRole.OPERATOR)
        assert role_rank(UserRole.OPERATOR) < role_rank(UserRole.ADMIN)

    def test_every_role_is_ranked(self) -> None:
        # An unranked role would raise rather than default to Viewer. A silent
        # default would be a privilege bug on the safe-looking side, but it
        # would also mean a new role was never reviewed here.
        for role in UserRole:
            assert isinstance(role_rank(role), int)

    @pytest.mark.parametrize(
        ("actual", "minimum", "expected"),
        [
            (UserRole.VIEWER, UserRole.VIEWER, True),
            (UserRole.VIEWER, UserRole.OPERATOR, False),
            (UserRole.VIEWER, UserRole.ADMIN, False),
            (UserRole.OPERATOR, UserRole.VIEWER, True),
            (UserRole.OPERATOR, UserRole.OPERATOR, True),
            (UserRole.OPERATOR, UserRole.ADMIN, False),
            (UserRole.ADMIN, UserRole.VIEWER, True),
            (UserRole.ADMIN, UserRole.OPERATOR, True),
            (UserRole.ADMIN, UserRole.ADMIN, True),
        ],
    )
    def test_role_satisfies(self, actual: UserRole, minimum: UserRole, expected: bool) -> None:
        assert role_satisfies(actual, minimum) is expected

    def test_higher_roles_admit_everything_lower_ones_do(self) -> None:
        # The property that makes a minimum-role test sufficient. If it ever
        # fails, the permission in question belongs on the capability axis
        # instead of becoming a new role (ADR-023).
        ordered = [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN]
        for index, higher in enumerate(ordered):
            for lower in ordered[: index + 1]:
                assert role_satisfies(higher, lower)


# ---------------------------------------------------------------------------
# The role axis
# ---------------------------------------------------------------------------


class TestRoleAuthorization:
    async def test_sufficient_role_is_permitted(self) -> None:
        authorizer = Authorizer()

        await authorizer.authorize(
            AuthorizationRequest(
                session=session_for(UserRole.ADMIN), minimum_role=UserRole.OPERATOR
            )
        )

    async def test_insufficient_role_is_refused(self) -> None:
        authorizer = Authorizer()

        with pytest.raises(InsufficientRoleError) as exc_info:
            await authorizer.authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.VIEWER), minimum_role=UserRole.ADMIN
                )
            )

        # The specifics are available server-side for the log.
        assert exc_info.value.required is UserRole.ADMIN
        assert exc_info.value.actual is UserRole.VIEWER

    async def test_viewer_minimum_admits_every_role(self) -> None:
        authorizer = Authorizer()

        for role in UserRole:
            await authorizer.authorize(
                AuthorizationRequest(session=session_for(role), minimum_role=UserRole.VIEWER)
            )

    async def test_authorization_does_not_re_check_the_account_state(self) -> None:
        # Deliberate separation: whether a session is usable at all — including
        # `is_active` — is authentication's job, re-checked on every request by
        # SessionService.resolve. Authorization answers a different question,
        # and duplicating the check here would invite the two drifting apart.
        authorizer = Authorizer()

        await authorizer.authorize(
            AuthorizationRequest(
                session=session_for(UserRole.ADMIN, is_active=False),
                minimum_role=UserRole.ADMIN,
            )
        )


# ---------------------------------------------------------------------------
# The capability axis
# ---------------------------------------------------------------------------


class TestCapabilityAxis:
    def test_no_resolver_is_configured_by_default(self) -> None:
        # Per-host capability configuration does not exist yet, so the second
        # axis is genuinely unavailable rather than quietly permissive.
        assert Authorizer().supports_capabilities is False

    def test_a_resolver_makes_the_axis_available(self) -> None:
        assert Authorizer(_AllowAll()).supports_capabilities is True

    def test_a_stub_resolver_satisfies_the_protocol(self) -> None:
        # Guards the seam: the protocol is what a future implementation has
        # to match.
        assert isinstance(_AllowAll(), CapabilityResolver)
        assert isinstance(_DenyAll(), CapabilityResolver)

    async def test_capability_request_without_a_resolver_is_denied(self) -> None:
        # The property that matters: an authorization question that cannot be
        # answered is answered "no", never "yes".
        authorizer = Authorizer()

        with pytest.raises(CapabilityNotAvailableError):
            await authorizer.authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.ADMIN),
                    minimum_role=UserRole.ADMIN,
                    capability=Capability.DOCKER_CONTROL,
                    host_id=uuid.uuid4(),
                )
            )

    async def test_role_is_checked_before_the_capability(self) -> None:
        # A caller whose role is too low is refused regardless of host
        # configuration, and without consulting the resolver.
        authorizer = Authorizer(_AllowAll())

        with pytest.raises(InsufficientRoleError):
            await authorizer.authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.VIEWER),
                    minimum_role=UserRole.ADMIN,
                    capability=Capability.DOCKER_READ,
                    host_id=uuid.uuid4(),
                )
            )

    async def test_capability_has_its_own_role_floor(self) -> None:
        # An endpoint may demand a lower role than the capability does; both
        # apply. Here the endpoint asks only for Viewer, but the terminal
        # capability is Administrator-only.
        authorizer = Authorizer(_AllowAll())

        with pytest.raises(InsufficientRoleError) as exc_info:
            await authorizer.authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.OPERATOR),
                    minimum_role=UserRole.VIEWER,
                    capability=Capability.TERMINAL,
                    host_id=uuid.uuid4(),
                )
            )

        assert exc_info.value.required is UserRole.ADMIN

    async def test_disabled_capability_is_refused(self) -> None:
        authorizer = Authorizer(_DenyAll())
        host_id = uuid.uuid4()

        with pytest.raises(CapabilityNotEnabledError) as exc_info:
            await authorizer.authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.ADMIN),
                    minimum_role=UserRole.VIEWER,
                    capability=Capability.DOCKER_CONTROL,
                    host_id=host_id,
                )
            )

        assert exc_info.value.capability is Capability.DOCKER_CONTROL
        assert exc_info.value.host_id == host_id

    async def test_enabled_capability_is_permitted(self) -> None:
        authorizer = Authorizer(_AllowAll())

        await authorizer.authorize(
            AuthorizationRequest(
                session=session_for(UserRole.ADMIN),
                minimum_role=UserRole.VIEWER,
                capability=Capability.DOCKER_CONTROL,
                host_id=uuid.uuid4(),
            )
        )

    def test_terminal_is_administrator_only(self) -> None:
        # The most dangerous capability in the product (ADR-022). Pinned so a
        # future edit to the table cannot quietly lower it.
        assert CAPABILITY_MINIMUM_ROLE[Capability.TERMINAL] is UserRole.ADMIN

    def test_every_capability_has_a_role_floor(self) -> None:
        # A capability missing from the table would raise KeyError at check
        # time; catching that here is better than at 3am.
        for capability in Capability:
            assert capability in CAPABILITY_MINIMUM_ROLE

    def test_control_capabilities_require_at_least_operator(self) -> None:
        for capability in Capability:
            if capability.value.endswith(".control"):
                assert role_satisfies(CAPABILITY_MINIMUM_ROLE[capability], UserRole.OPERATOR), (
                    capability
                )

    def test_read_capabilities_are_available_to_viewers(self) -> None:
        for capability in Capability:
            if capability.value.endswith(".read"):
                assert CAPABILITY_MINIMUM_ROLE[capability] is UserRole.VIEWER


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestAuthorizationRequest:
    def test_capability_requires_a_host(self) -> None:
        # A capability is a property of a specific host, so a request naming
        # one without the other is incoherent and is refused at construction
        # rather than silently skipping the capability check.
        with pytest.raises(ValueError, match="requires a host_id"):
            AuthorizationRequest(
                session=session_for(UserRole.ADMIN),
                minimum_role=UserRole.VIEWER,
                capability=Capability.TERMINAL,
            )

    def test_host_requires_a_capability(self) -> None:
        with pytest.raises(ValueError, match="without a capability"):
            AuthorizationRequest(
                session=session_for(UserRole.ADMIN),
                minimum_role=UserRole.VIEWER,
                host_id=uuid.uuid4(),
            )

    def test_role_only_request_is_valid(self) -> None:
        request = AuthorizationRequest(
            session=session_for(UserRole.ADMIN), minimum_role=UserRole.VIEWER
        )
        assert request.capability is None
        assert request.host_id is None

    def test_request_is_immutable(self) -> None:
        request = AuthorizationRequest(
            session=session_for(UserRole.ADMIN), minimum_role=UserRole.VIEWER
        )
        with pytest.raises(AttributeError):
            request.minimum_role = UserRole.VIEWER  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    async def test_unavailable_capability_is_logged_as_an_error(self) -> None:
        from structlog.testing import capture_logs

        # Reaching this means a call site asked for a capability before the
        # configuration exists — a programming error, logged as one, and still
        # denied.
        with capture_logs() as events, pytest.raises(CapabilityNotAvailableError):
            await Authorizer().authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.ADMIN),
                    minimum_role=UserRole.VIEWER,
                    capability=Capability.DOCKER_READ,
                    host_id=uuid.uuid4(),
                )
            )

        assert len(events) == 1
        assert events[0]["event"] == "capability_check_unavailable"
        assert events[0]["log_level"] == "error"

    async def test_role_denial_is_not_logged_by_the_authorizer(self) -> None:
        from structlog.testing import capture_logs

        # The API dependency logs denials with request context; logging here
        # too would double every entry.
        with capture_logs() as events, pytest.raises(InsufficientRoleError):
            await Authorizer().authorize(
                AuthorizationRequest(
                    session=session_for(UserRole.VIEWER), minimum_role=UserRole.ADMIN
                )
            )

        assert events == []
