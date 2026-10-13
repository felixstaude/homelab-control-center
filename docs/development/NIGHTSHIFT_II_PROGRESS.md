# Operation NIGHTSHIFT II — progress

Working state for the run that turns this repository from a working prototype
into something a Linux user can install. Factual and terse: this file records
*what was done and verified*, so work is not repeated after a context loss.

**This file is not evidence that anything passed.** Every test count below
names the command that produced it. Re-run the command rather than trusting
the number.

Branch: `feature/nightshift-ii-installation-platform`, from `main` at
`093ce32`.

---

## The plan, and where it came from

[`docs/plans/simple-installation-and-agent-distribution.md`](../plans/simple-installation-and-agent-distribution.md)
is a complete design for exactly this mission, written and argued over before
any of it was built. Its §7 work breakdown is the plan; this run implements it
rather than re-deciding it. Where implementation contradicts the design, the
progress entry says so and the design document is amended — a design that
silently diverges from the code is worse than no design.

| # | Work | Phase | Status |
| --- | --- | --- | --- |
| 1 | Release workflow: cross-compile, `checksums.txt`, SBOM, attestation | B | ✅ `db6895f` |
| 2 | Agent `install` / `uninstall` / `--dry-run` | C | ✅ |
| 3 | Credential prompt on `/dev/tty`, `--token-file`, `--non-interactive` | C | ✅ |
| 4 | Distribution matrix in CI: install, start, connect, collect, uninstall | G | ✅ |
| 5 | Controller image, non-root, auto-migrate entrypoint | E | ⬜ |
| 6 | `compose.yaml`, TLS overlay, `.env.example`, generated DB secret | E | ⬜ |
| 7 | `install-manifest` endpoint + manifest shipped with the controller | D | ⬜ |
| 8 | Enrolment dialog: architecture selector, two copy buttons, the command | D | ⬜ |
| 9 | `hcc-agent doctor` | C/F | ✅ |
| 10 | `hcc-agent upgrade`, previous-binary rollback | F | ✅ |
| 11 | Backup and restore scripts, exercised in CI | F | ⬜ |
| 12 | Documented, tested version-to-version upgrade | F | ⬜ |
| 13 | `.deb` / `.rpm` | — | deferred, stated below |

---

## Preflight — complete

Run on an idle machine before any change, at `093ce32`:

| Gate | Command | Result |
| --- | --- | --- |
| Every local gate | `make check` | ✓ docs, contracts, server, web, agent |
| Backend unit | included above | 802 passed, 1005 skipped (no database) |
| Frontend unit | included above | 542 passed, 23 files |
| Go, race detector | included above | 12 packages ok |

Local toolchain: Go 1.26.9 (CI pins 1.24), Python 3.12 venv present, Node
modules present, **Podman** — no Docker on this machine, which constrains how
the Compose work in phase E can be verified locally.

---

## Phase log

### Phase A — audit — complete

No code. Read: `README.md`, the installation plan, `ROADMAP.md` Phase 10,
`SECURITY.md`, the agent's `main.go`, `config`, `credentials`, `controller`,
`buildinfo`, the committed systemd unit and its README, `deploy/compose.yaml`,
`apps/server/hcc/config.py`, `.env.example`, the agent-facing API, the
enrolment dialog, the CI workflow and the Makefile.

Findings that shaped the order of work:

1. **The design document is implementable as written.** Its §5.2 decision —
   no installation shell script; the binary is its own installer — is the
   right one and is what the agent's existing structure supports. `enroll`
   already refuses secrets in argv, already writes 0600 atomically, already
   refuses a credential file it cannot vouch for. `install` is a composition
   of parts that exist.
2. **The controller does not serve the web client.** `apps/server/hcc/app.py`
   mounts no static files, so the single-origin deployment the CORS and CSRF
   model assumes (`config.py`, `cors_origins`) has no implementation. Phase E
   has to add it, gated on a setting, or the Compose deployment needs two
   published ports and a CORS exception — which is the arrangement the
   security model says not to ship.
3. **`buildinfo` already stamps version, commit and build date**, and the
   Makefile already passes them. Phase B is packaging that, plus the two
   fields the design asks for that are already available at runtime
   (`runtime.Version()`, `GOOS/GOARCH`).
4. **The systemd unit is committed as a file and pasted by hand.** Once an
   installer writes it, two copies exist. It becomes a generated artifact with
   a drift check, like the OpenAPI snapshot and the monitoring contracts.
5. **No release has ever been published**, so no manifest can reference a real
   URL. The `install-manifest` endpoint and the install command it renders must
   degrade honestly when no release exists rather than printing a 404 download.

---

### Phase B — release pipeline — complete (`db6895f`)

`scripts/build-agent-artifacts.sh`, `verify-artifacts.sh` (33 checks plus a
19-scenario self-test of its own refusals), `check-reproducible.sh`,
`check-versions.sh`, `VERSION`, `release-prepare.yml`, an `artifacts` job in
`ci.yml`, `docs/RELEASING.md`.

Verified: `make check-artifacts`, `make check-reproducible`,
`./scripts/verify-artifacts.sh --self-test` (19/19 caught by the intended
check), `./scripts/verify-artifacts.sh --execute`.

### Phase C — installer, diagnostics, upgrade — complete

New packages: `internal/install` (preflight, transaction, rollback, unit,
uninstall, upgrade), `internal/prompt` (`/dev/tty`, echo off),
`internal/doctor`. New subcommands: `install`, `uninstall`, `upgrade`,
`doctor`.

Decisions taken, each against an alternative:

1. **The unit has exactly one copy in the repository**, embedded in the
   binary at `internal/install/hcc-agent.service`. `deploy/hcc-agent.service`
   was deleted rather than kept as a documented copy, because two copies
   drift and the one people read would be the stale one.
   `install --dry-run` prints the embedded bytes verbatim, and a test asserts
   the deleted path has not come back.
2. **Rollback never removes the credential.** A one-time credential has been
   spent to obtain it. A surviving credential also puts the next `install`
   into `ModeIncomplete`, which finishes the installation *without* enrolling
   again — so the remedy for a half-finished install does not cost a second
   credential.
3. **A correct installation that has not connected is not rolled back.** The
   fault is on the network; removing correct files would be the wrong answer
   to somebody's firewall.
4. **"Installed" means connected**, proved by the controller's configuration
   arriving on disk — not by `systemctl is-active`. The installer does not
   open a WebSocket of its own, because doing so would supersede the
   service's connection and break what it was checking.
5. **`--dry-run` reports preflight problems and still prints the plan.** The
   point of the flag is to read what would happen *before* using sudo.

### Phase G (partial) — the installation matrix — complete

`scripts/install-matrix.sh`: a privileged container with systemd as pid 1, a
real controller against real PostgreSQL, a real internal CA, a real TLS
terminator (`scripts/tls-terminator.py`), and the release artifacts built by
the real build script. 61 assertions, 17 stages. Wired into `ci.yml` as a
3-way matrix (Debian 13, Ubuntu 24.04, Fedora 42) with `fail-fast: false`.

**Measured locally**: `61 passed, 0 failed` on `debian:13` and on `fedora:42`,
with Podman. Ubuntu 24.04 is in the CI matrix and has **not** been run
locally.

#### Three defects it found

1. **`ProcSubset=pid` in the committed unit.** Found by reading, before the
   matrix ran. It mounts /proc with `subset=pid`, which hides `/proc/stat`,
   `/proc/meminfo`, `/proc/loadavg` and `/proc/uptime` — the entire data
   source for every module. The service would have started, connected,
   reported itself healthy and collected nothing. Nothing had caught it
   because the unit had never been *started*: `systemd-analyze verify` parses
   and does not execute. Removed, with the reason in the file, and the matrix
   now asserts a real sample arrives.
2. **`credentials.Load` refused root reading a service-owned credential.**
   Found by the matrix, on a *healthy* installation. The installer writes the
   credential as root and hands it to `hcc-agent`, so a root-run `doctor` or
   `status` was told the agent would refuse its own working credential. For
   root the owner check cannot prevent what it exists to prevent — the mode
   and directory checks already answer "could an unprivileged user have
   substituted this?" — so root now accepts any owner. This affected shipped
   `hcc-agent status` too, not only the new code.
3. **A test that passed vacuously.** The matrix's rollback stage pointed at an
   archive path that did not exist, so the upgrade never ran — and the
   assertion after it ("the previous binary is running again") passed because
   nothing had changed. Caught because the stage asserts on the specific
   message rather than on the exit code.

---

## Deferred, with the reason

* **`.deb` / `.rpm` packaging** (design §7 item 13). Needs a signing key, a
  repository to serve it from, and per-distribution packaging; the design
  itself calls it the right answer eventually and the wrong first answer.
  Roadmap 10.6 keeps it.
* **Multi-arch controller image.** The agent is cross-compiled; a Python
  image for arm64 needs either a QEMU build or an arm64 runner, and the
  controller runs on the one machine an operator chooses rather than on every
  host. Stated in the deployment documentation rather than implied.

---

## Security findings

None yet.

---

## Known limitations

None recorded yet.
