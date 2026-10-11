"""Security-relevant configuration validation.

Milestone 1.5. Each of these refusals exists because the corresponding
misconfiguration is silent at runtime: it either weakens a protection without
saying so, or breaks a protection in a way that reports the wrong cause.
Failing at startup is the point.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from hcc.config import (
    KNOWN_NON_SETTING_VARIABLES,
    Settings,
    load_settings_or_exit,
    unknown_environment_variables,
)

DSN = "postgresql://hcc:hcc@127.0.0.1:5432/hcc"


def _settings(**overrides: Any) -> Settings:
    return Settings(database_url=DSN, **overrides)


class TestCorsOrigins:
    def test_wildcard_is_refused(self) -> None:
        """The verified defect.

        ``allow_credentials=True`` is set because this API authenticates with
        cookies. Starlette answers a wildcard in that configuration by echoing
        whichever ``Origin`` asked, with
        ``Access-Control-Allow-Credentials: true`` — so every website could
        read authenticated responses. Refused rather than silently narrowed,
        because an operator who wrote ``*`` wants something that must not be
        provided.
        """
        with pytest.raises(ValidationError, match="not permitted"):
            _settings(cors_origins=("*",))

    def test_wildcard_is_refused_even_alongside_real_origins(self) -> None:
        with pytest.raises(ValidationError, match="not permitted"):
            _settings(cors_origins=("https://hcc.example.com", "*"))

    def test_explicit_origins_are_accepted_and_canonicalised(self) -> None:
        settings = _settings(cors_origins=("HTTPS://HCC.Example.com:443/", "http://localhost:5173"))
        assert settings.cors_origins == ("https://hcc.example.com", "http://localhost:5173")

    def test_empty_is_the_default(self) -> None:
        # Same-origin deployment is the preferred shape and needs no CORS
        # entry at all.
        assert _settings().cors_origins == ()

    @pytest.mark.parametrize(
        "value", ["null", "example.com", "https://example.com/app", "ftp://example.com"]
    )
    def test_unusable_entries_are_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _settings(cors_origins=(value,))


class TestPublicOrigin:
    def test_is_canonicalised(self) -> None:
        assert (
            _settings(public_origin="HTTPS://HCC.Example.com/").public_origin
            == "https://hcc.example.com"
        )

    def test_none_means_derive_from_the_request(self) -> None:
        assert _settings().public_origin is None

    def test_blank_is_treated_as_unset(self) -> None:
        # A blank environment variable reads more naturally as "unset" than as
        # a validation failure.
        assert _settings(public_origin="   ").public_origin is None

    def test_a_wildcard_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(public_origin="*")

    def test_a_url_with_a_path_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="no path"):
            _settings(public_origin="https://hcc.example.com/dashboard")


class TestTrustedProxies:
    def test_empty_is_the_default(self) -> None:
        # Neither trusting nor ignoring forwarded headers is safe in general,
        # so the operator states where the proxy is. Defaulting to loopback
        # would be wrong for the Compose topology, where the proxy is a
        # sibling container.
        assert _settings().trusted_proxies == ()
        assert _settings().trusted_proxy_networks == ()

    def test_addresses_and_networks_are_parsed(self) -> None:
        settings = _settings(trusted_proxies=("127.0.0.1", "172.16.0.0/12"))
        assert [str(network) for network in settings.trusted_proxy_networks] == [
            "127.0.0.1/32",
            "172.16.0.0/12",
        ]

    def test_an_unparseable_entry_is_refused(self) -> None:
        # Silently dropping it would leave the operator believing the proxy is
        # trusted when it is not — and nothing would report the difference.
        with pytest.raises(ValidationError, match="not a valid IP address"):
            _settings(trusted_proxies=("127.0.0.1", "10.0.0.300"))

    def test_the_parsed_form_is_stable_across_reads(self) -> None:
        # Consulted on every request, so it is cached. The cache is keyed on
        # the immutable tuple, so it cannot serve a stale value.
        settings = _settings(trusted_proxies=("172.16.0.0/12",))
        assert settings.trusted_proxy_networks == settings.trusted_proxy_networks


class TestThrottleBudgetOrdering:
    def test_defaults_are_safe(self) -> None:
        settings = _settings()
        assert settings.login_rate_limit_per_address == 20
        assert settings.login_rate_limit_per_address_username == 10
        assert settings.login_rate_limit_per_username == 100
        # The invariant that prevents the single-source lockout.
        assert (
            settings.login_rate_limit_per_username > settings.login_rate_limit_per_address_username
        )

    def test_a_username_ceiling_at_or_below_the_per_source_budget_is_refused(self) -> None:
        """The configuration form of the verified lockout defect.

        If one source may spend as much of an account's global budget as the
        budget holds, that source can exhaust it and deny the account access
        indefinitely. Enforced rather than documented, because the unsafe
        relationship is not visible in either number alone.
        """
        with pytest.raises(ValidationError, match="must be greater than"):
            _settings(
                login_rate_limit_per_address_username=10,
                login_rate_limit_per_username=10,
            )

    def test_an_unreachable_per_account_budget_is_refused(self) -> None:
        # A per-source-per-account budget above the source's overall budget can
        # never be reached, so configuring one is misleading.
        with pytest.raises(ValidationError, match="must not exceed"):
            _settings(
                login_rate_limit_per_address=5,
                login_rate_limit_per_address_username=10,
            )

    def test_a_coherent_custom_set_is_accepted(self) -> None:
        settings = _settings(
            login_rate_limit_per_address=50,
            login_rate_limit_per_address_username=5,
            login_rate_limit_per_username=200,
        )
        assert settings.login_rate_limit_per_username == 200


class TestProductionOriginCoherence:
    def test_production_requires_an_origin_or_a_trusted_proxy(self) -> None:
        """Refuse a production setup that cannot validate its own origin.

        With neither setting, the application compares a browser's
        ``https://`` origin against its own internal ``http://`` view and
        rejects every state-changing request with a CSRF error naming the
        wrong cause. That was the verified HIGH-severity defect; failing at
        startup is strictly better than a deployment where login works and
        logout does not.
        """
        with pytest.raises(ValidationError, match="HCC_PUBLIC_ORIGIN"):
            _settings(environment="production", session_cookie_secure=True)

    def test_a_public_origin_satisfies_it(self) -> None:
        settings = _settings(
            environment="production",
            session_cookie_secure=True,
            public_origin="https://hcc.example.com",
        )
        assert settings.is_production is True

    def test_a_trusted_proxy_satisfies_it(self) -> None:
        # The alternative: let the application believe X-Forwarded-Proto from
        # the declared proxy and derive the origin per request.
        settings = _settings(
            environment="production",
            session_cookie_secure=True,
            trusted_proxies=("172.16.0.0/12",),
        )
        assert settings.trusted_proxies == ("172.16.0.0/12",)

    def test_development_needs_neither(self) -> None:
        # Direct local HTTP development derives its own origin correctly and
        # must not require ceremony.
        assert _settings(environment="development").public_origin is None
        assert _settings(environment="test").public_origin is None

    def test_the_message_names_both_variables(self) -> None:
        with pytest.raises(ValidationError, match="HCC_TRUSTED_PROXIES"):
            _settings(environment="production", session_cookie_secure=True)


class TestRemovedSettings:
    def test_the_old_proxy_header_setting_is_gone(self) -> None:
        """``trusted_proxy_headers`` was replaced by ``trusted_proxies``.

        The old setting trusted a *named header* unconditionally, which is the
        unconditional-trust problem itself. ``extra="forbid"`` means a
        deployment still carrying it fails at startup rather than silently
        losing the protection it thought it had configured.
        """
        with pytest.raises(ValidationError):
            _settings(trusted_proxy_headers=("x-forwarded-for",))


class TestUnknownEnvironmentVariables:
    """Unrecognised ``HCC_`` variables are refused, not ignored.

    ``extra="forbid"`` rejects an unknown constructor argument, but
    pydantic-settings simply ignores an environment variable matching no
    field — and the environment is the only configuration channel the
    deployment uses. A typo therefore left the default in force silently,
    while ``docs/SECURITY.md`` claimed the opposite. Found by a smoke check
    added in this milestone.
    """

    def test_a_typo_is_detected(self) -> None:
        # The realistic case: one missing character.
        assert unknown_environment_variables(
            {"HCC_DATABASE_URL": DSN, "HCC_SESSION_LIFETIME_SECOND": "99"}
        ) == ["HCC_SESSION_LIFETIME_SECOND"]

    def test_the_removed_proxy_header_setting_is_detected(self) -> None:
        # So a deployment carrying it learns that it now does nothing, rather
        # than losing the protection it thought it had configured.
        assert unknown_environment_variables(
            {"HCC_TRUSTED_PROXY_HEADERS": '["x-forwarded-for"]'}
        ) == ["HCC_TRUSTED_PROXY_HEADERS"]

    def test_real_settings_are_accepted(self) -> None:
        assert (
            unknown_environment_variables(
                {
                    "HCC_DATABASE_URL": DSN,
                    "HCC_ENVIRONMENT": "production",
                    "HCC_PUBLIC_ORIGIN": "https://hcc.example.com",
                    "HCC_TRUSTED_PROXIES": '["127.0.0.1"]',
                    "HCC_LOGIN_RATE_LIMIT_PER_ADDRESS_USERNAME": "10",
                }
            )
            == []
        )

    def test_variables_without_the_prefix_are_ignored(self) -> None:
        # Not ours to police.
        assert unknown_environment_variables({"PATH": "/usr/bin", "DATABASE_URL": DSN}) == []

    def test_the_test_harness_variable_is_exempt(self) -> None:
        # It shares the prefix for discoverability but is not a setting, so it
        # is exempted by name — a pattern would reopen the hole.
        assert "HCC_TEST_DATABASE_URL" in KNOWN_NON_SETTING_VARIABLES
        assert unknown_environment_variables({"HCC_TEST_DATABASE_URL": DSN}) == []

    def test_every_field_is_recognised(self) -> None:
        # Guards the prefix-and-case derivation: if it were wrong, every
        # legitimate variable would be reported as unknown.
        environ = {f"HCC_{name.upper()}": "x" for name in Settings.model_fields}

        assert unknown_environment_variables(environ) == []

    def test_the_startup_path_refuses_and_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HCC_DATABASE_URL", DSN)
        monkeypatch.setenv("HCC_NOT_A_SETTING", "x")

        with pytest.raises(SystemExit) as caught:
            load_settings_or_exit()

        assert caught.value.code == 2
        error = capsys.readouterr().err
        assert "HCC_NOT_A_SETTING" in error
        assert "refusing to start" in error
