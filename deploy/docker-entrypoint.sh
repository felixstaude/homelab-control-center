#!/bin/sh
#
# The control plane's entrypoint.
#
# Two jobs: run the migrations, then exec the server. Nothing else — an
# entrypoint that does anything clever is an entrypoint nobody can debug from
# a `docker logs`.
#
# Usage (the image's own CMD):
#   serve             migrate, then run the server          (the default)
#   migrate           migrate and exit, for a watched upgrade
#   <anything else>   exec'd as given, so `docker compose exec hcc hcc
#                     bootstrap-admin` and `… sh` both work
#
# ## Migrations run before the port is bound
#
# `alembic upgrade head` completes before uvicorn starts, so a container that
# cannot migrate exits non-zero and never serves traffic. The alternative —
# migrating in a background task after binding — produces a deployment that
# answers requests against a schema it is halfway through changing.
#
# This is safe *here* specifically because one controller process is the rule
# (ADR-046): there is no second replica racing the same migration. A
# deployment that ever becomes multi-replica has to move this to a one-shot
# service, and that is the point at which the ADR gets superseded rather than
# quietly violated.
#
# HCC_AUTO_MIGRATE=false skips it, for an operator who would rather run
# `docker compose run --rm hcc migrate` and watch it. The default does not
# require that, because a deployment whose first `up -d` leaves an empty
# database and a confusing error is not an installation.
#
# ## The database URL is not this script's business
#
# It used to be: this script read HCC_DATABASE_PASSWORD_FILE, assembled a
# postgresql:// URL and exported it. That worked for the server and broke the
# documented first-run step — `docker compose exec hcc hcc bootstrap-admin`
# does not run an entrypoint, so it saw no URL and failed with
# "HCC_DATABASE_URL: Field required" on a perfectly good deployment. The
# assembly is in hcc/config.py now, where the server, `hcc` anything and
# alembic all get it.
#
# ## exec, not a subshell
#
# `exec` replaces this shell with the server, so uvicorn becomes pid 1 and
# receives SIGTERM directly. Without it, the shell is pid 1, does not forward
# signals, and `docker compose down` waits ten seconds and then kills the
# process mid-request — losing the graceful shutdown the application
# implements.

set -eu

log() {
  # Matched to the application's own JSON log format, so `docker logs` is one
  # parseable stream rather than two.
  printf '{"event":"%s","level":"info","logger":"entrypoint","detail":"%s"}\n' "$1" "${2:-}"
}

fail() {
  printf '{"event":"%s","level":"error","logger":"entrypoint","detail":"%s"}\n' "$1" "${2:-}" >&2
  exit 1
}

migrate() {
  if [ "${HCC_AUTO_MIGRATE:-true}" = "false" ]; then
    log migrations_skipped "HCC_AUTO_MIGRATE=false; run 'migrate' yourself before serving"
    return 0
  fi

  log migrations_starting
  # From /opt/hcc, where the image put alembic.ini and alembic/. Alembic
  # resolves `script_location = alembic` relative to the working directory, so
  # the cd is not cosmetic.
  cd /opt/hcc || fail migrations_failed "cannot enter /opt/hcc"
  if ! alembic upgrade head; then
    fail migrations_failed "the database schema could not be brought up to date"
  fi
  log migrations_applied
}

case "${1:-serve}" in
  serve)
    migrate
    log serving "binding ${HCC_HOST:-0.0.0.0}:${HCC_PORT:-8000}"
    # One worker, deliberately, and there is no knob for more.
    #
    # Login throttling counters live in this process's memory (ADR-029,
    # ADR-046), so a second worker would get its own and multiply the
    # effective limit — a security control quietly weakened by a performance
    # setting. When per-process limiting is replaced, the knob arrives with
    # it.
    exec python -m hcc.main
    ;;
  migrate)
    migrate
    log migrations_complete "not serving; this invocation was 'migrate'"
    ;;
  *)
    # `docker compose exec hcc hcc bootstrap-admin` is the documented
    # first-run step and ADR-033 makes a local command the only way to create
    # the first administrator. Inside a container, `exec` *is* the local
    # command — so anything else passed here is run as given.
    #
    # Nothing is prepared for it. The database URL used to be assembled here
    # and exported before exec-ing the server, and that broke precisely this
    # path: `docker compose exec` does not run an entrypoint, so
    # `hcc bootstrap-admin` saw no URL and failed with "HCC_DATABASE_URL:
    # Field required" on a working deployment. The assembly now lives in
    # hcc/config.py, where every process in the container gets it.
    exec "$@"
    ;;
esac
