# Operation NIGHTSHIFT II — progress

Working state for the run that turns this repository from a working prototype
into something a Linux user can install. Factual and terse: this file records
*what was done and verified*, so work is not repeated after a context loss.

**This file is not evidence that anything passed.** Every test count below
names the command that produced it. Re-run the command rather than trusting
the number.

Branch: `feature/nightshift-ii-installation-platform`, from `main` at
`093ce32`. Draft PR: #7.

---

## The plan, and where it came from

[`docs/plans/simple-installation-and-agent-distribution.md`](../plans/simple-installation-and-agent-distribution.md)
is a complete design for exactly this mission, written and argued over before
any of it was built. Its §7 work breakdown is the plan; this run implements it
rather than re-deciding it. Where implementation contradicts the design, the
entry below says so.

| # | Work | Phase | Status |
| --- | --- | --- | --- |
| 1 | Release workflow: cross-compile, `checksums.txt`, SBOM, attestation | B | ✅ `db6895f` |
| 2 | Agent `install` / `uninstall` / `--dry-run` | C | ✅ `286711e` |
| 3 | Credential prompt on `/dev/tty`, `--token-file`, `--non-interactive` | C | ✅ `286711e` |
| 4 | Distribution matrix in CI: install, start, connect, collect, uninstall | G | ✅ `286711e` |
| 5 | Controller image, non-root, auto-migrate entrypoint | E | ✅ `ff0aa1b` |
| 6 | `compose.yaml`, TLS overlay, `.env.example`, generated DB secret | E | ✅ `ff0aa1b` |
| 7 | `install-manifest` endpoint + manifest shipped with the controller | D | ✅ `bad01e5` |
| 8 | Enrolment dialog: architecture selector, two copy buttons, the command | D | ✅ `bad01e5` |
| 9 | `hcc-agent doctor` | C/F | ✅ `286711e` |
| 10 | `hcc-agent upgrade`, previous-binary rollback | F | ✅ `286711e` |
| 11 | Backup and restore scripts, exercised in CI | F | ✅ `ff0aa1b` |
| 12 | Documented, tested version-to-version upgrade | F | 🟡 mechanism tested, no second version exists |
| 13 | `.deb` / `.rpm` | — | deferred, stated below |

Two amendments to the design, recorded rather than taken quietly:

1. **The endpoint is `/api/v1/agent-releases`, not
   `/api/v1/agents/install-manifest`** (§4.3). Everything under
   `/api/v1/agents` authenticates a per-host agent credential and refuses a
   browser session; one session-gated exception there would make that
   module's own invariant false for one path.
2. **`hcc-agent upgrade` does not call that endpoint** (§5.7). It takes
   `--from` and `--sha256`, which the administrator copies from the same page
   as everything else. One caller, one credential type, and the agent needs an
   HTTP client for nothing but enrolment and the socket.

---

## Preflight — complete

Run on an idle machine before any change, at `093ce32`:

| Gate | Command | Result |
| --- | --- | --- |
| Every local gate | `make check` | ✓ docs, contracts, server, web, agent |
| Backend unit | included above | 802 passed, 1005 skipped (no database) |
| Frontend unit | included above | 542 passed, 23 files |
| Go, race detector | included above | 12 packages ok |

Local toolchain: Go 1.26.9 (CI pins 1.24), Python 3.12 venv, Node modules
present, **Podman** — no Docker on this machine, which turned out to matter.

---

## Phase log

### Phase A — audit — complete

No code. Read the plan, `ROADMAP.md` Phase 10, `SECURITY.md`, the agent's
`main.go`, `config`, `credentials`, `controller`, `buildinfo`, the committed
systemd unit and its README, `deploy/compose.yaml`, `hcc/config.py`, the
agent-facing API, the enrolment dialog, the CI workflow and the Makefile.

Four findings shaped the order of work:

1. **The design is implementable as written.** Its §5.2 decision — no
   installation shell script; the binary is its own installer — is what the
   agent's existing structure supports.
2. **The controller did not serve the web client**, so the single-origin
   deployment the CORS and CSRF model assumes had no implementation.
3. **`buildinfo` already stamps version, commit and build date**, so phase B
   is packaging rather than new work.
4. **No release has ever been published**, so no manifest can reference a real
   URL and the install command must degrade honestly.

### Phase B — release pipeline — complete (`db6895f`)

`scripts/build-agent-artifacts.sh`, `verify-artifacts.sh` (33 checks plus a
19-scenario self-test of its own refusals), `check-reproducible.sh`,
`check-versions.sh`, `VERSION`, `release-prepare.yml`, an `artifacts` job in
`ci.yml`, `docs/RELEASING.md`.

Verified: `make check-artifacts`, `make check-reproducible`,
`verify-artifacts.sh --self-test` (19/19 caught by the intended check),
`--execute` (amd64 natively; arm64 and arm under qemu in CI).

### Phase C — installer, diagnostics, upgrade — complete (`286711e`)

New packages: `internal/install`, `internal/prompt`, `internal/doctor`. New
subcommands: `install`, `uninstall`, `upgrade`, `doctor`.

Decisions taken, each against an alternative:

1. **The unit has exactly one copy in the repository**, embedded in the
   binary. `deploy/hcc-agent.service` was deleted rather than kept as a
   documented copy: two copies drift and the one people read would be the
   stale one. `install --dry-run` prints the embedded bytes verbatim.
2. **Rollback never removes the credential.** A one-time credential was spent
   to obtain it, and a surviving credential puts the next `install` into
   `ModeIncomplete`, which finishes the job *without* enrolling again.
3. **A correct installation that has not connected is not rolled back.** The
   fault is the network; removing correct files is the wrong answer to
   somebody's firewall.
4. **"Installed" means connected**, proved by the controller's configuration
   arriving on disk — not by `systemctl is-active`, and not by a second
   WebSocket of the installer's own, which would supersede the service's.
5. **`--dry-run` reports preflight problems and still prints the plan.** The
   point of the flag is to read what would happen *before* using sudo.

### Phase D — install manifest and enrolment UX — complete (`bad01e5`)

`hcc/agents/releases.py`, `GET /api/v1/agent-releases`,
`scripts/build-release-manifest.py`, an architecture selector, the four-line
install command with its own copy button, and a live expiry countdown.

The countdown counts from the server's `expires_in_seconds`, not from
`expires_at` minus the browser's clock, and is derived from elapsed wall time
rather than decremented per tick — a throttled background tab would otherwise
claim time the operator does not have.

### Phase E — controller deployment — complete (`ff0aa1b`, `8d46e07`)

`hcc/web.py`, `Dockerfile`, `deploy/docker-entrypoint.sh`,
`deploy/production/{compose,compose.build,compose.tls}.yaml`, `Caddyfile`,
`.env.example`, `scripts/{deployment-e2e,backup,restore}.sh`,
`docs/DEPLOYMENT.md`.

### Phase F — backup and restore — complete (`ff0aa1b`)

`scripts/backup.sh` writes a `pg_dump -Fc` and a manifest stating what the
backup does and does not contain. `scripts/restore.sh` verifies the digest
before dropping anything, refuses a populated target without `--force`, and
has a `--dry-run`. Both have a `--container` mode, because `pg_dump` is
frequently not installed on the machine running the controller — the database
is in a container and so is its client.

### Phase G — CI — complete

Three new jobs in `ci.yml`: `artifacts`, `deployment`, and `install-matrix`
as a 3-way matrix (Debian 13, Ubuntu 24.04, Fedora 42) with
`fail-fast: false`. `release-prepare.yml` is a fourth workflow that builds,
verifies and publishes nothing.

### Phase H — security review — complete

[`docs/security/2026-10-14-installation-platform-review.md`](../security/2026-10-14-installation-platform-review.md).
Six findings: four fixed, two accepted with stated reasoning.

### Phase J — documentation — complete (`fcc0fa8`, `6142d25`, `47cf70d`)

Six ADRs (053–058), the design document marked implemented with its two
amendments, `README.md` restructured around what somebody arriving actually
wants (install it / develop on it / connect a host), `docs/DEPLOYMENT.md`,
`docs/RELEASING.md`, and the agent README's third copy of the systemd unit
removed — see defect 14.

### Phase I — performance — complete (`a41d26e`)

Unblocked once CI went fully green at `8a8b6aa`, which is the precondition the
brief sets. Measured first, in the one place this run created and nobody had
counted: the control plane now serves the web client, and before Phase E it
served no client at all.

`scripts/measure-transfer.py` and `make measure-transfer`, with the figures in
[`docs/PERFORMANCE.md §5`](../PERFORMANCE.md). It found that the SPA was served
**uncompressed** — a first load was 606 kB where 181 kB would do — and that the
chart payloads are the larger recurring cost at 237 kB per host page against
24 kB gzipped. Both are now compressed; see defect 16 and ADR-059.

The bundle itself was left alone, and that is a result rather than a gap. 570 kB
for an entry chunk holding React, the router, the query client and the icon set
is unremarkable, the chart library is already lazy-split, and 181 kB over a LAN
is not worth restructuring an application for.

---

## Defects found, and how

Sixteen, listed because the *how* is the useful part.

### Found by reading, before any test existed

1. **`ProcSubset=pid` in the systemd unit.** It mounts `/proc` with
   `subset=pid`, hiding `/proc/stat`, `/proc/meminfo`, `/proc/loadavg` and
   `/proc/uptime` — the entire data source for every module. The service
   would have started, connected, reported itself healthy and collected
   nothing. Nothing had caught it because nothing had ever *started* the unit;
   `systemd-analyze verify` does not execute. High severity, availability.

### Found by the installation matrix

2. **`credentials.Load` refused root reading a service-owned credential**, on
   a *healthy* installation. The installer writes the credential as root and
   hands it to `hcc-agent`, so root-run `doctor` and `status` reported a fault
   on something correct. This affected shipped `hcc-agent status` too.
3. **A matrix stage that passed vacuously.** The rollback scenario pointed at
   an archive path that did not exist, so the upgrade never ran — and the
   assertion after it passed because nothing had changed. Caught only because
   the stage asserts on the specific message rather than the exit code.

### Found by CI's deployment job

4. **`docker compose exec hcc hcc bootstrap-admin` could not reach the
   database.** The entrypoint assembled the URL and exported it before
   exec-ing the server; `compose exec` does not run an entrypoint. The one
   step ADR-033 makes mandatory was the one that did not work. The assembly
   moved into `hcc/config.py`.
5. **The scenario's own cleanup failed after passing.** Under rootful Docker
   the generator container writes a root-owned file into the bind mount, which
   an unprivileged runner cannot delete — so a 44/44 pass exited 1.

### Found by running it on this machine

6. **SELinux.** The secret directory was mounted `:ro`, and on an enforcing
   host — Fedora, RHEL, CentOS Stream, Rocky, Alma — the container gets
   "Permission denied" on a file that is plainly 0644. `:ro,z` fixes it.
   GitHub's runners do not enforce SELinux, so CI would never have found this.
7. **`log: not found` on container start.** Removing the URL assembly from the
   entrypoint took `log` and `fail` with it. `sh -n` parses and does not
   resolve calls, so the syntax check was green.
8. **`container_name: hcc-postgres` collided with the development stack**, so
   the deployment scenario's `compose down -v --remove-orphans` removed the
   developer's own database container. The volume survived. Fixed by removing
   every fixed container name and the `--remove-orphans`.
9. **`pg_restore --clean` does not work on this schema.** The metric tables
   are partitioned and `--clean` emits a `DROP CONSTRAINT` per partition which
   PostgreSQL refuses. Six errors and a database left neither old nor new.
   `restore.sh` drops the public schema instead.

### Found by the security review

10. **Manifest values reached a root-shell command unvalidated.** `url` and
    `archive` are interpolated into the install command; a `;`, a backtick or
    a `'` is injection into a shell the operator pastes into as root. Now
    refused rather than escaped.
11. **The installer followed a symlink at `/etc/hcc-agent`**, so `MkdirAll`,
    `Chmod` and `Chown` would have been applied to whatever it pointed at.
    Low reachability — planting it needs root — and fixed because the
    installer should not be a tool for it.

### Found by CI, pre-existing

12. **`reliability-e2e.sh` reported "68 rows but only 72 distinct keys"** — an
    arithmetic impossibility that reads as data corruption. It read two counts
    in two queries while the agent was still collecting. One query now.
    Nothing to do with this run's changes; it would have kept failing
    intermittently.

### Found by writing the documentation

14. **`agents/linux/README.md` carried a third copy of the systemd unit**, and
    that copy had three defects: `ProtectSystem=strict` with no
    `ReadWritePaths` (the agent cannot write its own config),
    `Restart=always` with no start limit, and `After=network-online.target`
    with no matching `Wants=`, which does nothing. Its own text called it a
    "short reference" and named another file as authoritative — which is how a
    stale copy survives review, because nobody reads a reference for
    correctness. Removed, and
    `TestNoDocumentCarriesASecondCopyOfTheUnit` makes the next one a test
    failure. The same README documented only `run`, while the binary had grown
    four more subcommands.

### Found by measuring

16. **The control plane served the web client uncompressed.** Phase E made it
    responsible for serving the SPA; it had no compression middleware of any
    kind, so a first load of the sign-in page was 606 kB where 181 kB would
    do — 3.35x, on the one transfer every user makes before seeing anything.
    The chart payloads turned out to be worse: 237 kB for a host page's four
    charts against 24 kB gzipped, and unlike the bundle that is re-fetched
    every page view rather than cached for a year. The TLS overlay's Caddy
    does `encode zstd gzip`, so the recommended deployment was unaffected and
    the default one — which has no proxy — was not. Fixed as an allow-list
    rather than an application-wide middleware, because BREACH makes
    `Content-Encoding` a security decision: ADR-059.

### Fixed in passing

15. **`make help` hid four targets.** Its grep pattern excluded digits, so
    `e2e`, `monitoring-e2e`, `reliability-e2e` and `deployment-e2e` all had
    descriptions and were invisible.

---

## Measured results

Each command, and what it reported. Re-run rather than trust.

| Command | Result |
| --- | --- |
| `make check` | ✓ all gates, 932 backend unit, 561 frontend |
| `make test-integration` | 1031 passed |
| `go test -race ./...` (agents/linux) | ✓ 14 packages |
| `E2E_REQUIRE_AGENT=1 make e2e` | 154 passed |
| `./scripts/reliability-e2e.sh` | 24 passed |
| `make check-artifacts` | 33 checks + 19/19 self-test refusals |
| `make check-reproducible` | byte-identical |
| `./scripts/install-matrix.sh --image debian:13` | 61 passed, 0 failed |
| `./scripts/install-matrix.sh --image fedora:42` | 61 passed, 0 failed |
| `./scripts/deployment-e2e.sh` (CI, Docker) | 44 passed, 0 failed |
| `systemd-analyze verify` on the embedded unit | ✓ |
| `make measure-transfer` | first load 606 kB → 181 kB |

CI at `fcc0fa8`: completed success, every job green.

---

## Deferred, with the reason

* **`.deb` / `.rpm` packaging** (design §7 item 13). Needs a signing key, a
  repository to serve it from, and per-distribution packaging; the design
  itself calls it the right answer eventually and the wrong first answer.
  Roadmap 10.6 keeps it.
* **Multi-arch controller image.** The agent is cross-compiled; a Python
  image for arm64 needs a QEMU build or an arm64 runner, and the controller
  runs on the one machine an operator chooses. Stated in
  `docs/DEPLOYMENT.md` §8 rather than implied.
* **A version-to-version upgrade.** There is only one version. The
  *mechanism* — migrations on start, a second start being a no-op, data
  surviving a restart — is verified by `deployment-e2e.sh`.
* **Publishing anything.** Out of scope by instruction, and the code degrades
  honestly without it: the Compose file has a build overlay and the enrolment
  dialog shows how to build the agent rather than a download that would 404.

---

## Known limitations

* **`compose.tls.yaml` has never obtained a certificate.** Caddy's issuance
  needs a publicly resolvable name. The Caddyfile's directives are checked;
  nothing has run it.
* **podman-compose cannot run the production stack.** Version 1.6.0 hangs on
  its `depends_on` conditions. `deploy/compose.yaml`, the development
  database, still works with both. `deployment-e2e.sh` skips rather than
  hanging.
* **No ARM hardware.** `arm64` and `arm` are built every commit and executed
  under `qemu-user-static`. The manifest's `tier` field says which claims are
  which.
* **No Alpine in the installation matrix.** busybox `adduser` is handled in
  the installer and is not tested.
* **`systemd-analyze security`** needs the unit installed on the host being
  scored and has not been run.
* **Ubuntu 24.04 has not been run locally**, only in CI. Debian 13 and
  Fedora 42 have been run in both.
