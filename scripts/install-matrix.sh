#!/usr/bin/env bash
#
# The installation lifecycle, against real systemd, on a real distribution.
#
# A container with systemd as pid 1, a real controller behind real TLS with a
# real internal CA, and the actual `hcc-agent install` command a person would
# paste. Then: does the service start, does the host connect, does a real
# sample arrive, does `doctor` agree, does `upgrade` swap and roll back, does
# `uninstall` leave the machine clean.
#
# # Why this exists, and what it already caught
#
# The unit file in this repository had `ProcSubset=pid` in it for several
# milestones. That directive mounts /proc with `subset=pid`, which hides
# /proc/stat, /proc/meminfo, /proc/loadavg and /proc/uptime — every file the
# agent reads. The service would have started, connected, reported itself
# healthy, accepted a configuration and collected nothing.
#
# Nothing caught it, because nothing had ever *started* the unit. It had been
# checked with `systemd-analyze verify`, which parses and does not execute, and
# the agent had only ever been run in the foreground. A mocked systemd would
# not have caught it either. The check that catches it is step 5 below: enable
# a module and assert a row appears.
#
# Usage:
#   make db-up
#   ./scripts/install-matrix.sh --image debian:13
#   ./scripts/install-matrix.sh --image fedora:42 --keep
#
# Safety
# ------
# Nothing here touches the developer's installation or any remote host:
#
#   * The database is created for this run and dropped on every exit path.
#   * The controller listens on loopback only. The TLS terminator in front of
#     it listens on **one** address and nothing else: the host's loopback
#     under rootless Podman, which maps it into the container with pasta's
#     --map-host-loopback, or the container bridge's gateway under Docker,
#     discovered from inside the container after it starts. No socket is
#     opened on any other interface, and never on 0.0.0.0.
#   * The container is disposable, is named for this run, and is removed on
#     every exit path unless --keep is given.
#   * The container is privileged, because systemd as pid 1 needs it. It gets
#     a read-only mount of one temporary directory and no access to the
#     repository, the developer's home or the Docker socket.
#   * No image is pushed anywhere. The test image is built locally from an
#     inline Containerfile and removed at the end.
#
# The container being privileged is the one real risk, and it is why this is a
# separate script rather than part of `make check`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
AGENT_DIR="agents/linux"
VENV="$SERVER_DIR/.venv"

IMAGE="debian:13"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

RUN_ID="$(date +%s)-$$"
SLUG="$(printf '%s' "$IMAGE" | tr -c 'a-zA-Z0-9' '-' | tr -s '-' | sed 's/^-//;s/-$//')"
CONTAINER="hcc-install-$SLUG-$RUN_ID"
TEST_IMAGE="hcc-install-test-$SLUG:$RUN_ID"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
TEST_DB="hcc_inst_$(date +%s)_$$"
TEST_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$TEST_DB"

# Not 8000, 8099, 8201 or 8211: a dev server, the smoke test, the browser
# suite and the monitoring scenario must not be mistaken for this one.
PORT=8221
TLS_PORT=8222
BASE="http://127.0.0.1:$PORT"
API="$BASE/api/v1"
# The name on the certificate. A .test name is reserved by RFC 6761 and will
# never resolve publicly, so a leak of this certificate is worth nothing.
CONTROLLER_NAME="hcc.test"

ADMIN_USER="install-matrix-admin"
ADMIN_PASSWORD="install-matrix-$(date +%s)-correct-horse-battery"

WORK=""
SERVER_PID=""
TLS_PID=""
CREATED=""
COOKIE_JAR=""
CSRF=""
HOST_ID=""
CONTAINER_STARTED=""
IMAGE_BUILT=""

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
  for log in /tmp/hcc-inst-server.log /tmp/hcc-inst-tls.log; do
    if [ -s "$log" ]; then
      red "--- $log (tail) ---"
      tail -30 "$log" || true
    fi
  done
  if [ -n "$CONTAINER_STARTED" ]; then
    red "--- journalctl -u hcc-agent (in the container) ---"
    $RUNTIME exec "$CONTAINER" journalctl -u hcc-agent -n 40 --no-pager 2>&1 | tail -40 || true
    red "--- systemctl status hcc-agent ---"
    $RUNTIME exec "$CONTAINER" systemctl status hcc-agent --no-pager 2>&1 | tail -25 || true
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
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  [ -n "$COOKIE_JAR" ] && rm -f "$COOKIE_JAR"
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

# Run a command inside the container.
in_container() { $RUNTIME exec "$CONTAINER" "$@"; }

# Poll until a query's first column equals a value.
await_query() {
  label="$1"; sql="$2"; expected="$3"; timeout="${4:-45}"; actual=""
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    actual="$(query "$sql" | head -1 || true)"
    if [ "$actual" = "$expected" ]; then
      ok "$label"
      return 0
    fi
    sleep 0.5
  done
  bad "$label — expected '$expected', last saw '$actual' after ${timeout}s"
  return 1
}

await_at_least() {
  label="$1"; sql="$2"; threshold="$3"; timeout="${4:-45}"; actual="0"
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    actual="$(query "$sql" | head -1 || true)"
    actual="${actual:-0}"
    case "$actual" in
      ''|*[!0-9-]*) actual=0 ;;
    esac
    if [ "$actual" -ge "$threshold" ]; then
      ok "$label ($actual)"
      return 0
    fi
    sleep 0.5
  done
  bad "$label — wanted at least $threshold, last saw '$actual' after ${timeout}s"
  return 1
}

# Stop the TLS terminator, firmly.
#
# A plain `kill` then `wait` is not enough: the terminator relays long-lived
# WebSocket connections, and a shutdown that waits for them to drain waits for
# ever. It no longer does — but the script should not depend on that, because
# a hang here is a hang in CI with no output.
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
    --key "$WORK/server.key" >>/tmp/hcc-inst-tls.log 2>&1 &
  TLS_PID=$!
  sleep 1
  kill -0 "$TLS_PID" 2>/dev/null || { cat /tmp/hcc-inst-tls.log; die "the TLS terminator died"; }
}

wait_for_http() {
  for _ in $(seq 1 100); do
    if curl -sS -o /dev/null --max-time 2 "$1" 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  return 1
}

# ===========================================================================
info "== 1. Preconditions =="

[ -x "$VENV/bin/python" ] || die "Python venv missing. Run: make install-server"
command -v go >/dev/null || die "Go missing. The agent is built from source here."
command -v openssl >/dev/null || die "openssl missing. The internal CA is generated with it."
command -v curl >/dev/null || die "curl missing."
ok "toolchain present ($RUNTIME, $(go version | awk '{print $3}'))"

if curl -sS -o /dev/null --max-time 2 "$BASE/" 2>/dev/null; then
  die "Something is already listening on port $PORT. Stop it first."
fi
ok "ports $PORT and $TLS_PORT are free"

trap cleanup EXIT
WORK="$(mktemp -d /tmp/hcc-inst-XXXXXX)"
chmod 755 "$WORK"

# ===========================================================================
info "== 2. An internal certificate authority =="

# A CA, and a certificate for the controller's name signed by it. This is the
# arrangement a homelab actually has, and it is the one `--ca-file` exists for
# — so testing against a self-signed *leaf* certificate would be testing
# something nobody runs.
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 1 \
  -keyout "$WORK/ca.key" -out "$WORK/ca.pem" \
  -subj "/CN=Homelab Control Center install-matrix CA" \
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
info "== 3. Disposable database and controller =="

psql_super "CREATE DATABASE \"$TEST_DB\"" >/dev/null
CREATED=1
ok "created $TEST_DB"

(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" "../../$VENV/bin/alembic" upgrade head) \
  >/tmp/hcc-inst-migrate.log 2>&1 || { cat /tmp/hcc-inst-migrate.log; die "migrations failed"; }
ok "migrations applied from zero"

(
  cd "$SERVER_DIR"
  exec env \
    HCC_DATABASE_URL="$TEST_DB_URL" \
    HCC_HOST=127.0.0.1 \
    HCC_PORT="$PORT" \
    HCC_ENVIRONMENT=development \
    HCC_LOG_FORMAT=json \
    HCC_METRICS_MAINTENANCE_INTERVAL_SECONDS=0 \
    .venv/bin/python -m hcc.main
) >/tmp/hcc-inst-server.log 2>&1 &
SERVER_PID=$!

wait_for_http "$BASE/healthz" || die "the controller did not start"
ok "controller serving on 127.0.0.1:$PORT (loopback only)"

(cd "$SERVER_DIR" && printf '%s\n' "$ADMIN_PASSWORD" | HCC_DATABASE_URL="$TEST_DB_URL" \
  .venv/bin/python -m hcc.cli.main bootstrap-admin \
  --username "$ADMIN_USER" --password-stdin) >/tmp/hcc-inst-bootstrap.log 2>&1 \
  || { cat /tmp/hcc-inst-bootstrap.log; die "bootstrap failed"; }

COOKIE_JAR="$(mktemp /tmp/hcc-inst-cookies-XXXXXX)"
login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
CSRF="$(printf '%s' "$login" | json_field csrf_token)"
[ -n "$CSRF" ] || die "login did not return a CSRF token: $login"
ok "administrator created and signed in"

# ===========================================================================
info "== 4. The release artifacts, built as CI builds them =="

# The real build script, not a `go build` shortcut: what gets installed in the
# container is what the release workflow would publish, archive and all.
./scripts/build-agent-artifacts.sh --output "$WORK/dist" --arch amd64 \
  >/tmp/hcc-inst-build.log 2>&1 || { cat /tmp/hcc-inst-build.log; die "artifact build failed"; }

ARCHIVE="$(cd "$WORK/dist" && ls hcc-agent_*_linux_amd64.tar.gz)"
DIGEST="$(cd "$WORK/dist" && sha256sum "$ARCHIVE" | cut -d' ' -f1)"
(cd "$WORK/dist" && tar xzf "$ARCHIVE" -C "$WORK")
# Beside the unpacked binary rather than only under dist/, because the
# container sees $WORK as /mnt/hcc and the rollback scenario installs this
# archive by path. It was only under dist/ for one run, and the rollback step
# then failed to find it — while the assertion after it passed *vacuously*,
# because nothing had been changed to roll back. Asserting on the specific
# message rather than on the exit code is what caught that.
cp "$WORK/dist/$ARCHIVE" "$WORK/"
chmod 755 "$WORK/hcc-agent"
INSTALLED_VERSION="$("$WORK/hcc-agent" version | awk '{print $2}')"
ok "built and unpacked $ARCHIVE ($INSTALLED_VERSION)"

# A second archive with a different version string, for the upgrade. Built
# from the same source with a different stamp, which is exactly what a real
# upgrade is.
UPGRADE_VERSION="99.0.0"
./scripts/build-agent-artifacts.sh --output "$WORK/dist-next" --arch amd64 \
  --version "$UPGRADE_VERSION" >>/tmp/hcc-inst-build.log 2>&1 \
  || { tail -20 /tmp/hcc-inst-build.log; die "upgrade artifact build failed"; }
UPGRADE_ARCHIVE="hcc-agent_${UPGRADE_VERSION}_linux_amd64.tar.gz"
UPGRADE_DIGEST="$(cd "$WORK/dist-next" && sha256sum "$UPGRADE_ARCHIVE" | cut -d' ' -f1)"
cp "$WORK/dist-next/$UPGRADE_ARCHIVE" "$WORK/"
ok "built $UPGRADE_ARCHIVE for the upgrade"

# ===========================================================================
info "== 5. A container with real systemd ($IMAGE) =="

# systemd has to be in the image before it can be pid 1, so the image is built
# rather than installed into at runtime. Built locally, never pushed.
case "$IMAGE" in
  debian:*|ubuntu:*)
    cat >"$WORK/Containerfile" <<EOF
FROM $IMAGE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \\
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus \\
 && apt-get clean && rm -rf /var/lib/apt/lists/*
# Nothing in a container needs these, and they slow the boot while producing
# failures that obscure the agent's own.
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
RUN dnf -y --setopt=install_weak_deps=False install systemd procps-ng \\
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
  >/tmp/hcc-inst-image.log 2>&1 || { tail -30 /tmp/hcc-inst-image.log; die "image build failed"; }
IMAGE_BUILT=1
ok "built $TEST_IMAGE with systemd"

# Not host networking. A privileged container sharing the host's network
# namespace is a privileged container that can see every socket on the
# machine, and this script runs on developers' laptops.
#
# The two runtimes reach the host differently, and the difference is not
# cosmetic:
#
#   * Docker puts the container on a bridge whose gateway *is* a real address
#     on the host, so the TLS terminator binds to that gateway.
#   * Rootless Podman runs the whole network inside a user namespace, so the
#     bridge gateway is not a host address at all and nothing on the host can
#     bind it. Pasta's --map-host-loopback forwards a chosen address to the
#     host's **loopback** instead, which is strictly better: the terminator
#     then listens on 127.0.0.1 and on no other interface.
#
# Either way the container resolves the certificate's name to whichever
# address applies, so the agent sees one URL and one certificate.
PASTA_HOST_ADDRESS="169.254.1.2"
run_arguments="-d --name $CONTAINER --privileged \
  -v $WORK:/mnt/hcc:ro \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp"
if [ "$RUNTIME" = "docker" ]; then
  # systemd needs a writable cgroup hierarchy. --cgroupns=host is what makes
  # that work on a cgroup v2 host, which every current distribution is.
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

# Where the TLS terminator has to listen for the container to reach it.
if [ "$RUNTIME" = "docker" ]; then
  # The bridge gateway, read from the container's own routing table rather
  # than from `docker network inspect`, so a non-default bridge works too.
  # /proc/net/route rather than `ip route`, which a minimal image may not
  # have: the gateway is a little-endian hex word, hence the reversal.
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
info "== 6. TLS in front of the controller, bound to the bridge only =="

start_tls

CONTROLLER_URL="https://$CONTROLLER_NAME:$TLS_PORT"
# Proved from inside the container, which is the only place it matters.
if in_container bash -c "exec 3<>/dev/tcp/$CONTROLLER_NAME/$TLS_PORT" 2>/dev/null; then
  ok "the container can open a connection to $CONTROLLER_NAME:$TLS_PORT"
else
  dim "  (could not probe the port from inside; the install below will prove it)"
fi

# ===========================================================================
info "== 7. Register a host and issue one credential =="

host="$(api POST /hosts \
  "{\"name\":\"install-matrix-$SLUG\",\"description\":\"$IMAGE\",\"environment\":\"test\"}")"
HOST_ID="$(printf '%s' "$host" | json_field host_id)"
[ -n "$HOST_ID" ] || die "host registration failed: $host"
ok "host registered ($HOST_ID)"

enrollment="$(api POST "/hosts/$HOST_ID/enrollment" '{}')"
credential="$(printf '%s' "$enrollment" | json_field credential)"
[ -n "$credential" ] || die "enrollment issuance failed: $enrollment"

# Written into the container with mode 0600 — the automation path the
# installer documents. Never a command-line argument: /proc makes an argv
# readable by every local user, and the installer has no flag for it.
in_container sh -c 'install -m 0600 /dev/null /root/enrolment'
printf '%s' "$credential" | $RUNTIME exec -i "$CONTAINER" \
  sh -c 'cat > /root/enrolment && chmod 600 /root/enrolment'
ok "one-time credential written to the container at mode 0600"

# ===========================================================================
info "== 8. install --dry-run changes nothing =="

dry="$(in_container /mnt/hcc/hcc-agent install \
  --controller "$CONTROLLER_URL" \
  --ca-file /mnt/hcc/ca.pem \
  --token-file /root/enrolment \
  --non-interactive --dry-run 2>&1)" || { echo "$dry"; die "--dry-run failed"; }

if printf '%s' "$dry" | grep -q "ProtectSystem=strict"; then
  ok "--dry-run printed the unit it would install"
else
  bad "--dry-run did not print the unit"
fi
if printf '%s' "$dry" | grep -q "certificate verified"; then
  ok "--dry-run verified the controller's certificate against the internal CA"
else
  bad "--dry-run did not report a verified certificate"
fi
if printf '%s' "$dry" | grep -q "$credential"; then
  bad "--dry-run printed the enrolment credential"
else
  ok "--dry-run did not print the credential"
fi
if in_container test -e /usr/local/bin/hcc-agent; then
  bad "--dry-run installed the binary"
else
  ok "--dry-run created nothing"
fi

# ===========================================================================
info "== 9. install =="

if in_container /mnt/hcc/hcc-agent install \
  --controller "$CONTROLLER_URL" \
  --ca-file /mnt/hcc/ca.pem \
  --token-file /root/enrolment \
  --non-interactive \
  --connect-timeout 30s >/tmp/hcc-inst-install.log 2>&1
then
  ok "install reported success"
else
  cat /tmp/hcc-inst-install.log
  die "install failed"
fi
dim "$(sed 's/^/    /' /tmp/hcc-inst-install.log | tail -30)"

# The things the installer claims to have done, checked independently of what
# it said.
in_container test -x /usr/local/bin/hcc-agent && ok "the binary is installed and executable" \
  || bad "no executable at /usr/local/bin/hcc-agent"
in_container test -f /etc/systemd/system/hcc-agent.service && ok "the unit is installed" \
  || bad "no unit at /etc/systemd/system/hcc-agent.service"
in_container id hcc-agent >/dev/null 2>&1 && ok "the hcc-agent service account exists" \
  || bad "no hcc-agent account"

mode="$(in_container stat -c '%a %U' /etc/hcc-agent/credential 2>/dev/null || true)"
if [ "$mode" = "600 hcc-agent" ]; then
  ok "the credential is 0600 and owned by hcc-agent"
else
  bad "the credential is '$mode', expected '600 hcc-agent'"
fi
dirmode="$(in_container stat -c '%a %U' /etc/hcc-agent 2>/dev/null || true)"
if [ "$dirmode" = "750 hcc-agent" ]; then
  ok "the configuration directory is 0750 and owned by hcc-agent"
else
  bad "the configuration directory is '$dirmode', expected '750 hcc-agent'"
fi
envmode="$(in_container stat -c '%a %U' /etc/hcc-agent/agent.env 2>/dev/null || true)"
if [ "$envmode" = "644 root" ]; then
  ok "the environment file is 0644 and root-owned, and holds no secret"
else
  bad "the environment file is '$envmode', expected '644 root'"
fi
if in_container grep -q "$credential" /etc/hcc-agent/agent.env 2>/dev/null; then
  bad "the environment file contains the enrolment credential"
else
  ok "the environment file contains no credential"
fi

if in_container systemctl is-active --quiet hcc-agent; then
  ok "the service is active"
else
  bad "the service is not active"
fi
if in_container systemctl is-enabled --quiet hcc-agent; then
  ok "the service is enabled at boot"
else
  bad "the service is not enabled"
fi

# The service really is unprivileged, read from /proc rather than from the
# unit file: a directive that did not take effect is exactly what this job
# exists to catch.
service_user="$(in_container sh -c '
  pid=$(systemctl show -p MainPID --value hcc-agent)
  [ "$pid" != "0" ] && awk "/^Uid:/ {print \$2}" /proc/$pid/status
' 2>/dev/null || true)"
if [ -n "$service_user" ] && [ "$service_user" != "0" ]; then
  ok "the service process runs as uid $service_user, not root"
else
  bad "the service process's uid is '$service_user' (0 means it is running as root)"
fi

await_query "the controller records the host as enrolled" \
  "SELECT lifecycle FROM hosts WHERE id = '$HOST_ID'" "enrolled" 30 || true

# Connectivity is asked of the API rather than read from a column, because it
# is *derived* (ADR-043) — there is no boolean to read, and the derivation is
# what the interface shows. A test asserting on the raw columns would pass
# while the thing an operator sees said something else.
await_connectivity() {
  label="$1"; expected="$2"; timeout="${3:-45}"; actual=""
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    actual="$(api_get "/hosts/$HOST_ID" | json_field connectivity 2>/dev/null || true)"
    if [ "$actual" = "$expected" ]; then
      ok "$label"
      return 0
    fi
    sleep 0.5
  done
  bad "$label — expected '$expected', last saw '$actual' after ${timeout}s"
  return 1
}

await_connectivity "the interface reports the host as connected" "connected" 30 || true

if in_container test -f /etc/hcc-agent/config.json; then
  ok "the agent stored the configuration the controller sent"
else
  bad "no /etc/hcc-agent/config.json — the agent could not persist its configuration"
fi

# ===========================================================================
info "== 10. Enable a module, and watch for a real sample =="

# The check this whole job exists for. ProcSubset=pid in the unit would let
# everything above pass and this fail, which is exactly how the defect
# survived several milestones.
# The response is checked, not discarded. A wrong path here returns a 404 and
# the scenario would then wait sixty seconds for samples from a module nobody
# had enabled, and report the sandbox as broken — which is exactly what
# happened the first time this script ran.
enable_response="$(curl -sS -o /tmp/hcc-inst-enable.json -w '%{http_code}' \
  -X PUT "$API/hosts/$HOST_ID/monitoring/modules/cpu" \
  -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"enabled":true,"schedule":{"kind":"interval","interval_seconds":1}}')"
if [ "$enable_response" = "200" ]; then
  ok "cpu enabled for this host at a one-second interval"
else
  bad "enabling cpu returned HTTP $enable_response: $(head -c 300 /tmp/hcc-inst-enable.json)"
fi

await_at_least "real CPU samples arrive from inside the sandbox" \
  "SELECT count(*) FROM metric_samples_raw s JOIN metric_series e ON e.id = s.series_id
   WHERE e.host_id = '$HOST_ID' AND e.module = 'cpu'" 2 60 || {
  red "    The service is running and no sample arrived. The usual cause is a"
  red "    sandbox directive hiding /proc from the service — ProcSubset=pid"
  red "    hides exactly the four files the agent reads."
  dump_logs
}

# ===========================================================================
info "== 11. doctor =="

if in_container /usr/local/bin/hcc-agent doctor >/tmp/hcc-inst-doctor.log 2>&1; then
  ok "doctor exits zero on a healthy installation"
else
  red "--- doctor ---"
  cat /tmp/hcc-inst-doctor.log
  bad "doctor reported a problem on a healthy installation"
fi
dim "$(sed 's/^/    /' /tmp/hcc-inst-doctor.log | tail -40)"

if grep -q "$credential" /tmp/hcc-inst-doctor.log; then
  bad "doctor printed the enrolment credential"
else
  ok "doctor's output contains no credential"
fi
if in_container /usr/local/bin/hcc-agent doctor --json \
  | "$VENV/bin/python" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
  ok "doctor --json produces valid JSON"
else
  bad "doctor --json did not produce valid JSON"
fi

# ===========================================================================
info "== 12. A second install is refused =="

if in_container /mnt/hcc/hcc-agent install \
  --controller "$CONTROLLER_URL" --ca-file /mnt/hcc/ca.pem \
  --token-file /root/enrolment --non-interactive >/tmp/hcc-inst-again.log 2>&1
then
  bad "a second install proceeded without --force"
else
  if grep -q "already has a complete installation" /tmp/hcc-inst-again.log; then
    ok "a second install is refused, and names upgrade as the alternative"
  else
    bad "a second install failed for the wrong reason:"
    sed 's/^/      /' /tmp/hcc-inst-again.log | tail -5
  fi
fi
if in_container systemctl is-active --quiet hcc-agent; then
  ok "the refused install left the running service alone"
else
  bad "the refused install disturbed the running service"
fi

# ===========================================================================
info "== 13. upgrade =="

if in_container /usr/local/bin/hcc-agent upgrade \
  --from "/mnt/hcc/$UPGRADE_ARCHIVE" --sha256 "$UPGRADE_DIGEST" \
  --connect-timeout 30s >/tmp/hcc-inst-upgrade.log 2>&1
then
  ok "upgrade reported success"
else
  cat /tmp/hcc-inst-upgrade.log
  bad "upgrade failed"
fi

running_version="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$running_version" = "$UPGRADE_VERSION" ]; then
  ok "the installed binary is now $UPGRADE_VERSION"
else
  bad "the installed binary reports $running_version, expected $UPGRADE_VERSION"
fi
in_container test -f /usr/local/bin/hcc-agent.previous \
  && ok "the previous binary is kept for rollback" \
  || bad "no hcc-agent.previous after the upgrade"
if in_container systemctl is-active --quiet hcc-agent; then
  ok "the service is active after the upgrade"
else
  bad "the service is not active after the upgrade"
fi
await_query "the upgraded agent reports its new version to the controller" \
  "SELECT agent_version FROM hosts WHERE id = '$HOST_ID'" "$UPGRADE_VERSION" 45 || true

# ===========================================================================
info "== 14. A digest mismatch is refused =="

if in_container /usr/local/bin/hcc-agent upgrade \
  --from "/mnt/hcc/$UPGRADE_ARCHIVE" \
  --sha256 "0000000000000000000000000000000000000000000000000000000000000000" \
  >/tmp/hcc-inst-mismatch.log 2>&1
then
  bad "an upgrade with a wrong digest was accepted"
else
  if grep -q "does not match the digest" /tmp/hcc-inst-mismatch.log; then
    ok "a digest mismatch is refused with the two digests printed"
  else
    bad "the refusal did not name the problem"
  fi
fi
still="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$still" = "$UPGRADE_VERSION" ]; then
  ok "the refused upgrade changed nothing"
else
  bad "the refused upgrade left version $still installed"
fi

# ===========================================================================
info "== 15. An upgrade that cannot reconnect rolls back =="

# The controller is made unreachable by stopping the TLS terminator, which is
# the shape of a real failure: an upgrade attempted while the network is
# broken. The new binary starts, cannot connect, and the previous one has to
# come back.
stop_tls
dim "  TLS terminator stopped; the controller is now unreachable from the container"

# Back to the original version, so a successful rollback is observable.
if in_container /usr/local/bin/hcc-agent upgrade \
  --from "/mnt/hcc/$ARCHIVE" --sha256 "$DIGEST" \
  --connect-timeout 8s >/tmp/hcc-inst-rollback.log 2>&1
then
  bad "an upgrade that could not reconnect reported success"
else
  if grep -q "rolled back" /tmp/hcc-inst-rollback.log; then
    ok "the upgrade failed and said it rolled back"
  else
    bad "the upgrade failed without rolling back:"
    sed 's/^/      /' /tmp/hcc-inst-rollback.log | tail -8
  fi
fi
after_rollback="$(in_container /usr/local/bin/hcc-agent version | awk '{print $2}')"
if [ "$after_rollback" = "$UPGRADE_VERSION" ]; then
  ok "the binary that was running before the upgrade is running again"
else
  bad "after the rollback the installed binary is $after_rollback, expected $UPGRADE_VERSION"
fi
in_container test -f /etc/hcc-agent/credential \
  && ok "the rollback did not touch the credential" \
  || bad "the credential is gone after a failed upgrade"

# Put the controller back, and confirm the agent finds its own way home.
start_tls
await_connectivity "the agent reconnects on its own once the controller is back" \
  "connected" 120 || true

# ===========================================================================
info "== 16. uninstall keeps the credential =="

if in_container /usr/local/bin/hcc-agent uninstall >/tmp/hcc-inst-uninstall.log 2>&1; then
  ok "uninstall reported success"
else
  cat /tmp/hcc-inst-uninstall.log
  bad "uninstall failed"
fi
in_container test -e /etc/systemd/system/hcc-agent.service \
  && bad "the unit survived the uninstall" || ok "the unit is gone"
in_container test -e /usr/local/bin/hcc-agent \
  && bad "the binary survived the uninstall" || ok "the binary is gone"
in_container test -f /etc/hcc-agent/credential \
  && ok "the credential is kept, as documented" \
  || bad "the credential was removed without --purge"
if grep -q "Revoke this host's agent credential" /tmp/hcc-inst-uninstall.log; then
  ok "uninstall reminds the operator to revoke the credential"
else
  bad "uninstall did not remind the operator to revoke"
fi
if in_container systemctl is-active --quiet hcc-agent 2>/dev/null; then
  bad "the service is still running after the uninstall"
else
  ok "the service is stopped"
fi

# The controller keeps everything. An agent must not be able to delete the
# record of what it reported.
samples="$(query "SELECT count(*) FROM metric_samples_raw s JOIN metric_series e
                  ON e.id = s.series_id WHERE e.host_id = '$HOST_ID'" | head -1)"
if [ "${samples:-0}" -gt 0 ]; then
  ok "the controller still holds this host's $samples samples"
else
  bad "the host's samples disappeared"
fi

# ===========================================================================
info "== 17. uninstall --purge removes everything =="

# The binary is gone, so the purge is run from the mounted copy — which is
# also the realistic case: somebody who removed the agent and then decided to
# remove its state too.
if in_container /mnt/hcc/hcc-agent uninstall --purge >/tmp/hcc-inst-purge.log 2>&1; then
  ok "uninstall --purge reported success"
else
  cat /tmp/hcc-inst-purge.log
  bad "uninstall --purge failed"
fi
in_container test -e /etc/hcc-agent \
  && bad "/etc/hcc-agent survived --purge" || ok "/etc/hcc-agent is gone"
if in_container id hcc-agent >/dev/null 2>&1; then
  bad "the hcc-agent account survived --purge"
else
  ok "the hcc-agent account is gone"
fi

# ===========================================================================
info "== Result =="

printf '  %s passed, %s failed  (%s, %s)\n' "$PASS" "$FAIL" "$IMAGE" "$RUNTIME"
if [ "$FAIL" -gt 0 ]; then
  red "✗ the installation lifecycle is not sound on $IMAGE"
  dump_logs
  exit 1
fi
green "✓ install, collect, diagnose, upgrade, roll back and uninstall, on $IMAGE"
