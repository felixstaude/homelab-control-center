# Homelab Control Center — the control plane, with the web client built in.
#
# One image, one port, one origin. The API is under /api, the operational
# probes are at the root, and everything else is the client — which is the
# arrangement the whole security model is written for: session cookies are
# SameSite=Lax, the CSRF Origin check has something to compare against, and
# CORS needs no entries at all. See apps/server/hcc/web.py.
#
# Build:
#   docker build -t hcc:local .
#
# Run: see deploy/production/compose.yaml, which is what this image is for.
# It expects a PostgreSQL 17 it can reach and exactly one setting it cannot
# guess — the origin browsers use.
#
# ## What this image is not
#
# Not multi-arch. The agent is cross-compiled for three architectures because
# it runs on whatever a homelab owns; the controller runs on the one machine
# an operator chooses, and a Python image for arm64 needs either QEMU or an
# arm64 runner. Stated in the deployment documentation rather than implied by
# a manifest list that was never built.

# ===========================================================================
# 1. The web client
# ===========================================================================
# Node is here and only here. The final stage carries no JavaScript toolchain,
# no node_modules, and no source — just the hashed bundles Vite emits.
FROM docker.io/library/node:22-alpine AS web

WORKDIR /build

# The lockfile first, so a source-only change does not reinstall every
# dependency. `npm ci` installs exactly the lockfile and fails if
# package.json has drifted from it.
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY apps/web/ ./
# The production build. Type errors are caught by `make check` and by CI; this
# stage is `vite build` alone, because a container build is the wrong place to
# discover a type error and a slow place to look for one.
RUN npm run build -- --mode production


# ===========================================================================
# 2. The Python dependencies
# ===========================================================================
# A separate stage so the final image gets the installed packages and not the
# compilers some of them need to build. asyncpg and argon2-cffi both compile
# C extensions.
FROM docker.io/library/python:3.12-slim-bookworm AS python-build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Into a virtualenv rather than the system Python, so the final stage copies
# one self-contained directory and nothing has to be reinstalled there.
RUN python -m venv /opt/hcc/venv
ENV PATH="/opt/hcc/venv/bin:$PATH"

# The dependency declaration first, for the layer cache. `pip install .`
# without the `[dev]` extra: pytest, mypy and ruff have no business in a
# production image, and their absence is also what keeps it small.
COPY apps/server/pyproject.toml apps/server/README.md ./
COPY apps/server/hcc/__init__.py ./hcc/__init__.py
RUN pip install --upgrade pip && pip install .

# Then the application, installed over the top. Editable would be smaller to
# rebuild and would leave the image depending on a source tree that the final
# stage does not copy.
COPY apps/server/ ./
RUN pip install --no-deps .


# ===========================================================================
# 3. The image that runs
# ===========================================================================
FROM docker.io/library/python:3.12-slim-bookworm AS runtime

# Build metadata, passed in by the build so the image can say what it is.
# Not baked as defaults that would be wrong: "unknown" is honest for a local
# build and a label claiming 0.1.0 for an arbitrary tree is not.
ARG HCC_VERSION=unknown
ARG HCC_COMMIT=unknown
ARG HCC_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Homelab Control Center" \
      org.opencontainers.image.description="Self-hosted monitoring for Linux hosts" \
      org.opencontainers.image.source="https://github.com/felixstaude/homelab-control-center" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${HCC_VERSION}" \
      org.opencontainers.image.revision="${HCC_COMMIT}" \
      org.opencontainers.image.created="${HCC_BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/hcc/venv/bin:$PATH" \
    # Where the client lives, so the application serves it without the
    # operator having to know the path.
    HCC_WEB_ROOT=/opt/hcc/web \
    # Inside a container the port is deliberately published, so binding
    # loopback would make the service unreachable. The port itself is not
    # published by the Compose file without a proxy in front.
    HCC_HOST=0.0.0.0 \
    HCC_PORT=8000 \
    HCC_ENVIRONMENT=production \
    HCC_LOG_FORMAT=json

# curl for the health check and nothing else. The runtime needs no compiler,
# no node, and no package manager; `--no-install-recommends` and the cleanup
# are what keep that true.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 # A dedicated unprivileged account with no login shell and no home
 # directory. uid 10001 rather than a low one: a high, fixed uid cannot
 # collide with a host account when a volume is shared, and a *fixed* one is
 # what makes a bind-mounted volume's ownership predictable.
 && groupadd --system --gid 10001 hcc \
 && useradd --system --uid 10001 --gid hcc --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin hcc

COPY --from=python-build /opt/hcc/venv /opt/hcc/venv
COPY --from=web /build/dist /opt/hcc/web
COPY --chmod=0755 deploy/docker-entrypoint.sh /opt/hcc/entrypoint.sh

# The migrations, copied rather than packaged.
#
# `[tool.hatch.build.targets.wheel] packages = ["hcc"]` ships the application
# and not `alembic/`, which is correct — the migration scripts are a
# deployment artefact rather than library code, and packaging them would put
# them on the import path of anything that depended on this. So they are
# copied beside the venv and the entrypoint runs alembic from here, where
# `script_location = alembic` in alembic.ini resolves.
COPY apps/server/alembic.ini /opt/hcc/alembic.ini
COPY apps/server/alembic /opt/hcc/alembic

# Everything the application reads is root-owned and read-only to the service
# account. It writes nothing to the filesystem: all state is in PostgreSQL,
# which is an existing architectural property rather than a convenience, and
# is what lets the Compose file run this with a read-only root filesystem.
RUN chown -R root:root /opt/hcc && chmod -R a-w /opt/hcc

USER hcc:hcc
WORKDIR /opt/hcc
EXPOSE 8000

# /readyz, not /healthz. /healthz answers "this process is alive", which a
# process that cannot reach its database also does; /readyz reports the
# database too, which is what "ready to serve" means here.
#
# start-period covers the migrations the entrypoint runs before binding.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS --max-time 4 http://127.0.0.1:8000/readyz || exit 1

ENTRYPOINT ["/opt/hcc/entrypoint.sh"]
CMD ["serve"]
