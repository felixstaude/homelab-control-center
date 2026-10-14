"""The deployment files and the application have to agree about settings.

## Why this is a test and not a review item

The application **refuses to start** on an unrecognised ``HCC_`` variable
(:func:`hcc.config.unknown_environment_variables`). That is deliberate: a
typo'd setting that was silently ignored would leave an operator believing
they had configured something. The consequence is that every ``HCC_``
variable the Compose file sets, or the entrypoint reads, has to be either a
field on :class:`~hcc.config.Settings` or an entry in
:data:`~hcc.config.KNOWN_NON_SETTING_VARIABLES`.

Getting that wrong produces the worst available failure: `docker compose up
-d` exits non-zero with "Unrecognised HCC_ environment variables", on a
deployment file the operator did not write and cannot be expected to debug.
It nearly happened twice while these files were being written —
``HCC_AUTO_MIGRATE`` and the four database parts the entrypoint assembles a
URL from are all read outside the settings model.

So the agreement is checked mechanically. The files are parsed, every
``HCC_`` name in them is extracted, and each one has to be accounted for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hcc.config import KNOWN_NON_SETTING_VARIABLES, Settings

#: Three directories up from this file: tests/ → server/ → apps/ → root.
REPOSITORY = Path(__file__).resolve().parents[3]

DEPLOY = REPOSITORY / "deploy" / "production"
COMPOSE = DEPLOY / "compose.yaml"
COMPOSE_BUILD = DEPLOY / "compose.build.yaml"
COMPOSE_TLS = DEPLOY / "compose.tls.yaml"
ENV_EXAMPLE = DEPLOY / ".env.example"
CADDYFILE = DEPLOY / "Caddyfile"
ENTRYPOINT = REPOSITORY / "deploy" / "docker-entrypoint.sh"
DOCKERFILE = REPOSITORY / "Dockerfile"

#: Every ``HCC_``-prefixed token, wherever it appears.
HCC_VARIABLE = re.compile(r"\bHCC_[A-Z0-9_]+\b")

#: Names in the deployment files that are **not** application settings and
#: are not read by the entrypoint either: they are Compose's own, substituted
#: into the file before any container starts.
#:
#: Listed explicitly rather than pattern-matched, because the whole point of
#: this test is that an unaccounted-for name is a failure.
COMPOSE_ONLY_VARIABLES = frozenset(
    {
        # Which host port the controller is published on.
        "HCC_BIND_PORT",
        # Caddy's, in the TLS overlay.
        "HCC_SITE_ADDRESS",
        "HCC_TLS_DIRECTIVE",
        "HCC_HTTP_PORT",
        "HCC_HTTPS_PORT",
        # Image labels, in the build overlay.
        "HCC_VERSION",
        "HCC_COMMIT",
        "HCC_BUILD_DATE",
    }
)


def settings_variables() -> frozenset[str]:
    """Every environment variable :class:`Settings` recognises."""
    return frozenset(f"HCC_{name.upper()}" for name in Settings.model_fields)


def accounted_for() -> frozenset[str]:
    return settings_variables() | KNOWN_NON_SETTING_VARIABLES | COMPOSE_ONLY_VARIABLES


def variables_in(path: Path) -> frozenset[str]:
    """Every ``HCC_`` name in the file, **including** those in comments.

    Comments are scanned deliberately. A commented-out variable that
    configures nothing is worse than an absent one — an operator will
    uncomment it, restart, and believe it took effect — and this is not
    hypothetical: the first draft of ``compose.tls.yaml`` documented
    ``HCC_TLS_INTERNAL`` and ``HCC_TLS_CERT``, neither of which the
    implementation ever read.
    """
    return frozenset(HCC_VARIABLE.findall(path.read_text(encoding="utf-8")))


def without_comments(path: Path) -> str:
    """The file's effective configuration, with comment lines removed.

    Needed by the assertions below *because* these files explain themselves:
    ``compose.yaml`` says "No `ports:`" in a comment, and
    ``compose.tls.yaml`` explains why it does not use ``!override``. A
    substring check over the raw text would match the explanation and report
    the opposite of the truth.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


class TestTheFilesExist:
    @pytest.mark.parametrize(
        "path",
        [COMPOSE, COMPOSE_BUILD, COMPOSE_TLS, ENV_EXAMPLE, CADDYFILE, ENTRYPOINT, DOCKERFILE],
        ids=lambda path: path.name,
    )
    def test_the_deployment_file_is_present(self, path: Path) -> None:
        # The documentation names each of these. A guide referring to a file
        # that is not in the repository is worse than no guide.
        assert path.is_file(), f"{path} is missing"


class TestEverySettingIsAccountedFor:
    @pytest.mark.parametrize(
        "path",
        [COMPOSE, COMPOSE_BUILD, COMPOSE_TLS, ENTRYPOINT, DOCKERFILE],
        ids=lambda path: path.name,
    )
    def test_no_file_names_a_variable_the_application_would_refuse(self, path: Path) -> None:
        unexpected = sorted(variables_in(path) - accounted_for())

        assert not unexpected, (
            f"{path.name} names {unexpected}, which the application does not "
            "recognise — startup would fail with 'Unrecognised HCC_ environment "
            "variables'. Add the field to Settings, or add the name to "
            "KNOWN_NON_SETTING_VARIABLES (if the entrypoint reads it) or to "
            "COMPOSE_ONLY_VARIABLES in this test (if Compose substitutes it)."
        )

    def test_the_env_example_names_only_real_settings(self) -> None:
        # The file an operator edits. A commented-out variable that does
        # nothing is worse than an absent one: they will set it, restart, and
        # believe it took effect.
        unexpected = sorted(variables_in(ENV_EXAMPLE) - accounted_for())

        assert not unexpected, f".env.example names {unexpected}, which configure nothing"

    def test_every_entrypoint_variable_is_exempted_rather_than_a_setting(self) -> None:
        # The specific trap this suite exists for. The entrypoint runs *before*
        # the application and reads variables the settings model never sees, so
        # each has to be in the exemption list — and adding one to the script
        # without adding it there makes the container exit on startup.
        read_by_entrypoint = variables_in(ENTRYPOINT)

        for name in sorted(read_by_entrypoint):
            assert name in accounted_for(), f"the entrypoint reads {name} and nothing declares it"

        # HCC_AUTO_MIGRATE is the one that matters and the only one left. The
        # database parts used to be exempted too, assembled into a URL by the
        # entrypoint, until `docker compose exec hcc hcc bootstrap-admin`
        # turned out not to run an entrypoint and so saw no URL at all. They
        # are real settings now, and this asserts the exemption list has not
        # regrown.
        assert "HCC_AUTO_MIGRATE" in KNOWN_NON_SETTING_VARIABLES
        assert "HCC_DATABASE_PASSWORD_FILE" in settings_variables(), (
            "the database password file is read by the application, not the "
            "entrypoint, and must be a real setting — otherwise `compose exec "
            "hcc hcc bootstrap-admin` cannot reach the database"
        )


class TestTheEntrypointIsSelfConsistent:
    """Every function the entrypoint calls is one it defines.

    `sh -n` parses and does not resolve calls, so a helper removed by an edit
    is a runtime failure on container start — which is exactly what happened:
    moving the database-URL assembly out of this script took `log` and `fail`
    with it, and the container's first line became
    `/opt/hcc/entrypoint.sh: 59: log: not found`.
    """

    def test_every_called_helper_is_defined(self) -> None:
        # Lines that are not comments, so the prose does not count as a call.
        body = without_comments(ENTRYPOINT)

        defined = set(re.findall(r"^(\w+)\(\) \{", body, re.MULTILINE))
        # Called at the start of a line or after `! `, `then`, `else`, `||`,
        # `&&` — which covers every call shape in this script without trying
        # to be a shell parser.
        called = set(re.findall(r"(?:^|\|\||&&|!|;|then|else)\s+(\w+)\b", body, re.MULTILINE))

        # Only the ones this script is supposed to define.
        helpers = {"log", "fail", "migrate"}
        for name in sorted(helpers & called):
            assert name in defined, (
                f"the entrypoint calls {name} and does not define it; the "
                "container would fail on start with "
                f"'{name}: not found'"
            )
        # And it defines all three, so a future edit that drops one is caught
        # by the loop above rather than by a container that will not start.
        assert helpers <= defined, f"the entrypoint no longer defines {sorted(helpers - defined)}"

    def test_the_assembly_is_no_longer_here(self) -> None:
        # It moved into hcc/config.py because `docker compose exec` does not
        # run an entrypoint. Asserted so that a future edit does not put it
        # back and silently reintroduce the broken first-run step.
        body = without_comments(ENTRYPOINT)

        assert "HCC_DATABASE_PASSWORD_FILE" not in body
        assert "postgresql://" not in body


class TestTheDeploymentIsSafeByDefault:
    def test_postgres_publishes_no_port(self) -> None:
        # A published PostgreSQL is the most common way a self-hosted
        # deployment ends up reachable from the internet. Asserted on the text
        # rather than the parsed document so that a `ports:` added anywhere in
        # that service's block fails here.
        text = without_comments(COMPOSE)
        postgres_block = text.split("\n  postgres:", 1)[1].split("\n  hcc:", 1)[0]

        assert "ports:" not in postgres_block, (
            "the database publishes a port; nothing outside the stack needs to reach it"
        )
        assert "5432:5432" not in text

    def test_the_controller_publishes_only_on_loopback(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")

        # `- "0.0.0.0:8000:8000"` or `- "8000:8000"` would put the control
        # plane on the LAN over plain HTTP, with session cookies on it.
        assert '"127.0.0.1:${HCC_BIND_PORT:-8000}:8000"' in text
        assert '"0.0.0.0:' not in text

    def test_the_database_password_is_generated_and_never_in_the_environment(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")

        # The file, not the value, for both services. A container's configured
        # environment shows up in `docker inspect`, which people paste into
        # issue reports.
        assert "POSTGRES_PASSWORD_FILE: /run/secrets/db_password" in compose
        assert "POSTGRES_PASSWORD:" not in compose
        assert "HCC_DATABASE_PASSWORD_FILE: /run/secrets/db_password" in compose
        # And both consumers mount the directory read-only: neither PostgreSQL
        # nor the control plane has any business changing the password.
        #
        # `z` is not decoration. Without the SELinux relabel the deployment
        # fails on Fedora, RHEL, CentOS Stream, Rocky and Alma with
        # "Permission denied" on a file that is plainly mode 0644 — found by
        # running it on an enforcing host. Docker and Podman both ignore the
        # flag where SELinux is not enforcing.
        assert compose.count("./secrets:/run/secrets:ro,z") == 2
        # And no password is written into the file an operator edits.
        assert "PASSWORD=" not in ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_the_generated_password_is_url_safe(self) -> None:
        # It goes into a postgresql:// URL. A '/' or '@' in it would silently
        # produce a URL pointing somewhere else, and the error that follows
        # would be about DNS.
        compose = COMPOSE.read_text(encoding="utf-8")

        assert "tr '+/' '-_'" in compose, "the generator does not produce a URL-safe password"
        # And the *application* refuses one that is not, rather than trusting
        # the generator stayed correct. The check used to be in the
        # entrypoint; it moved into hcc/config.py along with the assembly,
        # because `docker compose exec` does not run an entrypoint.
        #
        # Asserted on the constant's name rather than on the message, which a
        # formatter is free to rewrap across lines.
        assert "_URL_SAFE_PASSWORD" in (
            REPOSITORY / "apps" / "server" / "hcc" / "config.py"
        ).read_text(encoding="utf-8")

    def test_the_container_runs_unprivileged_with_a_read_only_root(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        assert "read_only: true" in compose
        assert "no-new-privileges:true" in compose
        assert "cap_drop:" in compose
        # The image drops to the service account itself, so the hardening does
        # not depend on the Compose file being the one that runs it.
        assert "USER hcc:hcc" in dockerfile

    def test_the_image_builds_the_client_in_a_stage_it_does_not_ship(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        # Node builds the client and the final stage carries neither it nor
        # node_modules. A single-stage image would ship a JavaScript toolchain
        # and a compiler to every deployment.
        assert "AS web" in dockerfile
        assert "AS runtime" in dockerfile
        assert "COPY --from=web /build/dist /opt/hcc/web" in dockerfile

    def test_the_health_check_reports_the_database_too(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        # /healthz answers "this process is alive", which a process that
        # cannot reach its database also does. /readyz reports the database,
        # which is what "ready to serve" means here.
        assert "/readyz" in dockerfile
        assert "HEALTHCHECK" in dockerfile

    def test_there_is_no_worker_count_knob(self) -> None:
        # Login throttling counters live in one process's memory (ADR-029,
        # ADR-046), so a second worker gets its own and multiplies the
        # effective limit. A performance setting must not be able to weaken a
        # security control; when per-process limiting is replaced, the knob
        # arrives with it.
        for path in (COMPOSE, ENV_EXAMPLE, ENTRYPOINT, DOCKERFILE):
            text = without_comments(path).lower()
            assert "workers=" not in text, f"{path.name} exposes a worker count"
            assert "--workers" not in text, f"{path.name} exposes a worker count"


class TestTheProxyVariantIsCoherent:
    def test_caddy_proxies_websockets_without_a_read_timeout(self) -> None:
        # The agent connection *is* a WebSocket and is meant to stay open for
        # hours. A proxy read timeout would disconnect every agent on a fixed
        # cycle, they would all reconnect, and the fleet would look like it
        # was flapping — with nothing in the controller's log to explain it.
        caddyfile = CADDYFILE.read_text(encoding="utf-8")

        assert "read_timeout 0" in caddyfile
        assert "write_timeout 0" in caddyfile
        assert "reverse_proxy hcc:8000" in caddyfile

    def test_the_trusted_proxy_default_is_not_loopback(self) -> None:
        # On single-host Compose the proxy is a sibling *container*, so its
        # address is on the bridge network. Trusting 127.0.0.1 would trust
        # nothing, every client would collapse onto the proxy's single
        # rate-limit budget, and one attacker could deny login to everyone.
        compose = COMPOSE.read_text(encoding="utf-8")
        trusted = next(line for line in compose.splitlines() if "HCC_TRUSTED_PROXIES" in line)

        assert "127.0.0.1" not in trusted
        assert "172.16.0.0/12" in trusted

    def test_the_tls_overlay_does_not_use_a_compose_only_tag(self) -> None:
        # `!override` is Docker Compose's and podman-compose does not
        # understand it, which would make this overlay silently unusable for
        # half the people who try it.
        effective = without_comments(COMPOSE_TLS)

        assert "!override" not in effective
        assert "!reset" not in effective


class TestTheOneRequiredSetting:
    def test_the_public_origin_is_required_with_a_message(self) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")

        # `:?` makes Compose refuse to start with the message rather than
        # substituting an empty string — which the application would then
        # reject at startup with a less useful error, after pulling images.
        assert "HCC_PUBLIC_ORIGIN: ${HCC_PUBLIC_ORIGIN:?" in compose

    def test_the_env_example_puts_it_first_and_uncommented(self) -> None:
        lines = [
            line
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]

        # The only uncommented line in the file. Everything else has a working
        # default, and a file where the required field is buried among
        # optional ones is a file where it gets missed.
        assert lines, ".env.example has no uncommented settings"
        assert lines[0].startswith("HCC_PUBLIC_ORIGIN=")
        assert len(lines) == 1, f"more than one uncommented setting: {lines}"
