"""Application configuration.

All configuration is read from the environment (or a local ``.env`` file during
development) and validated once at startup. There are no hardcoded credentials
and no runtime mutation of settings: if the environment is invalid the process
fails immediately with a readable error rather than degrading later.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, PostgresDsn, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hcc.security.forwarded import TrustedNetworks, parse_trusted_networks
from hcc.security.origins import normalize_configured_origin

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Environment-variable prefix. Declared once so the settings model and the
#: unknown-variable check below cannot disagree about it.
_ENV_PREFIX: Final = "HCC_"


@lru_cache(maxsize=32)
def _parse_networks_cached(entries: tuple[str, ...]) -> TrustedNetworks:
    """Parse trusted-proxy entries once per distinct configuration.

    Keyed on the tuple rather than memoised on the settings instance, because
    ``Settings`` is frozen and a module-level cache keyed by an immutable
    value cannot go stale. Validation already guaranteed these parse.
    """
    return parse_trusted_networks(entries)


Environment = Literal["development", "test", "production"]
LogFormat = Literal["console", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
CookieSameSite = Literal["lax", "strict", "none"]
DatabasePoolClass = Literal["queue", "null"]


class Settings(BaseSettings):
    """Validated application settings.

    Every field maps to an ``HCC_``-prefixed environment variable, e.g.
    ``HCC_DATABASE_URL``. See ``.env.example`` for the full documented set.
    """

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
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
    #
    # Validated and canonicalised at startup. `*` is refused: this API sends
    # session cookies, and a wildcard with credentials enabled makes Starlette
    # echo whichever origin asked. See hcc/security/origins.py.
    cors_origins: tuple[str, ...] = ()

    # The origin a browser actually reaches this deployment on, e.g.
    # `https://hcc.example.com`. Set this when a TLS-terminating reverse proxy
    # sits in front, because the application then sees plain HTTP internally
    # while the browser sends an `https://` Origin — and a comparison against
    # the internal scheme would reject every state-changing request.
    #
    # None means "derive it from the request", which is correct for direct
    # local development and for a proxy declared in `trusted_proxies`. See
    # hcc/api/deps.py and docs/SECURITY.md.
    public_origin: str | None = None

    # Addresses or CIDR networks of reverse proxies whose `X-Forwarded-For`
    # and `X-Forwarded-Proto` headers may be believed.
    #
    # Empty by default, and that default is deliberate in both directions:
    # trusting these headers unconditionally lets any caller forge its own
    # address and bypass per-address throttling, while ignoring them behind a
    # proxy collapses every client onto the proxy's single budget. Neither is
    # safe, so the operator states where the proxy is. See
    # hcc/security/forwarded.py.
    trusted_proxies: tuple[str, ...] = ()

    # --- Database ---------------------------------------------------------
    # No default. A missing database URL is a configuration error, not
    # something to paper over with a localhost guess that fails confusingly
    # later.
    database_url: PostgresDsn
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_echo: bool = False
    # `queue` keeps connections open between requests, which is what a
    # long-running server wants. `null` opens and closes one per checkout:
    # correct behind an external pooler such as PgBouncer, which does the
    # pooling itself and for which a second layer causes session-state bugs,
    # and for short-lived processes where pooling buys nothing.
    database_pool_class: DatabasePoolClass = "queue"

    # --- Password hashing -------------------------------------------------
    # Argon2id cost parameters. Defaults are the RFC 9106 "low memory"
    # (second recommended) profile. Configurable so an operator on
    # constrained hardware can tune them, with floors enforced so that
    # configuration cannot weaken the security property — the invariants
    # versus capabilities rule in docs/PRODUCT_VISION.md. See ADR-025 and
    # hcc/security/password.py for the full rationale.
    #
    # Memory is the dominant cost and is charged *per concurrent hash*:
    # 65536 KiB = 64 MiB each.
    password_hash_memory_cost: int = Field(
        default=65536, ge=19456, le=4194304, description="Argon2id memory cost in KiB."
    )
    # Floor of 2 passes; 1 pass is only appropriate at RFC 9106's 2 GiB profile.
    password_hash_time_cost: int = Field(default=3, ge=2, le=20, description="Argon2id iterations.")
    password_hash_parallelism: int = Field(
        default=4, ge=1, le=16, description="Argon2id parallel lanes."
    )
    # Caps simultaneous Argon2 operations, and therefore peak memory:
    # max_concurrency x memory_cost. Unbounded, a burst of login attempts
    # would exhaust memory, and the login endpoint is reachable without
    # authentication. See ADR-029.
    password_hash_max_concurrency: int = Field(
        default=4, ge=1, le=64, description="Simultaneous Argon2id operations."
    )

    # --- Sessions ---------------------------------------------------------
    session_cookie_name: str = "hcc_session"
    # Absolute lifetime. A session dies this long after login regardless of
    # activity, which bounds the damage from a stolen cookie. 12 hours suits a
    # dashboard someone leaves open during a working day.
    session_lifetime_seconds: int = Field(
        default=43200, ge=60, le=2592000, description="Absolute session lifetime."
    )
    # Idle timeout, enforced from `last_seen_at`. Zero disables it, leaving
    # only the absolute lifetime.
    session_idle_timeout_seconds: int = Field(
        default=3600, ge=0, le=2592000, description="Idle timeout; 0 disables."
    )
    # `last_seen_at` is only rewritten once per this interval, so an active
    # session does not cause a database write on every request.
    session_touch_interval_seconds: int = Field(
        default=60, ge=1, le=3600, description="Minimum gap between last_seen_at writes."
    )
    # Overrides the Secure cookie flag. None means "derive it": on in
    # production, off elsewhere so that plain-HTTP local development works.
    # Setting this False in production is refused at startup.
    session_cookie_secure: bool | None = None
    session_cookie_samesite: CookieSameSite = "lax"
    csrf_cookie_name: str = "hcc_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    # --- Login throttling -------------------------------------------------
    # Checked BEFORE password verification. Three budgets, all of which must
    # allow an attempt. See hcc/security/ratelimit.py for why three, and for
    # the lockout failure this shape exists to avoid.
    #
    #   per_address           one source, any account
    #   per_address_username  one source against one account
    #   per_username          every source against one account
    login_rate_limit_per_address: int = Field(
        default=20, ge=1, le=10000, description="Login attempts per source address per window."
    )
    login_rate_limit_per_address_username: int = Field(
        default=10,
        ge=1,
        le=10000,
        description="Login attempts per source address against one username per window.",
    )
    # Deliberately much higher than the per-address budget. A single source is
    # capped at `per_address_username` attempts against one account, so
    # reaching this global ceiling requires many distinct sources — which is
    # what stops one attacker from locking a known administrator out of their
    # own installation. The relationship is enforced below.
    login_rate_limit_per_username: int = Field(
        default=100,
        ge=1,
        le=100000,
        description="Login attempts against one username, from all sources, per window.",
    )
    login_rate_limit_window_seconds: int = Field(
        default=300, ge=1, le=86400, description="Throttle window."
    )

    # --- Security headers -------------------------------------------------
    # A strict Content-Security-Policy breaks the Vite dev server, which needs
    # inline scripts and a websocket for hot reload. Enabled in production,
    # off elsewhere, so development does not train people to ignore it.
    security_headers_enabled: bool = True
    content_security_policy_enabled: bool | None = None

    # --- Logging ----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    @field_validator("api_prefix")
    @classmethod
    def _validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("api_prefix must start with '/'")
        return value.rstrip("/")

    @field_validator("cors_origins")
    @classmethod
    def _validate_cors_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Canonicalise every CORS origin, refusing wildcards.

        Validated here rather than at the point of use so a bad value fails at
        startup with the variable named, and so the comparison in
        ``hcc/api/deps.py`` is against already-canonical strings.
        """
        return tuple(normalize_configured_origin(entry) for entry in value)

    @field_validator("public_origin")
    @classmethod
    def _validate_public_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            # An empty string in the environment means "unset", which is a
            # friendlier reading than failing on a blank variable.
            return None
        return normalize_configured_origin(value)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an unparseable proxy entry at startup.

        A typo'd CIDR that were silently dropped would leave the operator
        believing their proxy is trusted when it is not — so the forwarded
        headers would be ignored, every client would collapse onto the proxy's
        rate-limit budget, and nothing would say so.
        """
        parse_trusted_networks(value)
        return value

    @model_validator(mode="after")
    def _validate_cookie_security(self) -> Settings:
        """Refuse a configuration that would send session cookies in clear.

        An operator who sets ``session_cookie_secure=false`` in production has
        almost certainly done it to work around a missing reverse proxy, which
        means session cookies would travel unencrypted over the network. That
        is a silent, total compromise of the session mechanism, so it fails at
        startup instead.
        """
        if self.environment == "production" and self.session_cookie_secure is False:
            raise ValueError(
                "session_cookie_secure must not be disabled in production: "
                "session cookies would be sent over plain HTTP. Terminate TLS "
                "at a reverse proxy — see docs/SECURITY.md."
            )
        if self.session_cookie_samesite == "none" and not self.effective_cookie_secure:
            # SameSite=None without Secure is rejected by browsers anyway, so
            # accepting it would produce a confusing "cookie silently dropped"
            # failure rather than a clear one.
            raise ValueError(
                "session_cookie_samesite='none' requires secure cookies; "
                "browsers reject SameSite=None without Secure."
            )
        return self

    @model_validator(mode="after")
    def _validate_throttle_budgets(self) -> Settings:
        """Keep the per-username ceiling above what one source can spend.

        This is the invariant that prevents the lockout defect: if the global
        per-username budget were at or below what a single address may spend
        against one account, one attacker could exhaust a known
        administrator's budget and deny them access indefinitely. Requiring
        strictly more means reaching the ceiling takes several distinct
        sources.

        Enforced in configuration rather than only documented, because the
        values are operator-tunable and the unsafe relationship is not obvious
        from either number alone.
        """
        if self.login_rate_limit_per_username <= self.login_rate_limit_per_address_username:
            raise ValueError(
                "login_rate_limit_per_username "
                f"({self.login_rate_limit_per_username}) must be greater than "
                "login_rate_limit_per_address_username "
                f"({self.login_rate_limit_per_address_username}): otherwise a "
                "single source could exhaust one account's global budget and "
                "lock that user out. See docs/SECURITY.md."
            )
        if self.login_rate_limit_per_address_username > self.login_rate_limit_per_address:
            raise ValueError(
                "login_rate_limit_per_address_username "
                f"({self.login_rate_limit_per_address_username}) must not exceed "
                f"login_rate_limit_per_address ({self.login_rate_limit_per_address}): "
                "the per-account budget for one source can never be reached "
                "beyond that source's overall budget, so a larger value is "
                "misleading configuration."
            )
        return self

    @model_validator(mode="after")
    def _validate_proxy_origin_coherence(self) -> Settings:
        """Refuse a production setup that cannot validate its own origin.

        In production with a TLS-terminating proxy, the application needs one
        of two things to know what origin a browser will send: an explicit
        ``public_origin``, or a ``trusted_proxies`` entry letting it believe
        ``X-Forwarded-Proto``. With neither, it compares the browser's
        ``https://`` origin against its own ``http://`` view and rejects every
        state-changing request with a CSRF error that names the wrong cause.

        That was a real, verified defect. Failing at startup with an
        actionable message is strictly better than a deployment where login
        works and logout does not.
        """
        if self.is_production and self.public_origin is None and not self.trusted_proxies:
            raise ValueError(
                "a production deployment must set HCC_PUBLIC_ORIGIN (e.g. "
                "https://hcc.example.com) or HCC_TRUSTED_PROXIES (e.g. "
                "172.16.0.0/12). Without one of them the application cannot "
                "tell which origin browsers use, and every state-changing "
                "request fails CSRF validation behind an HTTPS reverse proxy. "
                "See docs/SECURITY.md."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def trusted_proxy_networks(self) -> TrustedNetworks:
        """The parsed form of :attr:`trusted_proxies`.

        Consulted on every request, so the parse is cached by the module-level
        :func:`_parse_networks_cached` rather than repeated. Settings are
        frozen and validated, so the cache can never serve a stale value.
        """
        return _parse_networks_cached(self.trusted_proxies)

    @property
    def effective_cookie_secure(self) -> bool:
        """Whether to set the ``Secure`` flag on cookies.

        Derived from the environment unless explicitly overridden, so that
        local HTTP development works without anyone having to disable a
        security flag by hand — and so production gets it without anyone
        having to remember to enable it.
        """
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    @property
    def effective_csp_enabled(self) -> bool:
        """Whether to send a Content-Security-Policy header.

        Off outside production by default: the Vite dev server needs inline
        scripts and a websocket, and a policy that has to be weakened for
        development is one nobody trusts in production.
        """
        if self.content_security_policy_enabled is not None:
            return self.content_security_policy_enabled
        return self.is_production

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


#: ``HCC_``-prefixed variables that are deliberately not settings.
#:
#: Only one: the integration-test harness reads ``HCC_TEST_DATABASE_URL`` to
#: decide whether to run database-backed tests. It shares the prefix for
#: discoverability, so it has to be exempted by name rather than by pattern —
#: a pattern would reopen the hole this check exists to close.
KNOWN_NON_SETTING_VARIABLES: Final = frozenset({"HCC_TEST_DATABASE_URL"})


def unknown_environment_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return ``HCC_``-prefixed variables that match no setting.

    ``extra="forbid"`` rejects an unknown *constructor argument*, but
    pydantic-settings simply ignores an environment variable that matches no
    field — and the environment is the only configuration channel this
    deployment uses (ARCHITECTURE §12.3). So a typo'd
    ``HCC_SESSION_LIFETIME_SECOND`` was silently discarded and the operator
    got the default while believing they had configured something.

    This closes that. It is a separate function rather than a validator
    because a validator sees resolved values, not the raw environment, and
    therefore cannot tell an ignored variable from an absent one.
    """
    source = os.environ if environ is None else environ
    known = {f"{_ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    return sorted(
        name
        for name in source
        if name.startswith(_ENV_PREFIX)
        and name not in known
        and name not in KNOWN_NON_SETTING_VARIABLES
    )


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
    unknown = unknown_environment_variables()
    if unknown:
        # Refused, not ignored: a variable that looks like configuration and
        # does nothing is worse than one that fails, because the operator
        # believes a setting is in force when the default is.
        print("Unrecognised HCC_ environment variables — refusing to start:\n", file=sys.stderr)
        for name in unknown:
            print(f"  {name}", file=sys.stderr)
        print(
            "\nCheck for a typo, or remove the variable. "
            "HCC_TRUSTED_PROXY_HEADERS was removed in milestone 1.5 — use "
            "HCC_TRUSTED_PROXIES.\nSee apps/server/.env.example for the full set.",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
