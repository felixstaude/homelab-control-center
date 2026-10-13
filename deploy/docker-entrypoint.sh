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
# ## exec, not a subshell
#
# `exec` replaces this shell with the server, so uvicorn becomes pid 1 and
# receives SIGTERM directly. Without it, the shell is pid 1, does not forward
# signals, and `docker compose down` waits ten seconds and then kills the
# process mid-request — losing the graceful shutdown the application
# implements.

set -eu

# ---------------------------------------------------------------------------
# The database URL, assembled from a password file
# ---------------------------------------------------------------------------
#
# HCC_DATABASE_URL wins when it is set: an operator pointing at an existing
# PostgreSQL gives the whole URL and none of this runs.
#
# Otherwise the URL is assembled here from HCC_DATABASE_PASSWORD_FILE and four
# defaults. The point is that the password is never in the container's
# *configured* environment — `docker inspect` shows that, and people paste
# `docker inspect` into issue reports. A file does not appear there, and the
# value this script exports lands only in the server process's own
# environment.
#
# Every variable read here is listed in KNOWN_NON_SETTING_VARIABLES in
# hcc/config.py. It has to be: the application refuses to start on an
# unrecognised HCC_ variable, deliberately, because a typo'd setting that was
# silently ignored would leave an operator believing they had configured
# something. tests/test_deployment.py checks the two lists agree.
# Returns non-zero rather than exiting, so the caller decides whether a
# missing database URL is fatal. It is for `serve` and `migrate`; it is not for
# `hcc --help`, and an entrypoint that refused to print a help message because
# no database was configured would be an entrypoint people work around.
try_assemble_database_url() {
  if [ -n "${HCC_DATABASE_URL:-}" ]; then
    log database_url_given "HCC_DATABASE_URL is set; not assembling one"
    return 0
  fi

  password_file="${HCC_DATABASE_PASSWORD_FILE:-}"
  if [ -z "$password_file" ]; then
    reason="set HCC_DATABASE_URL, or HCC_DATABASE_PASSWORD_FILE and let this assemble one"
    return 1
  fi
  if [ ! -r "$password_file" ]; then
    reason="cannot read the password file $password_file"
    return 1
  fi

  password="$(cat "$password_file")"
  if [ -z "$password" ]; then
    reason="the password file $password_file is empty"
    return 1
  fi

  # The password is generated from a URL-safe alphabet by the hcc-init
  # service, so it needs no escaping here. Checked rather than assumed,
  # because a password containing '/' or '@' would silently produce a URL
  # pointing at the wrong host — and the error that follows would be about
  # DNS.
  case "$password" in
    *[!A-Za-z0-9_-]*)
      fail database_url_unsafe \
        "the password in $password_file contains characters that are not URL-safe; \
regenerate it, or set HCC_DATABASE_URL yourself with the password percent-encoded"
      ;;
  esac

  HCC_DATABASE_URL="postgresql://${HCC_DATABASE_USER:-hcc}:${password}@${HCC_DATABASE_HOST:-postgres}:${HCC_DATABASE_PORT:-5432}/${HCC_DATABASE_NAME:-hcc}"
  export HCC_DATABASE_URL
  # The *shape*, never the value. A log line carrying a database password is
  # a log line in somebody's aggregator forever.
  log database_url_assembled \
    "postgresql://${HCC_DATABASE_USER:-hcc}:<from ${password_file}>@${HCC_DATABASE_HOST:-postgres}:${HCC_DATABASE_PORT:-5432}/${HCC_DATABASE_NAME:-hcc}"
}

# The same thing, where not having one is fatal.
assemble_database_url() {
  reason=""
  if ! try_assemble_database_url; then
    fail database_url_missing "$reason"
  fi
}

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
    assemble_database_url
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
    assemble_database_url
    migrate
    log migrations_complete "not serving; this invocation was 'migrate'"
    ;;
  *)
    # `docker compose exec hcc hcc bootstrap-admin` is the documented
    # first-run step and ADR-033 makes a local command the only way to create
    # the first administrator. Inside a container, `exec` *is* the local
    # command — so anything else passed here is run as given.
    #
    # The URL is assembled first, because `hcc bootstrap-admin` needs the
    # database as much as the server does and an operator running it should
    # not have to know how the password reaches the process.
    #
    # Best effort, though: `hcc --help` needs no database, and an entrypoint
    # that refused to print a help message because none was configured is an
    # entrypoint people work around. A command that does need one fails on its
    # own terms, with its own message.
    reason=""
    if ! try_assemble_database_url; then
      log database_url_unavailable "$reason"
    fi
    exec "$@"
    ;;
esac
