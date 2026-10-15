#!/usr/bin/env bash
#
# A real version-to-version update, driven by the controller, on real systemd.
#
# Two genuinely different agent builds, a signed release manifest, a real
# controller behind real TLS, and an agent in a disposable container that
# downloads, verifies, installs and restarts itself — then an update that
# fails and leaves the host running.
#
# # Why this exists and what install-matrix.sh does not cover
#
# `install-matrix.sh` already tests `hcc-agent upgrade --from <archive>`: the
# operator-driven path, where a person runs a command and the process doing
# the upgrade is not the service. That path is the easy one. Everything
# interesting about automatic updates is in the half it cannot reach:
#
#   * The **instruction** path. The controller authorizes a job, the agent
#     asks for it on its next heartbeat, and a frame with signed metadata
#     comes back. Nothing here is a command: the frame is data, and the agent
#     decides.
#   * The **signature**. The agent verifies a publisher Ed25519 signature over
#     the manifest's exact bytes with a key stamped into its own binary. A
#     controller that has been taken over cannot forge it, and the only way to
#     know that is true is to run it.
#   * The **self-replacement**. The upgrade runs *inside the service process*,
#     so `systemctl restart` kills the thing performing it. No process can
#     report its own successful replacement, so the controller concludes the
#     job when the replacement connects. That interaction has three moving
#     parts and no unit test can observe it.
#   * The **host's own veto**. Automatic updates are off until this host says
#     otherwise, in a file the controller cannot write.
#
# # What is asserted
#
#  1. An agent with updates disabled declines an instruction and says why.
#  2. An agent with updates enabled installs 0.1.1-test over 0.1.0-test.
#  3. The job's event log holds the states the agent actually reported.
#  4. The controller concludes the job when the new binary connects.
#  5. An update whose bytes do not match the signed digest fails, and the host
#     keeps running the version it had.
#  6. A manifest signed by a key the agent does not trust is refused.
#  7. The host's version in the interface is the host's own report.
#
# Usage:
#   make db-up
#   ./scripts/update-e2e.sh
#   ./scripts/update-e2e.sh --image debian:13 --keep
#
# Safety
# ------
# Nothing here touches the developer's installation or any remote host:
#
#   * The database is created for this run and dropped on every exit path.
#   * The controller listens on loopback only, behind a TLS terminator bound
#     to one address: the host's loopback under rootless Podman, or the
#     container bridge's gateway under Docker. Never 0.0.0.0.
#   * The release signing key is generated for this run, into a directory
#     removed on every exit path. It signs nothing that leaves this machine.
#   * The agents built here are stamped with that key and with -test versions.
#     They are **not** publishable artifacts and nothing publishes them.
#   * The container is disposable, named for this run, and removed on every
#     exit path unless --keep is given.
#   * Automatic updates are enabled **inside the container only**, by writing
#     the container's own environment file. No real host is touched.
#
# The container is privileged, because systemd as pid 1 needs it. That is why
# this is a separate script rather than part of `make check`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
VENV="$SERVER_DIR/.venv"
# Absolute, because the artifact mirror runs with its working directory inside
# the temporary tree and a relative interpreter path does not survive that.
VENV_PYTHON="$REPO_ROOT/$VENV/bin/python"

IMAGE="debian:13"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,70p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

RUN_ID="$(date +%s)-$$"
SLUG="$(printf '%s' "$IMAGE" | tr -c 'a-zA-Z0-9' '-' | tr -s '-' | sed 's/^-//;s/-$//')"
CONTAINER="hcc-update-$SLUG-$RUN_ID"
TEST_IMAGE="hcc-update-test-$SLUG:$RUN_ID"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
TEST_DB="hcc_upd_$(date +%s)_$$"
TEST_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$TEST_DB"

# Not 8000, 8099, 8201, 8211 or 8221: a dev server, the smoke test, the
# browser suite, the monitoring scenario and the installation matrix must not
# be mistaken for this one.
PORT=8241
TLS_PORT=8242
MIRROR_TLS_PORT=8243
MIRROR_PORT=8244
BASE="http://127.0.0.1:$PORT"
API="$BASE/api/v1"
# A .test name is reserved by RFC 6761 and will never resolve publicly, so a
# leak of this certificate is worth nothing.
CONTROLLER_NAME="hcc.test"

# The three builds. Pre-release identifiers on purpose: these are not
# publishable versions, and `0.1.0-test` sorts below every real 0.1.0 so one
# cannot be mistaken for a release even if it escaped.
#
# `0.1.0-test → 0.1.1-test` is a *patch* step — the step kind is measured on
# the core triple — which is what makes it something an automatic patch policy
# would take.
VERSION_OLD="0.1.0-test"
VERSION_NEW="0.1.1-test"
VERSION_BROKEN="0.1.2-test"

ADMIN_USER="update-e2e-admin"
ADMIN_PASSWORD="update-e2e-$(date +%s)-correct-horse-battery"

WORK=""
SERVER_PID=""
TLS_PID=""
MIRROR_TLS_PID=""
MIRROR_PID=""
CREATED=""
COOKIE_JAR=""
CSRF=""
HOST_ID=""
CONTAINER_STARTED=""
IMAGE_BUILT=""
TRUSTED_KEY=""
SCHEMA_REVISION=""

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
info()  { printf '\n\033[0;36m%s\033[0m\n' "$1"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$1"; }

ok()  { green "  ✓ $1"; PASS=$((PASS + 1)); }
bad() { red   "  ✗ $1"; FAIL=$((FAIL + 1)); }

die() {
  red "$1"
  dump_logs
  exit 1
}

dump_logs() {
  for log in /tmp/hcc-upd-server.log /tmp/hcc-upd-tls.log /tmp/hcc-upd-mirror.log; do
    if [ -s "$log" ]; then
      red "--- $log (tail) ---"
      tail -30 "$log" || true
    fi
  done
  if [ -n "$CONTAINER_STARTED" ]; then
    red "--- journalctl -u hcc-agent (in the container) ---"
    $RUNTIME exec "$CONTAINER" journalctl -u hcc-agent -n 60 --no-pager 2>&1 | tail -60 || true
  fi
  if [ -n "$HOST_ID" ]; then
    red "--- update jobs ---"
    query "SELECT state, from_version, to_version, attempt, coalesce(error, '') FROM update_jobs
           WHERE host_id = '$HOST_ID' ORDER BY created_at" 2>/dev/null || true
    red "--- update events ---"
    query "SELECT e.state, e.source, coalesce(e.detail, '') FROM update_events e
           JOIN update_jobs j ON j.id = e.job_id
           WHERE j.host_id = '$HOST_ID' ORDER BY e.id" 2>/dev/null || true
  fi
}

# --- Container runtime ------------------------------------------------------
RUNTIME=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  RUNTIME="docker"
elif command -v podman >/dev/null 2>&1; then
  RUNTIME="podman"
else
  die "no container runtime: install Docker or Podman"
fi

cleanup() {
  status=$?
  if [ -n "$CONTAINER_STARTED" ] && [ "$KEEP" = 0 ]; then
    $RUNTIME rm -f "$CONTAINER" >/dev/null 2>&1 || true
  elif [ -n "$CONTAINER_STARTED" ]; then
    dim "kept container $CONTAINER (--keep)"
  fi
  if [ -n "$IMAGE_BUILT" ] && [ "$KEEP" = 0 ]; then
    $RUNTIME rmi -f "$TEST_IMAGE" >/dev/null 2>&1 || true
  fi
  stop_tls
  stop_mirror
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  [ -n "$COOKIE_JAR" ] && rm -f "$COOKIE_JAR"
  # The signing key lives in here. Removed on every exit path, including the
  # failure ones — a private key surviving a failed test run is how a test
  # key ends up trusted by something.
  [ -n "$WORK" ] && rm -rf "$WORK"
  if [ -n "$CREATED" ]; then
    if PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super \
      "DROP DATABASE IF EXISTS \"$TEST_DB\" WITH (FORCE)" 2>/dev/null; then
      green "✓ dropped $TEST_DB"
    else
      red "✗ could not drop $TEST_DB — remove it by hand"
    fi
  fi
  exit "$status"
}

psql_super() {
  PG_SUPERUSER_URL="$PG_SUPERUSER_URL" "$VENV/bin/python" - "$1" <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["PG_SUPERUSER_URL"])
    try:
        await connection.execute(sys.argv[1])
    finally:
        await connection.close()


asyncio.run(main())
PY
}

query() {
  TEST_DB_URL="$TEST_DB_URL" "$VENV/bin/python" - "$1" <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["TEST_DB_URL"])
    try:
        rows = await connection.fetch(sys.argv[1])
        for row in rows:
            print("\t".join("" if value is None else str(value) for value in row.values()))
    finally:
        await connection.close()


asyncio.run(main())
PY
}

json_field() {
  "$VENV/bin/python" -c "
import json, sys
document = json.load(sys.stdin)
for key in sys.argv[1].split('.'):
    document = document[int(key)] if isinstance(document, list) else document[key]
print(document if document is not None else '')
" "$1"
}

api() {
  method="$1"; path="$2"; body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$API$path" -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" -d "$body"
  else
    curl -sS -X "$method" "$API$path" -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      -H "X-CSRF-Token: $CSRF"
  fi
}

api_get() { curl -sS "$API$1" -b "$COOKIE_JAR" -c "$COOKIE_JAR"; }

in_container() { $RUNTIME exec "$CONTAINER" "$@"; }

await_query() {
  label="$1"; sql="$2"; expected="$3"; timeout="${4:-45}"; actual=""
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    actual="$(query "$sql" | head -1 || true)"
    if [ "$actual" = "$expected" ]; then
      ok "$label"
      return 0
    fi
    sleep 1
  done
  bad "$label — expected '$expected', last saw '$actual' after ${timeout}s"
  return 1
}

# Poll until a query's first column is one of several values, reporting which.
#
# Separate from await_query because an update has more than one terminal
# state and "it reached a terminal state, and it was the wrong one" is a far
# more useful failure than "it never reached the one I asked for" — which is
# what a single-value wait reports for a job that failed in two seconds.
await_terminal() {
  label="$1"; sql="$2"; timeout="${3:-180}"; actual=""
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    actual="$(query "$sql" | head -1 || true)"
    case "$actual" in
      succeeded|failed|rolled_back|cancelled|deferred)
        # To stderr, deliberately: stdout is the channel the caller reads the
        # state from, and a progress line on it would be captured as part of
        # the answer. This was a real bug for one revision — the comparison
        # then failed with the state and the message concatenated.
        dim "  $label reached '$actual' after $((timeout - (deadline - SECONDS)))s" >&2
        printf '%s' "$actual"
        return 0
        ;;
    esac
    sleep 1
  done
  red "  ✗ $label never reached a terminal state (last '$actual' after ${timeout}s)" >&2
  printf '%s' "${actual:-none}"
  return 1
}

stop_tls() {
  [ -n "$TLS_PID" ] || return 0
  kill "$TLS_PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$TLS_PID" 2>/dev/null || break
    sleep 0.25
  done
  kill -9 "$TLS_PID" 2>/dev/null || true
  wait "$TLS_PID" 2>/dev/null || true
  TLS_PID=""
}

start_tls() {
  "$VENV/bin/python" scripts/tls-terminator.py \
    --listen "$BIND_ADDRESS:$TLS_PORT" \
    --target "127.0.0.1:$PORT" \
    --certificate "$WORK/server-chain.pem" \
    --key "$WORK/server.key" >>/tmp/hcc-upd-tls.log 2>&1 &
  TLS_PID=$!
  sleep 1
  kill -0 "$TLS_PID" 2>/dev/null || { cat /tmp/hcc-upd-tls.log; die "the TLS terminator died"; }
}

stop_mirror() {
  for pid in "$MIRROR_TLS_PID" "$MIRROR_PID"; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 12); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  MIRROR_TLS_PID=""
  MIRROR_PID=""
}

# The artifact mirror: where the archives are actually downloaded from.
#
# Separate from the controller on purpose, and that separation is the point of
# the whole exercise. In production the archives live on a release host the
# controller does not control; here they live behind a different port with the
# same certificate. The agent is told where to fetch by *signed* metadata, so
# a compromised controller cannot redirect it — and the digest in that
# metadata is what makes the mirror untrusted.
start_mirror() {
  (cd "$WORK/mirror" && exec "$VENV_PYTHON" -m http.server "$MIRROR_PORT" \
    --bind 127.0.0.1) >>/tmp/hcc-upd-mirror.log 2>&1 &
  MIRROR_PID=$!
  "$VENV/bin/python" scripts/tls-terminator.py \
    --listen "$BIND_ADDRESS:$MIRROR_TLS_PORT" \
    --target "127.0.0.1:$MIRROR_PORT" \
    --certificate "$WORK/server-chain.pem" \
    --key "$WORK/server.key" >>/tmp/hcc-upd-mirror.log 2>&1 &
  MIRROR_TLS_PID=$!
  sleep 1
  kill -0 "$MIRROR_PID" 2>/dev/null || { cat /tmp/hcc-upd-mirror.log; die "the mirror died"; }
  kill -0 "$MIRROR_TLS_PID" 2>/dev/null || {
    cat /tmp/hcc-upd-mirror.log; die "the mirror's TLS terminator died"; }
}

wait_for_http() {
  for _ in $(seq 1 100); do
    if curl -sS -o /dev/null --max-time 2 "$1" 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  return 1
}

restart_controller() {
  # Used when the manifest on disk changes: it is read through an lru_cache
  # keyed on the path, so a replacement file is not picked up by a running
  # process. Restarting is what a real deployment does too — the manifest is
  # baked into the controller's image.
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  start_controller
  [ -n "$COOKIE_JAR" ] && rm -f "$COOKIE_JAR"
  COOKIE_JAR="$(mktemp /tmp/hcc-upd-cookies-XXXXXX)"
  login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
  CSRF="$(printf '%s' "$login" | json_field csrf_token)"
  [ -n "$CSRF" ] || die "re-login after the restart failed: $login"
}

start_controller() {
  (
    cd "$SERVER_DIR"
    exec env \
      HCC_DATABASE_URL="$TEST_DB_URL" \
      HCC_HOST=127.0.0.1 \
      HCC_PORT="$PORT" \
      HCC_ENVIRONMENT=development \
      HCC_LOG_FORMAT=json \
      HCC_METRICS_MAINTENANCE_INTERVAL_SECONDS=0 \
      HCC_AGENT_RELEASE_MANIFEST="$WORK/agent-releases.json" \
      HCC_RELEASE_SIGNING_KEYS="[\"$TRUSTED_KEY\"]" \
      .venv/bin/python -m hcc.main
  ) >>/tmp/hcc-upd-server.log 2>&1 &
  SERVER_PID=$!
  wait_for_http "$BASE/healthz" || die "the controller did not start"
}

# ===========================================================================
info "== 1. Preconditions =="

[ -x "$VENV/bin/python" ] || die "Python venv missing. Run: make install-server"
command -v go >/dev/null || die "Go missing. Two agents are built from source here."
command -v openssl >/dev/null || die "openssl missing. The internal CA is generated with it."
command -v curl >/dev/null || die "curl missing."
ok "toolchain present ($RUNTIME, $(go version | awk '{print $3}'))"

for port in "$PORT" "$TLS_PORT" "$MIRROR_TLS_PORT" "$MIRROR_PORT"; do
  if curl -sS -o /dev/null --max-time 1 "http://127.0.0.1:$port/" 2>/dev/null; then
    die "Something is already listening on port $port. Stop it first."
  fi
done
ok "ports $PORT, $TLS_PORT, $MIRROR_TLS_PORT and $MIRROR_PORT are free"

trap cleanup EXIT
WORK="$(mktemp -d /tmp/hcc-upd-XXXXXX)"
chmod 755 "$WORK"
mkdir -p "$WORK/mirror"

# ===========================================================================
info "== 2. An internal certificate authority =="

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 1 \
  -keyout "$WORK/ca.key" -out "$WORK/ca.pem" \
  -subj "/CN=Homelab Control Center update-e2e CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  >/dev/null 2>&1 || die "could not generate the CA"

openssl req -newkey rsa:2048 -nodes -sha256 \
  -keyout "$WORK/server.key" -out "$WORK/server.csr" \
  -subj "/CN=$CONTROLLER_NAME" >/dev/null 2>&1 || die "could not generate the server key"

cat >"$WORK/server.ext" <<EXT
subjectAltName = DNS:$CONTROLLER_NAME
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
EXT

openssl x509 -req -in "$WORK/server.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" \
  -CAcreateserial -days 1 -sha256 -extfile "$WORK/server.ext" \
  -out "$WORK/server.pem" >/dev/null 2>&1 || die "could not sign the server certificate"

cat "$WORK/server.pem" "$WORK/ca.pem" >"$WORK/server-chain.pem"
chmod 644 "$WORK/ca.pem" "$WORK/server-chain.pem"
chmod 600 "$WORK/ca.key" "$WORK/server.key"
ok "CA and a certificate for $CONTROLLER_NAME, valid for one day"

# ===========================================================================
info "== 3. A release signing keypair, for this run only =="

"$VENV/bin/python" scripts/sign-release.py keygen \
  --private-key "$WORK/release.key" --public-key "$WORK/release.pub" \
  >/tmp/hcc-upd-keygen.log 2>&1 || { cat /tmp/hcc-upd-keygen.log; die "keygen failed"; }
TRUSTED_KEY="$(tr -d '\n' <"$WORK/release.pub")"
[ -n "$TRUSTED_KEY" ] || die "no public key was written"

# A second keypair that nothing trusts, for the refusal below. Having it means
# "signed by the wrong key" can be tested as itself rather than approximated
# by corrupting a signature — which the verifier reports differently.
"$VENV/bin/python" scripts/sign-release.py keygen \
  --private-key "$WORK/attacker.key" --public-key "$WORK/attacker.pub" \
  >>/tmp/hcc-upd-keygen.log 2>&1 || die "the second keygen failed"

mode="$(stat -c '%a' "$WORK/release.key")"
if [ "$mode" = "600" ]; then
  ok "signing key generated at mode 0600 (id $(grep -o 'key id *[0-9a-f]*' /tmp/hcc-upd-keygen.log | head -1 | awk '{print $3}'))"
else
  bad "the signing key is mode $mode, expected 600"
fi

# ===========================================================================
info "== 4. Three agent builds, stamped with that key =="

# The real build script, not a `go build` shortcut: what the container
# installs and later downloads is what the release workflow would publish.
#
# --trust-key is what makes automatic updates possible at all. Without it
# these binaries would trust nothing and decline every instruction, which is
# the correct behaviour for an unsigned build and is asserted separately.
build_agent() {
  version="$1"; destination="$2"
  ./scripts/build-agent-artifacts.sh \
    --output "$destination" --arch amd64 --version "$version" \
    --trust-key "$TRUSTED_KEY" >>/tmp/hcc-upd-build.log 2>&1 \
    || { tail -30 /tmp/hcc-upd-build.log; die "building $version failed"; }
}

: >/tmp/hcc-upd-build.log
build_agent "$VERSION_OLD" "$WORK/dist-old"
build_agent "$VERSION_NEW" "$WORK/dist-new"
build_agent "$VERSION_BROKEN" "$WORK/dist-broken"

ARCHIVE_OLD="hcc-agent_${VERSION_OLD}_linux_amd64.tar.gz"
ARCHIVE_NEW="hcc-agent_${VERSION_NEW}_linux_amd64.tar.gz"
ARCHIVE_BROKEN="hcc-agent_${VERSION_BROKEN}_linux_amd64.tar.gz"

# The installed binary, unpacked where the container can run it from.
tar xzf "$WORK/dist-old/$ARCHIVE_OLD" -C "$WORK"
chmod 755 "$WORK/hcc-agent"
built_version="$("$WORK/hcc-agent" version | awk '{print $2}')"
if [ "$built_version" = "$VERSION_OLD" ]; then
  ok "built $VERSION_OLD, $VERSION_NEW and $VERSION_BROKEN, each trusting the run's key"
else
  die "the $VERSION_OLD build reports $built_version"
fi

# The mirror serves the archives. The new one as built; the broken one with a
# byte flipped *after* its digest went into the manifest, which is exactly the
# shape of a substituted artifact.
cp "$WORK/dist-new/$ARCHIVE_NEW" "$WORK/mirror/"
cp "$WORK/dist-broken/$ARCHIVE_BROKEN" "$WORK/mirror/"

# ===========================================================================
info "== 5. Disposable database and controller =="

psql_super "CREATE DATABASE \"$TEST_DB\"" >/dev/null
CREATED=1
ok "created $TEST_DB"

(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" "../../$VENV/bin/alembic" upgrade head) \
  >/tmp/hcc-upd-migrate.log 2>&1 || { cat /tmp/hcc-upd-migrate.log; die "migrations failed"; }
SCHEMA_REVISION="$(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" \
  "../../$VENV/bin/alembic" current 2>/dev/null | awk '{print $1}' | tail -1)"
[ -n "$SCHEMA_REVISION" ] || die "could not read the applied schema revision"
ok "migrations applied from zero (head is $SCHEMA_REVISION)"

# ===========================================================================
info "== 6. A signed release manifest =="

# The digests come out of each build's own artifacts.json rather than being
# transcribed, and the compatibility constraints are stated rather than
# omitted: an entry with no protocol versions is "unknown", which blocks every
# automatic update.
#
# schema_requirement is the revision the database actually reports, read above.
# Stating a literal here would make this script fail on the next migration for
# a reason that has nothing to do with updates.
build_manifest() {
  version="$1"; dist="$2"; shift 2
  "$VENV/bin/python" scripts/build-release-manifest.py \
    --artifacts "$dist/artifacts.json" \
    --base-url "https://$CONTROLLER_NAME:$MIRROR_TLS_PORT" \
    --output "$WORK/agent-releases.json" \
    --protocol-versions "1,2" \
    --min-controller "0.1.0" \
    --schema-requirement "$SCHEMA_REVISION" \
    --minimum-supported "$VERSION_OLD" \
    "$@" >>/tmp/hcc-upd-manifest.log 2>&1 \
    || { tail -20 /tmp/hcc-upd-manifest.log; die "building the manifest for $version failed"; }
}

: >/tmp/hcc-upd-manifest.log
build_manifest "$VERSION_OLD" "$WORK/dist-old"
build_manifest "$VERSION_NEW" "$WORK/dist-new" --merge-into "$WORK/agent-releases.json"

sign_manifest() {
  "$VENV/bin/python" scripts/sign-release.py sign \
    --manifest "$WORK/agent-releases.json" --private-key "$1" --force \
    >>/tmp/hcc-upd-manifest.log 2>&1 \
    || { tail -20 /tmp/hcc-upd-manifest.log; die "signing the manifest failed"; }
}

sign_manifest "$WORK/release.key"
if "$VENV/bin/python" scripts/sign-release.py verify \
  --manifest "$WORK/agent-releases.json" --public-key "$WORK/release.pub" \
  >/tmp/hcc-upd-verify.log 2>&1
then
  ok "the manifest names $VERSION_OLD and $VERSION_NEW and carries a valid signature"
else
  cat /tmp/hcc-upd-verify.log
  bad "the signature over the manifest does not verify"
fi

start_controller
ok "controller serving on 127.0.0.1:$PORT (loopback only), trusting one release key"

(cd "$SERVER_DIR" && printf '%s\n' "$ADMIN_PASSWORD" | HCC_DATABASE_URL="$TEST_DB_URL" \
  .venv/bin/python -m hcc.cli.main bootstrap-admin \
  --username "$ADMIN_USER" --password-stdin) >/tmp/hcc-upd-bootstrap.log 2>&1 \
  || { cat /tmp/hcc-upd-bootstrap.log; die "bootstrap failed"; }

COOKIE_JAR="$(mktemp /tmp/hcc-upd-cookies-XXXXXX)"
login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
CSRF="$(printf '%s' "$login" | json_field csrf_token)"
[ -n "$CSRF" ] || die "login did not return a CSRF token: $login"
ok "administrator created and signed in"

signature_status="$(api_get /updates/overview | json_field metadata_signature)"
if [ "$signature_status" = "valid" ]; then
  ok "the controller reports the release metadata as signed by a key it trusts"
else
  bad "the controller reports metadata_signature '$signature_status', expected 'valid'"
fi

# ===========================================================================
info "== 7. A container with real systemd ($IMAGE) =="

case "$IMAGE" in
  debian:*|ubuntu:*)
    cat >"$WORK/Containerfile" <<EOF
FROM $IMAGE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \\
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus ca-certificates \\
 && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN systemctl mask systemd-logind.service systemd-resolved.service \\
      systemd-networkd.service systemd-timesyncd.service \\
      getty.target console-getty.service || true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
    ;;
  fedora:*|rockylinux:*|almalinux:*)
    cat >"$WORK/Containerfile" <<EOF
FROM $IMAGE
RUN dnf -y --setopt=install_weak_deps=False install systemd procps-ng ca-certificates \\
 && dnf clean all
RUN systemctl mask systemd-logind.service systemd-resolved.service \\
      systemd-networkd.service systemd-timesyncd.service \\
      getty.target console-getty.service || true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
    ;;
  *)
    die "unsupported image $IMAGE: this script knows apt and dnf"
    ;;
esac

$RUNTIME build -t "$TEST_IMAGE" -f "$WORK/Containerfile" "$WORK" \
  >/tmp/hcc-upd-image.log 2>&1 || { tail -30 /tmp/hcc-upd-image.log; die "image build failed"; }
IMAGE_BUILT=1
ok "built $TEST_IMAGE with systemd"

PASTA_HOST_ADDRESS="169.254.1.2"
run_arguments="-d --name $CONTAINER --privileged \
  -v $WORK:/mnt/hcc:ro \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp"
if [ "$RUNTIME" = "docker" ]; then
  run_arguments="$run_arguments --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw"
  run_arguments="$run_arguments --add-host $CONTROLLER_NAME:host-gateway"
else
  run_arguments="$run_arguments --systemd=always"
  run_arguments="$run_arguments --network=pasta:--map-host-loopback,$PASTA_HOST_ADDRESS"
  run_arguments="$run_arguments --add-host $CONTROLLER_NAME:$PASTA_HOST_ADDRESS"
fi

# shellcheck disable=SC2086
$RUNTIME run $run_arguments "$TEST_IMAGE" >/dev/null || die "could not start the container"
CONTAINER_STARTED=1

for _ in $(seq 1 60); do
  if in_container systemctl is-system-running 2>/dev/null | \
     grep -qE 'running|degraded|starting'; then
    break
  fi
  sleep 0.5
done
state="$(in_container systemctl is-system-running 2>/dev/null || true)"
case "$state" in
  running|degraded) ok "systemd is pid 1 and the system is $state" ;;
  *) die "systemd did not come up in the container (state: ${state:-none})" ;;
esac

if [ "$RUNTIME" = "docker" ]; then
  BIND_ADDRESS="$(in_container sh -c '
    while read -r _iface dest gw _rest; do
      [ "$dest" = "00000000" ] || continue
      printf "%d.%d.%d.%d\n" \
        "0x$(printf %s "$gw" | cut -c7-8)" "0x$(printf %s "$gw" | cut -c5-6)" \
        "0x$(printf %s "$gw" | cut -c3-4)" "0x$(printf %s "$gw" | cut -c1-2)"
      exit 0
    done < /proc/net/route
  ')"
else
  BIND_ADDRESS="127.0.0.1"
fi
case "$BIND_ADDRESS" in
  [0-9]*.[0-9]*.[0-9]*.[0-9]*) : ;;
  *) die "could not determine where to listen for the container (got '${BIND_ADDRESS}')" ;;
esac
resolved="$(in_container getent hosts "$CONTROLLER_NAME" | awk '{print $1}' | head -1)"
[ -n "$resolved" ] || die "the container cannot resolve $CONTROLLER_NAME"
ok "the container resolves $CONTROLLER_NAME to $resolved; listening on $BIND_ADDRESS"

# ===========================================================================
info "== 8. TLS in front of the controller and the mirror =="

start_tls
start_mirror
CONTROLLER_URL="https://$CONTROLLER_NAME:$TLS_PORT"

# Proved from inside the container, because that is the only place it matters
# — and because a mirror the agent cannot reach would make every update below
# fail for a reason that has nothing to do with updates.
# `bash -c`, not `sh -c`: /dev/tcp is a bash feature and Debian's /bin/sh is
# dash, which does not have it. This probe reported the mirror unreachable for
# one run while the update it was checking for downloaded through it
# successfully — a false negative is worse than no check.
if in_container bash -c "exec 3<>/dev/tcp/$CONTROLLER_NAME/$MIRROR_TLS_PORT" 2>/dev/null; then
  ok "the container can open a connection to the artifact mirror"
else
  bad "the container cannot reach the artifact mirror on $MIRROR_TLS_PORT"
fi

# ===========================================================================
info "== 9. Register a host and install $VERSION_OLD =="

host="$(api POST /hosts \
  "{\"name\":\"update-e2e-$SLUG\",\"description\":\"$IMAGE\",\"environment\":\"test\"}")"
HOST_ID="$(printf '%s' "$host" | json_field host_id)"
[ -n "$HOST_ID" ] || die "host registration failed: $host"

enrollment="$(api POST "/hosts/$HOST_ID/enrollment" '{}')"
credential="$(printf '%s' "$enrollment" | json_field credential)"
[ -n "$credential" ] || die "enrollment issuance failed: $enrollment"
printf '%s' "$credential" | $RUNTIME exec -i "$CONTAINER" \
  sh -c 'cat > /root/enrolment && chmod 600 /root/enrolment'

if in_container /mnt/hcc/hcc-agent install \
  --controller "$CONTROLLER_URL" \
  --ca-file /mnt/hcc/ca.pem \
  --token-file /root/enrolment \
  --non-interactive \
  --connect-timeout 30s >/tmp/hcc-upd-install.log 2>&1
then
  ok "installed $VERSION_OLD and the service connected"
else
  cat /tmp/hcc-upd-install.log
  die "install failed"
fi

await_query "the controller records the host as running $VERSION_OLD" \
  "SELECT agent_version FROM hosts WHERE id = '$HOST_ID'" "$VERSION_OLD" 45 || true

# ===========================================================================
info "== 10. The host's own veto, before anything is enabled =="

# Automatic updates are off: the installer writes HCC_AGENT_AUTO_UPDATE=false
# and does not offer to change it. An administrator starting an update by hand
# is still authorized to — the controller sends the instruction — and the
# agent declines it, which is the half the controller cannot overrule.
if in_container grep -q '^HCC_AGENT_AUTO_UPDATE=false' /etc/hcc-agent/agent.env; then
  ok "the installer wrote HCC_AGENT_AUTO_UPDATE=false"
else
  bad "the environment file does not have automatic updates off:"
  in_container grep -i update /etc/hcc-agent/agent.env | sed 's/^/      /' || true
fi

status_json="$(api_get "/hosts/$HOST_ID/updates")"
outcome="$(printf '%s' "$status_json" | json_field plan.outcome)"
target="$(printf '%s' "$status_json" | json_field plan.target_version)"
if [ "$outcome" = "update_available" ] && [ "$target" = "$VERSION_NEW" ]; then
  ok "the controller offers $VERSION_NEW ($outcome)"
else
  bad "the controller's plan is '$outcome' targeting '$target', expected update_available/$VERSION_NEW"
  printf '%s' "$status_json" | sed 's/^/      /'
fi

started="$(api POST "/hosts/$HOST_ID/updates" '{}')"
job_id="$(printf '%s' "$started" | json_field job_id 2>/dev/null || true)"
if [ -n "$job_id" ]; then
  ok "an administrator started an update while the host has updates disabled"
else
  bad "starting the update failed: $started"
fi

declined="$(await_terminal "the declined job" \
  "SELECT state FROM update_jobs WHERE id = '$job_id'" 120 || true)"
if [ "$declined" = "failed" ]; then
  ok "the agent declined the instruction rather than installing it"
else
  bad "the job reached '$declined', expected 'failed' (declined)"
fi
reason="$(query "SELECT coalesce(error, '') FROM update_jobs WHERE id = '$job_id'" | head -1)"
case "$reason" in
  *"automatic updates"*|*"auto"*|*"not enabled"*)
    ok "the reason reached the controller: $(printf '%s' "$reason" | cut -c1-70)…" ;;
  *)
    bad "the recorded reason does not explain the refusal: '$reason'" ;;
esac
still="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$still" = "$VERSION_OLD" ]; then
  ok "the host is still running $VERSION_OLD"
else
  bad "the declined update changed the binary to $still"
fi

# ===========================================================================
info "== 11. Enable automatic updates on this container only =="

# Written into the container's own environment file — the operator path the
# installer documents, and the only path there is. Nothing on the developer's
# machine or on any real host is touched.
$RUNTIME exec -i "$CONTAINER" sh -c \
  "sed -i 's/^HCC_AGENT_AUTO_UPDATE=false/HCC_AGENT_AUTO_UPDATE=true/' /etc/hcc-agent/agent.env"
in_container systemctl restart hcc-agent
sleep 2
if in_container grep -q '^HCC_AGENT_AUTO_UPDATE=true' /etc/hcc-agent/agent.env; then
  ok "automatic updates enabled in the container and the service restarted"
else
  die "could not enable automatic updates in the container"
fi
if in_container journalctl -u hcc-agent -n 50 --no-pager 2>/dev/null \
  | grep -q "controller-driven updates are enabled"; then
  ok "the agent logged that updates are enabled, with its trusted key count"
else
  bad "the agent did not report that updates are enabled"
  in_container journalctl -u hcc-agent -n 20 --no-pager 2>&1 | tail -10 | sed 's/^/      /' || true
fi

# ===========================================================================
info "== 12. A real update: $VERSION_OLD → $VERSION_NEW =="

started="$(api POST "/hosts/$HOST_ID/updates" '{}')"
job_id="$(printf '%s' "$started" | json_field job_id 2>/dev/null || true)"
[ -n "$job_id" ] || die "starting the update failed: $started"
ok "job $job_id queued"

# Up to three minutes: the agent learns about the job on its next heartbeat
# (30s), then downloads three megabytes, extracts, swaps the binary, restarts
# itself and reconnects.
final="$(await_terminal "the update" \
  "SELECT state FROM update_jobs WHERE id = '$job_id'" 240 || true)"
if [ "$final" = "succeeded" ]; then
  ok "the job succeeded"
else
  bad "the job reached '$final', expected 'succeeded'"
  query "SELECT coalesce(error, '') FROM update_jobs WHERE id = '$job_id'" | sed 's/^/      /'
fi

running_version="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$running_version" = "$VERSION_NEW" ]; then
  ok "the installed binary is $VERSION_NEW"
else
  bad "the installed binary reports $running_version, expected $VERSION_NEW"
fi
in_container test -f /usr/local/bin/hcc-agent.previous \
  && ok "the previous binary is kept for rollback" \
  || bad "no hcc-agent.previous after the update"
if in_container systemctl is-active --quiet hcc-agent; then
  ok "the service is active after the update"
else
  bad "the service is not active after the update"
fi
in_container test -f /etc/hcc-agent/credential \
  && ok "the update did not touch the credential" \
  || bad "the credential is gone after the update"

await_query "the host reports $VERSION_NEW to the controller" \
  "SELECT agent_version FROM hosts WHERE id = '$HOST_ID'" "$VERSION_NEW" 90 || true

resulting="$(query "SELECT coalesce(resulting_version, '') FROM update_jobs WHERE id = '$job_id'" | head -1)"
if [ "$resulting" = "$VERSION_NEW" ]; then
  ok "the job records $VERSION_NEW as the version the host ended up on"
else
  bad "the job's resulting_version is '$resulting', expected $VERSION_NEW"
fi

# ===========================================================================
info "== 13. The event log holds what the agent actually reported =="

# The states below came off the wire from the host. None of them is derived
# from a timer, and the interface renders this log rather than a simulation —
# so a stuck download shows a bar that does not move.
events="$(query "SELECT state FROM update_events WHERE job_id = '$job_id' ORDER BY id")"
dim "$(printf '%s' "$events" | tr '\n' ' ' | sed 's/^/    /')"
for expected in downloading verifying installing restarting succeeded; do
  if printf '%s\n' "$events" | grep -qx "$expected"; then
    ok "the host reported '$expected'"
  else
    bad "no '$expected' event was recorded"
  fi
done

# The one state no process can report about itself.
concluded="$(query "SELECT source FROM update_events
  WHERE job_id = '$job_id' AND state = 'succeeded'" | head -1)"
if [ "$concluded" = "controller" ]; then
  ok "'succeeded' came from the controller, which is the only thing that can see it"
else
  bad "'succeeded' was recorded with source '$concluded', expected 'controller'"
fi

agent_reported="$(query "SELECT count(*) FROM update_events
  WHERE job_id = '$job_id' AND source = 'agent'" | head -1)"
if [ "${agent_reported:-0}" -ge 4 ]; then
  ok "$agent_reported events came from the host itself"
else
  bad "only ${agent_reported:-0} events came from the host; the log is mostly inference"
fi

# ===========================================================================
info "== 14. An update whose bytes do not match the signed digest =="

# The substituted-artifact case, built the only honest way: the manifest is
# generated from the real build's digest and signed, and *then* a byte of the
# served archive is flipped. Nothing about the manifest is wrong — the
# signature is valid, the controller is happy — and the agent still refuses,
# because the digest it checks comes out of the signed metadata.
build_manifest "$VERSION_BROKEN" "$WORK/dist-broken" --merge-into "$WORK/agent-releases.json"
sign_manifest "$WORK/release.key"

"$VENV/bin/python" - "$WORK/mirror/$ARCHIVE_BROKEN" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = bytearray(path.read_bytes())
# The last byte, XOR 0xFF. The same length, so the size check cannot be what
# refuses it — the digest has to be.
data[-1] ^= 0xFF
path.write_bytes(bytes(data))
PY
ok "flipped a byte of the served $VERSION_BROKEN archive, keeping its length"

restart_controller
dim "  controller restarted to pick up the new manifest"

started="$(api POST "/hosts/$HOST_ID/updates" '{}')"
job_id="$(printf '%s' "$started" | json_field job_id 2>/dev/null || true)"
[ -n "$job_id" ] || die "starting the update failed: $started"

final="$(await_terminal "the substituted update" \
  "SELECT state FROM update_jobs WHERE id = '$job_id'" 240 || true)"
if [ "$final" = "failed" ]; then
  ok "the agent refused the substituted archive"
else
  bad "the job reached '$final', expected 'failed'"
fi
reason="$(query "SELECT coalesce(error, '') FROM update_jobs WHERE id = '$job_id'" | head -1)"
case "$reason" in
  *digest*|*sha256*|*"does not match"*)
    ok "the refusal names the digest: $(printf '%s' "$reason" | cut -c1-70)…" ;;
  *)
    bad "the refusal does not mention the digest: '$reason'" ;;
esac

# The point of the whole exercise: a failed update leaves a working host.
running_version="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$running_version" = "$VERSION_NEW" ]; then
  ok "the host is still running $VERSION_NEW"
else
  bad "the refused update left $running_version installed"
fi
if in_container systemctl is-active --quiet hcc-agent; then
  ok "the service is still active"
else
  bad "the service is not active after the refused update"
fi
# Refused before the download touched the installation at all: there is no
# second .previous and no staged binary left behind.
staged="$(in_container sh -c 'ls /var/tmp/hcc-agent-update* /tmp/hcc-agent-update* 2>/dev/null | wc -l' || echo 0)"
if [ "${staged:-0}" = "0" ]; then
  ok "no staged archive was left behind"
else
  bad "$staged staged files remain after the refusal"
fi

# ===========================================================================
info "== 15. A manifest signed by a key nothing trusts =="

# Re-signed with the attacker keypair. The *bytes* are identical to the ones
# that worked a moment ago, so nothing about the release has changed — only
# who vouched for it. Both halves have to refuse: the controller reports the
# signature as untrusted, and the agent declines the instruction.
sign_manifest "$WORK/attacker.key"
restart_controller

signature_status="$(api_get /updates/overview | json_field metadata_signature)"
if [ "$signature_status" = "untrusted_key" ]; then
  ok "the controller reports the metadata as signed by a key it does not trust"
else
  bad "the controller reports '$signature_status', expected 'untrusted_key'"
fi

# And the planner refuses to offer it, rather than offering an update the
# agent would decline.
#
# Waited for first, for the reason §16 already names: the controller was just
# restarted, and a host that has not reported its version yet plans as
# `unknown_version`. That outcome is also "not an actionable update", so
# accepting it here would let this assertion pass with the signature check
# broken — it would be passing because the controller does not know the
# version, not because it refuses untrusted metadata. So the version is
# waited for and `unknown_version` stays a failure.
#
# This was a CI flake before it was a wait: the step passed locally and on
# most runs, and failed on one where the agent reconnected a second later.
await_query "the host has reported its version again after the restart" \
  "SELECT coalesce(agent_version, '') FROM hosts WHERE id = '$HOST_ID'" "$VERSION_NEW" 120 || true

status_json="$(api_get "/hosts/$HOST_ID/updates")"
outcome="$(printf '%s' "$status_json" | json_field plan.outcome)"
case "$outcome" in
  up_to_date|update_available_incompatible|metadata_unavailable)
    ok "the plan is '$outcome' rather than an actionable update" ;;
  *)
    reported="$(query "SELECT coalesce(agent_version, '(null)') FROM hosts WHERE id = '$HOST_ID'" | head -1)"
    bad "the plan is '$outcome' for metadata nothing can vouch for (the host reports $reported)" ;;
esac

# Put the real signature back, so the state at the end of the run is coherent
# for anybody using --keep.
sign_manifest "$WORK/release.key"
restart_controller
signature_status="$(api_get /updates/overview | json_field metadata_signature)"
[ "$signature_status" = "valid" ] \
  && ok "the trusted signature was restored" \
  || bad "the signature is '$signature_status' after restoring it"

# ===========================================================================
info "== 16. The history an administrator reads =="

history="$(api_get "/updates/history?host_id=$HOST_ID&limit=10")"
job_count="$(printf '%s' "$history" | "$VENV/bin/python" -c '
import json, sys
print(len(json.load(sys.stdin)["jobs"]))')"
if [ "${job_count:-0}" -ge 3 ]; then
  ok "the history holds all $job_count attempts, including the ones that failed"
else
  bad "the history holds ${job_count:-0} attempts, expected at least 3"
fi

# The controller was restarted three times in the section above, so the agent
# may still be reconnecting. Waited for rather than assumed: the assertion is
# that the summary shows *the host's own report*, and a host that has not
# reported yet correctly shows nothing. Asserting before it reconnects would
# be testing the reconnection delay.
await_query "the host has reported its version again after the restarts" \
  "SELECT coalesce(agent_version, '') FROM hosts WHERE id = '$HOST_ID'" "$VERSION_NEW" 120 || true

overview="$(api_get /updates/overview)"
reported_version="$(printf '%s' "$overview" | "$VENV/bin/python" -c '
import json, sys
document = json.load(sys.stdin)
entries = document["version_distribution"]
print(entries[0]["version"] if entries else "")')"
if [ "$reported_version" = "$VERSION_NEW" ]; then
  ok "the fleet summary shows $VERSION_NEW, which is the host's own report"
else
  bad "the version distribution leads with '$reported_version', expected $VERSION_NEW"
fi

# ===========================================================================
info "== 17. A staged rollout, driven by the controller's own background pass =="

# What this proves, and what it does not.
#
# **Proves:** the whole rollout loop runs in a real controller — create
# through the API, the background task picking it up on its own interval,
# batch assignment, the per-member decision, and the terminal state — with no
# test reaching into the service.
#
# **Does not prove:** a canary installing and then gating a second batch.
# That needs two hosts and a version this one is not already on, and the
# fixture has one container and no spare build. It is covered by the 30
# integration tests in `test_rollout_service_integration.py`, which drive
# real jobs through real state transitions against a real database.
#
# The member here is **skipped**, because the host already runs
# $VERSION_NEW — which is itself one of the behaviours worth proving at this
# level: "already on the target version" must not be a failure, or a rollout
# re-run over a partly-updated fleet would pause on the hosts that were
# already done.

rollout_body="$("$VENV/bin/python" - "$HOST_ID" "$VERSION_NEW" <<'PY'
import json
import sys

print(
    json.dumps(
        {
            "name": "end-to-end rollout",
            "to_version": sys.argv[2],
            "host_ids": [sys.argv[1]],
            # No settle: the member is skipped rather than installed, so
            # there is nothing to wait for and a 120-second default would
            # only make this scenario slower.
            "health_check_seconds": 0,
        }
    )
)
PY
)"
created="$(api POST /updates/rollouts "$rollout_body")"
ROLLOUT_ID="$(printf '%s' "$created" | json_field rollout_id 2>/dev/null || true)"
if [ -n "$ROLLOUT_ID" ]; then
  ok "a rollout was created over 1 host"
else
  bad "creating the rollout failed: $created"
fi

if [ -n "$ROLLOUT_ID" ]; then
  # Pending, not started. The request creates; the background pass runs.
  state="$(printf '%s' "$created" | json_field state)"
  if [ "$state" = "pending" ]; then
    ok "the rollout is pending — creating one starts nothing"
  else
    bad "a freshly created rollout is '$state', expected pending"
  fi

  # Now wait for the controller's own task, which runs every 30 seconds by
  # default. Nothing here nudges it: if this passes, the background pass is
  # genuinely running in the controller under test.
  #
  # `await_query` rather than `await_terminal`: the latter recognises *job*
  # terminal states, and a rollout's are a different set.
  await_query "the background pass carried the rollout to completion" \
    "SELECT state FROM update_rollouts WHERE id = '$ROLLOUT_ID'" "completed" 120

  skipped="$(query "SELECT coalesce(skipped_reason, '') FROM update_rollout_members
                    WHERE rollout_id = '$ROLLOUT_ID'" | head -1)"
  case "$skipped" in
    *"already running"*)
      ok "the host was skipped, not failed: $(printf '%s' "$skipped" | cut -c1-60)…" ;;
    "")
      bad "the member was neither skipped nor explained" ;;
    *)
      bad "the skip reason does not name the cause: '$skipped'" ;;
  esac

  # And no job was created for it. A skip must not leave an update attempt
  # behind, or the history would show one that never happened.
  rollout_jobs="$(query "SELECT count(*) FROM update_jobs WHERE rollout_id = '$ROLLOUT_ID'")"
  if [ "$rollout_jobs" = "0" ]; then
    ok "no update job was created for a skipped member"
  else
    bad "$rollout_jobs job(s) exist for a rollout that installed nothing"
  fi
fi

# ===========================================================================
info "== Result =="

printf '\n'
green "  $PASS passed"
if [ "$FAIL" -gt 0 ]; then
  red "  $FAIL failed"
  dump_logs
  exit 1
fi
green "✓ a real version-to-version update, verified end to end on $IMAGE"
