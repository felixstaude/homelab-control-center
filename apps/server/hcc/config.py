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
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, PostgresDsn, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hcc.security.forwarded import TrustedNetworks, parse_trusted_networks
from hcc.security.origins import normalize_configured_origin

# `Path` above is a runtime import for two reasons: it annotates pydantic
# fields on `Settings`, which pydantic resolves at runtime to build its
# validators, and `_assemble_database_url_from_parts` calls it. The first is
# the trap — a type that exists only for the type checker makes the model
# unconstructable, with a confusing `NameError` at import rather than a type
# error. Same constraint FastAPI imposes in hcc/api/deps.py.
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


#: Characters permitted in an assembled database password.
#:
#: base64url's alphabet. Anything outside it has a meaning inside a URL's
#: userinfo — "/" and "@" in particular change which host is addressed — and
#: the resulting failure is a connection error about DNS rather than about the
#: password.
_URL_SAFE_PASSWORD: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

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

    # Where the built web client lives, if this deployment serves one.
    #
    # Set in the container image, which serves the API and the client from one
    # origin — the arrangement the session cookies, the CSRF Origin check and
    # the empty production CORS list are all written for. Unset in
    # development, where Vite serves the client on :5173 and `cors_origins`
    # names it.
    #
    # A path that is set and holds no built client makes startup fail, because
    # the alternative is a container that reports itself ready and answers
    # every page with a 404 — healthy, and useless. See hcc/web.py.
    web_root: Path | None = None

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
    #
    # It may be *assembled* rather than given — see
    # :meth:`_assemble_database_url_from_parts` below and the five fields
    # under it. Either way this field is required after validation.
    database_url: PostgresDsn

    # The parts a database URL is assembled from when one is not given
    # directly.
    #
    # ## Why the password comes from a file
    #
    # A container's environment appears in `docker inspect`, and
    # `docker inspect` is what people paste into issue reports. A file does
    # not. The Compose deployment generates the password into one on first
    # start and passes the *path*.
    #
    # ## Why this is in the application and not the entrypoint
    #
    # It was in the entrypoint, which assembled the URL and exported it before
    # exec-ing the server. That worked for the server and broke the documented
    # first-run command: `docker compose exec hcc hcc bootstrap-admin` does
    # not go through the entrypoint, so it saw no URL and failed with
    # "HCC_DATABASE_URL: Field required" on a perfectly good deployment
    # (ADR-033 makes that command the only way to create the first
    # administrator, so this was the one step that had to work).
    #
    # Here, every process in the container gets it: the server, `hcc`
    # anything, and alembic — which resolves its URL through these settings
    # too.
    database_password_file: Path | None = None
    database_host: str | None = None
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str = "hcc"
    database_name: str = "hcc"
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

    # --- Password change throttling ---------------------------------------
    # A password change costs *two* Argon2 operations — one verify and one
    # hash — so ~128 MiB and ~100 ms per request. The endpoint requires a
    # session, so an anonymous caller cannot reach it, but an authenticated
    # one could still use it as a CPU and memory amplifier.
    #
    # Keyed on the user id and kept deliberately separate from the login
    # budgets rather than reusing them: sharing would mean a user who changes
    # their password a few times has spent the budget they need in order to
    # sign in, which is a self-inflicted lockout created by a throttle meant
    # to prevent one. Five per window is far above any legitimate use and far
    # below a useful amplifier.
    password_change_rate_limit_per_user: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="Password change attempts per account per window.",
    )
    password_change_rate_limit_window_seconds: int = Field(
        default=300, ge=1, le=86400, description="Password change throttle window."
    )

    # --- Agent enrollment and connectivity --------------------------------
    # How long a one-time enrollment credential stays usable. Short by design:
    # this is the secret that gets pasted into a terminal, lands in shell
    # history and is read over somebody's shoulder, and a short window is what
    # makes that exposure survivable (docs/AGENT_ARCHITECTURE.md §4.1).
    #
    # Fifteen minutes is enough to walk to the machine and run one command,
    # and not enough to leave a live credential in a chat log over a weekend.
    # The floor of 60 seconds stops a value so small that enrollment could
    # never succeed; the ceiling of 24 hours stops "short lived" being
    # configured away entirely — an invariant, not a capability
    # (docs/PRODUCT_VISION.md).
    agent_enrollment_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
        description="Lifetime of a one-time agent enrollment credential.",
    )
    # Where the agent release manifest lives, if this build has one.
    #
    # The interface renders an install command carrying the expected SHA-256 of
    # the archive it tells somebody to download, and that digest has to come
    # from here rather than from the download origin — the administrator is
    # already authenticated to this controller over TLS, so it is the thing
    # that can vouch for the bytes (see hcc/agents/releases.py).
    #
    # None means "no release has been published for this build", which is the
    # honest default for a source checkout and for every image built so far.
    # The endpoint then says so and the interface shows the build-from-source
    # path, rather than a command pointing at a URL that would not resolve.
    #
    # A path that is set and unusable is a deployment fault and is reported as
    # one, which is why absence and breakage are different states here.
    agent_release_manifest: Path | None = None

    # Publisher keys whose signatures over the release manifest this
    # controller will accept, base64 raw Ed25519.
    #
    # Set as a JSON array, matching `HCC_CORS_ORIGINS` — pydantic-settings
    # treats a tuple field as complex and JSON-parses it from the
    # environment, so `HCC_RELEASE_SIGNING_KEYS=["AAAA…"]`. Comma-splitting
    # would need a custom parser and a second convention for list settings in
    # one file.
    #
    # **A public key is configuration, not a secret.** It is safe in
    # `docker inspect`, in a committed `.env.example` and in a support ticket —
    # the whole point of the scheme is that knowing it lets you *verify* and
    # not sign. So it is an ordinary setting rather than a file like the
    # database password, and the deployment documentation can print it.
    #
    # Empty by default, which means the controller reports the manifest's
    # signature as `untrusted_key` rather than `valid`. That is honest: this
    # build has no key stamped in because nothing has been published or
    # signed. The *agent* carries its own copy and is the party whose
    # verification is load-bearing; this one exists so the interface can tell
    # an administrator whether the metadata it is serving is authentic.
    #
    # A list rather than one value, so a key can be rotated by trusting both
    # for a window rather than by a flag day across a fleet.
    release_signing_keys: tuple[str, ...] = ()

    # How long a recorded connection may go without a heartbeat before the
    # interface reports it as stale rather than connected. Three heartbeat
    # intervals, so one dropped heartbeat does not flap the status while a
    # genuine failure surfaces inside a minute and a half.
    #
    # There is no setting that disables staleness detection, and that is
    # deliberate: a monitoring tool whose liveness check can be turned off
    # reports "connected" for a fleet it has not heard from, which is the one
    # thing product principle 2 forbids.
    agent_heartbeat_grace_seconds: int = Field(
        default=90,
        ge=10,
        le=3600,
        description="Heartbeat silence tolerated before a host reads as stale.",
    )

    # --- Metric storage and retention -------------------------------------
    #
    # Defaults from docs/DATA_RETENTION.md §2's "CPU, memory, load" row, which
    # is every category this phase collects. Per-*category* retention is
    # deferred: it needs a configuration surface of its own and the categories
    # here all want the same numbers.
    #
    # Each has a floor and a ceiling, for the reasons §6.4 gives. The floor of
    # one hour on raw data is what stops retention being configured to
    # "effectively off", which would make the product's history claims false;
    # the ceiling of a year on raw is a hard limit regardless of disk, because
    # beyond it the rollup tiers are what an operator should be using.
    metrics_raw_retention_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="How long full-resolution samples are kept.",
    )
    metrics_5m_retention_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="How long five-minute aggregates are kept.",
    )
    metrics_1h_retention_days: int = Field(
        default=730,
        ge=1,
        le=3650,
        description="How long hourly aggregates are kept.",
    )

    # How long a bucket must be closed before it is rolled up. An agent
    # reconnecting after an outage backfills with its original timestamps, so
    # the newest buckets are still changing (DATA_RETENTION.md §3.2).
    metrics_rollup_lateness_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="Grace period before a bucket is considered sealed.",
    )

    # How often the maintenance job runs: create partitions, roll up, expire.
    #
    # Five minutes. The job is cheap when there is nothing to do — a handful of
    # catalogue reads — and the interval has to be well under the narrowest
    # partition width (one day) so a missed run is never the reason inserts
    # start landing in the DEFAULT partition.
    #
    # Zero disables it, which is what the test suite and a one-shot
    # maintenance command use. Not offered as an operational setting: a
    # deployment with the job disabled silently stops expiring data, so the
    # startup log says so when it is off.
    metrics_maintenance_interval_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="How often partition maintenance, rollups and retention run. 0 disables.",
    )

    # --- Automatic update policies ----------------------------------------
    # How often every host's automatic update policy is evaluated.
    #
    # Five minutes, the same cadence as metric maintenance and for a similar
    # reason: the thing being watched changes on the order of minutes — a new
    # manifest after a controller upgrade, a maintenance window opening — and
    # a tighter interval would walk the fleet for nothing.
    #
    # The interval is also the **precision of a maintenance window**. A window
    # of 02:00 to 04:00 evaluated every five minutes starts an update within
    # minutes of 02:00, which is what somebody who wrote "02:00" meant. A
    # window shorter than this interval might be missed entirely, which is why
    # the interface's hint recommends at least an hour.
    #
    # Zero disables it, and then an automatic policy does nothing: no host
    # updates itself, whatever its policy says. The startup log says so when
    # it is off, because a deployment where automatic updates are configured
    # and silently never happen is the worst available state.
    update_policy_interval_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description=(
            "How often automatic update policies are evaluated and expired "
            "instructions are swept. 0 disables automatic updates entirely."
        ),
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

    @model_validator(mode="before")
    @classmethod
    def _assemble_database_url_from_parts(cls, values: object) -> object:
        """Build ``database_url`` from a password file when one is not given.

        ``mode="before"``, so the assembled string goes through
        ``PostgresDsn``'s own validation exactly as a supplied one does — and
        so ``database_url`` stays a required field rather than becoming an
        optional one with a reconstruction step after it.

        An explicit ``database_url`` always wins: an operator pointing at an
        existing PostgreSQL gives the whole URL and none of this runs.
        """
        if not isinstance(values, dict):
            return values
        if values.get("database_url"):
            return values

        password_file = values.get("database_password_file")
        if not password_file:
            # Left alone: ``database_url`` is required, so validation produces
            # its own error. The message is sharpened in
            # :func:`load_settings_or_exit`, which knows about both forms.
            return values

        path = Path(str(password_file))
        try:
            password = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(
                f"database_password_file is {path} and cannot be read: {error}"
            ) from error
        if not password:
            raise ValueError(f"database_password_file is {path} and is empty")

        # The password goes into a URL's userinfo, where "/" and "@" change
        # which host is being addressed. The Compose stack generates a
        # base64url password for exactly this reason; checked here rather than
        # trusted, because the failure otherwise is a connection error about
        # DNS on a correctly configured deployment.
        if any(character not in _URL_SAFE_PASSWORD for character in password):
            raise ValueError(
                f"the password in {path} contains characters that are not safe in a "
                "URL. Regenerate it from [A-Za-z0-9_-], or set HCC_DATABASE_URL "
                "yourself with the password percent-encoded."
            )

        host = values.get("database_host")
        if not host:
            raise ValueError(
                "database_password_file is set and database_host is not; the URL "
                "cannot be assembled without knowing which host to connect to"
            )
        port = values.get("database_port", 5432)
        user = values.get("database_user", "hcc")
        name = values.get("database_name", "hcc")
        values["database_url"] = f"postgresql://{user}:{password}@{host}:{port}/{name}"
        return values

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
#: Each is read by something *other* than this model and shares the prefix for
#: discoverability, so each has to be exempted by name. Deliberately not a
#: pattern: a pattern would reopen the hole the unknown-variable check exists
#: to close, which is a typo'd setting being silently ignored.
#:
#: Adding a variable to the container entrypoint without adding it here makes
#: the application **refuse to start** — which is the check working, and is
#: also why ``tests/test_deployment.py`` asserts that the entrypoint and the
#: Compose file name nothing this set and the model do not between them cover.
KNOWN_NON_SETTING_VARIABLES: Final = frozenset(
    {
        # The integration-test harness reads this to decide whether to run
        # database-backed tests.
        "HCC_TEST_DATABASE_URL",
        # Read by deploy/docker-entrypoint.sh, which runs before the
        # application and is not configured by it.
        #
        # Whether to run `alembic upgrade head` before binding a port. False
        # is for an operator who would rather run the migration themselves and
        # watch it.
        #
        # The only one. The database parts used to be here too, assembled into
        # a URL by the entrypoint — until `docker compose exec hcc hcc
        # bootstrap-admin` turned out not to go through the entrypoint and so
        # saw no URL at all. They are real settings now.
        "HCC_AUTO_MIGRATE",
    }
)


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
        missing_database_url = False
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            if location:
                print(f"  HCC_{location.upper()}: {error['msg']}", file=sys.stderr)
            else:
                # A model-level validator failed, so there is no field to
                # name. Printing "HCC_:" for it — which this did — invents a
                # variable the operator will then go looking for.
                print(f"  {error['msg']}", file=sys.stderr)
            if location == "database_url" and error["type"] == "missing":
                missing_database_url = True
        if missing_database_url:
            # "Field required" is true and unhelpful: there are two ways to
            # supply it and the deployment uses the second one.
            print(
                "\nThe database can be named either way:\n"
                "  HCC_DATABASE_URL=postgresql://user:password@host:5432/hcc\n"
                "or, to keep the password out of the environment:\n"
                "  HCC_DATABASE_PASSWORD_FILE=/run/secrets/db_password\n"
                "  HCC_DATABASE_HOST=postgres",
                file=sys.stderr,
            )
        print("\nSee apps/server/.env.example for the expected values.", file=sys.stderr)
        raise SystemExit(2) from exc
