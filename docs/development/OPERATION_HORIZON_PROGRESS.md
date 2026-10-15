# Operation HORIZON — progress

Working state for the run that hardens the update system, adds the real Linux
monitoring modules, and makes the product something an operator can launch and
use. Factual and terse: this file records *what was done and verified*, so work
is not repeated after a context loss.

**This file is not evidence that anything passed.** Every test count below
names the command that produced it. Re-run the command rather than trusting the
number.

---

## Base state, verified rather than assumed

Checked at the start of the run, on 2026-10-14:

| Question | Answer | How it was checked |
| --- | --- | --- |
| Is Evergreen PR #9 merged? | **No — still DRAFT** | `gh pr list --state all` |
| Is the local Evergreen head current? | Yes, `b480d5c` = `origin/feature/evergreen-update-management` | `git rev-parse` |
| How far ahead of main? | 16 commits, 0 behind | `git rev-list --left-right --count origin/main...HEAD` |
| Is Evergreen's CI green? | 13/13 repository jobs pass. `GitGuardian Security Checks` fails in 2 s — an external app, not a repository job | `gh pr checks 9` |
| Do the agent tests pass locally? | Yes, every package | `cd agents/linux && go test ./...` |

So Evergreen is **not merged**, and this branch is a documented dependent
branch on top of it:

```
main (3994787)
  └── feature/evergreen-update-management (b480d5c)   ← PR #9, draft, not merged
        └── feature/horizon-product-maturity          ← this branch
```

PR #9 is not merged by this run. If it merges first, this branch rebases onto
main cleanly because it shares Evergreen's history.

**Toolchain on the development machine:** Go 1.26.9, Node 24.21, Python 3.12
venv at `apps/server/.venv`, **Podman 5.8.7 and no Docker**. That last one
shapes Phase G: Docker monitoring is written against the Docker Engine API and
exercised locally against Podman's Docker-compatible socket, with real Docker
left to CI.

---

## Phase log

### Phase A — secure the update system — complete

Mandatory, highest priority, and the reason it is first: Evergreen shipped a
privileged installer, and an installer with a gap is worse than no installer.

#### A1 — anti-rollback — done, commit `eda8c39`

A durable monotonic floor per host in `/var/lib/hcc-agent-update/`, root-owned,
a **sibling** of the agent's state directory rather than a child (rename
permission comes from the parent, so a root-owned directory inside an
agent-owned one can be renamed away). Three versions kept apart — running,
`highest_installed`, `floor`. Rises only on a health-checked success. Absence
permissive, corruption a refusal. `allow_downgrade` does not reach below it;
`hcc-agent upgrade --allow-downgrade --version` does, and needs root.
ADR-063.

Enforced in the privileged installer (authoritative) and advisorily in the
agent (for the message in the update history). Seeded by `hcc-agent install`
and, for hosts predating it, by the privileged unit at the start of the first
update it handles — so that update is protected, not the one after.

Tests: 25 in `floor_test.go`, 7 more through the privileged path in
`handoff_test.go`, 4 in the updater. Mutation-checked: inverting the
comparison fails 3.

#### A2 — privileged handoff review — done, commit `eda8c39`

**One real privilege escalation found**, plus three hardenings.

*The substitution race.* The upgrade read the archive twice — hash, then
extract — and both reads opened the path. That path is in `/var/lib/hcc-agent`,
which belongs to the unprivileged agent account by design, so the two reads
could see different bytes: digest verified against an archive that is then
not the one installed. Agent account → root code execution.

The fix found its own defect: a single file descriptor was the obvious
answer, and the test that substituted the archive with `os.WriteFile` went
straight through it, because `WriteFile` truncates **in place** and a
descriptor follows the inode. A descriptor closes the rename form only. The
privileged side now copies the archive into its own root-owned directory
first, bounded by the size in the signed metadata, and installs from that;
the descriptor stays as a second layer. ADR-064.

Also: the archive must be a *direct child* of the state directory (a prefix
test is satisfied by a path through a symlinked subdirectory the agent can
create); replay protection by instruction id (otherwise a compromised agent
account can drive a root-owned service into a restart loop); a state-directory
sanity check.

#### A3 — release signing integration — done

`scripts/prepare-release.sh`: build → verify artifacts → derive the manifest →
sign → verify the signature, in one command, with every missing prerequisite
a refusal *before* anything is built. Publishes nothing.

`make release-candidate` runs the whole pipeline with a keypair generated for
the run and shredded at the end, and CI now does it on every pull request. An
ephemeral run writes `agent-releases.json.ephemeral` beside the manifest, so a
future publishing step can refuse on a file rather than on somebody
remembering. Verified: the six refusals (no key, leading `v`, non-semver, no
protocol versions, a 644 key, both key modes) each fire.

#### A4 — key lifecycle — done

A `.sig` may now carry several signatures over the same bytes; a verifier
accepts if any one is by a trusted key. This exists for one specific trap: an
agent built before a rotation trusts only the old key, so a manifest signed
with only the new key strands exactly the fleet the rotation is for —
including the update that would have taught it the new key.

Both languages, kept in step by 8 new contract vectors (24 total). Precedence
when nothing verifies is worst-first: a *trusted* key whose signature fails is
tampering and outranks an untrusted one, which is routine. Mutation-checked:
an implementation that stops at the first entry fails 4 vectors.

`sign-release.py sign --add` signs alongside what is already there and
verifies that the existing signature survived. The five-step rotation
procedure is in `docs/RELEASING.md`.

### Phases D and E — disk, filesystem and network monitoring — complete

Three modules: `filesystem`, `diskio`, `network`. Commit `ad27212`, with the
UI in a later commit.

Capacity and throughput are **separate modules** because they have different
natural rates (free space moves over days, throughput over seconds),
different cardinality (per mount against per device), and an operator who
wants one should not have to pay for the other.

**Nothing is summed anywhere**, deliberately. Stacked storage reports the same
I/O at every layer and a container's packet is counted three times; there is
no aggregate metric, so there is nothing to be wrong.

Found by running it rather than by reasoning:

1. Against this laptop, `/` and `/home` reported byte-identical figures —
   btrfs subvolumes get their own anonymous device numbers, so a `st_dev`
   check misses them. A second test on the source device catches it.
2. A fixture written the way the kernel renders an error-driven read-only
   remount showed the check reading the wrong field. Both the per-mount and
   the super options are checked now, with an `errors=remount-ro` regression
   test — a substring match reports nearly every Linux root as read-only.
3. `metric_series.unit` was `varchar(16)` and `operations_per_second` is 21.
   See the defect table.

ADR-065 records why the agent divides by its own measured elapsed time rather
than shipping counters or trusting the configured interval.

### Phase K — the demo environment — complete

`make demo`. Commit `592f675`.

Isolated PostgreSQL container on 55433, controller on 8900, state in
`.demo/`, a real agent built from source reporting this machine's own `/proc`
and `/sys`, seven modules enabled, a generated administrator password. Both
ports loopback-only with no flag to change that.

**No Compose**, which is why it works on this machine at all: the production
stack is verified against Docker Compose only (podman-compose hangs on its
`depends_on: service_completed_successfully`) and this machine has Podman.

Verified on Fedora with Podman 5.8.7: 986 samples across 65 series within
30 s of a cold start; `demo-down` then `up` resumed the same data;
`demo-clean` left no container, volume or directory.

### Phases F, G and H — sensors, containers, services — complete

`sensors` (commit `80a54d2`), `docker` (`00688a1`), `systemd` (`e4f6aa6`).

**`sensors`** reads `/sys/class/hwmon` and deliberately not
`/sys/class/thermal`: on this machine `thermal_zone0` *is* `hwmon5`'s device,
and `thermal_zone1` duplicates `coretemp`'s package sensor through a
different device, so no path comparison catches it. Identity is chip + label,
never the hwmon number (probe order, changes between boots). Running it here
showed two NVMe drives both reporting `nvme`/`Composite` — the device
basename disambiguates.

**`docker`** is the only module with `requires_privileges`. The socket is
root-equivalent, so it is granted locally by `HCC_AGENT_DOCKER_SOCKET` and
the controller can only decide whether to *use* a capability the host has
given. Read-only is structural: one request builder, GET only, asserted by a
test against a real HTTP server on a real unix socket.

**`systemd`** is the only module that runs a command, within ADR-021's eight
rules, which existed before any module needed them. `--output=json` is a
structured interface; an older systemd is a refusal naming the version.
`systemctl show` is asked for six named properties, not the ~200 it prints by
default — `Environment` can hold secrets. Running it against a deliberately
absent unit found that `systemctl show` answers for units it has never heard
of, so `LoadState` is checked.

### Phase L — end-to-end coverage for the new modules — partly done

`scripts/monitoring-e2e.sh` §17 and §18, run by CI on every change. **87
checks pass** locally (was 60).

Every assertion cross-checks the agent against the machine it runs on rather
than against itself:

- the agent's size for `/` against `statfs` directly (3,723 GiB, within one
  block)
- `used + available ≤ total` for every filesystem
- no pseudo-filesystem or tmpfs reported as storage
- every `diskio` device has a `/sys/block` entry — a partition here would
  mean the same I/O counted twice
- every `network` interface exists in `/sys/class/net`, and loopback does not
- the failed-unit count against `systemctl --failed`

§18 is separate because of the mission's rule about hardware: each of
`sensors`, `systemd` and `docker` asserts **either** real readings **or** a
correctly-reported unavailability, never silence. A runner with no sensors
produces "this host exposes no hwmon temperature sensors, and none were
invented" — a result, not a skip.

Three pinned counts fired and were replaced with reads of the registry
rather than updated numbers: a literal asserts that somebody remembered to
edit a second file, which is how one came to say 5 while the registry had 11.

### Phase B — staged rollouts — the engine exists

Evergreen created `update_rollouts` and `update_rollout_members` — a canary
batch, a batch size, a failure tolerance, an offline deadline — and nothing
that drove one. `hcc/services/rollouts.py` is the engine, with migration
0012 adding the columns it needs.

**"The process restarted" is not "the upgrade succeeded".** The job concludes
when the agent reconnects reporting the target version, which is a real check
and a narrow one — an agent that comes up, reports its version and crashes a
minute later satisfies it completely. So the rollout does not advance on the
job. It advances on a **member health check** taken after the install, over a
settling period, asking four separate questions: is the host connected *now*,
is it reporting the target version, has it acknowledged its configuration at
the current revision, and has it delivered telemetry since the install. The
third is the one that catches a half-working binary.

**Three outcomes, not two.** A host offline past its deadline is *skipped*,
and a skip does not count against the failure tolerance — a powered-off NAS
says nothing about the release, and stopping a fleet rollout over one would
be a gate firing for an unrelated reason. Same for a host already running the
target version.

**A controller restart is a non-event.** There is no in-memory rollout state:
a pass reads rows, decides, writes rows. A test constructs a *fresh service
object* between passes, which is what a restarted controller has, and asserts
it reaches the same conclusion.

Resuming a rollout that the failure gate stopped **pauses it again** unless
the administrator passes `override_failures`, which is recorded on the
rollout rather than implied by the resume — "resumed after a failure" and
"resumed after an administrator paused it" must not be able to look alike
afterwards.

A third background task, at 30 s rather than the policy pass's 300 s: a
rollout is something somebody is watching, and five minutes between a canary
going healthy and the first batch starting is a product that feels broken.

30 integration tests, all against a real database. Two engine defects found
by them: a batch where every member was skipped sat on `running` for an
interval with nothing left to do, and the "already running the target
version" skip reason said "this host is already pinned" — true of the
question the planner was asked and nonsense as an explanation.

**Not yet done in this phase:** the frontend. The API is complete and tested;
the Updates page does not render rollouts.

### Phase C — safe controller upgrades — done

`scripts/upgrade-controller.sh`, plus a read-only
`GET /api/v1/controller/upgrade-readiness`.

**The controller guides; the command acts.** Replacing the controller means
replacing the container the interface is served from, and for the
application to do that to itself it would need the Docker socket — root on
the host. There is no remote-command API for the same reason. A test asserts
`POST`, `PUT`, `PATCH` and `DELETE` on the readiness endpoint all answer
405.

**The backup is verified by restoring it**, into a scratch database in the
same PostgreSQL instance, with its table count compared against the live
one. A dump whose checksum matches is *intact*; that is not the same as one
that *restores*, and the difference is the whole value of the step. The
upgrade does not start if the restore fails or comes back short.

**No automatic database rollback**, stated rather than implied: an Alembic
downgrade that drops a column is data loss performed by a script at the
worst possible moment, and some of this project's own downgrades refuse
rather than destroy. Recovery is the old image *and* the backup, in that
order, and the script prints both commands with the real path in them.

The readiness endpoint names the one direction a backup cannot save you
from: **a database ahead of the image**. It reports zero pending migrations,
because the older build has never heard of the revision to be behind it, and
looks entirely current. `deployment-e2e.sh` §11 sets a future revision and
asserts it is caught, that the advice says what to do, and that the
controller keeps serving while reporting it — a controller that fell over
would take the interface down at the moment somebody needs it to explain
what happened.

One bug found while writing it: `schema_is_ahead` asked `applied_revisions`,
which reports an unknown head *as the head alone* — deliberately, so a
blocker can name it — which made the unknown revision look applied. There is
now a `knows_revision` helper that asks the history directly.

### Phase J — the host detail page — in progress

`resource-panels.tsx`: filesystems, disks and interfaces as **tables**, not
tiles. A laptop with three filesystems, four devices and two interfaces
produces 55 labelled series, and 55 tiles is a wall with the wanted number
somewhere in it. Scalars stay as tiles.

Verified in a real browser against the running demo, not only in jsdom —
which is how the duplicate `Used` column heading was found.

---

## Decisions

Recorded as they are taken, with the alternative that was rejected.

1. **The floor is a sibling directory, not a sticky-bit subdirectory.** The
   alternative keeps one directory and depends on `+t` on `/var/lib/hcc-agent`
   — a subtle property that one `chmod` undoes silently. A sibling needs write
   access to `/var/lib`, which the agent does not have, and the argument fits
   in a sentence.
2. **The floor's uid-0 requirement is not relaxable from outside
   `internal/install`.** That makes it untestable from the `updater` package's
   external tests, so `updater.Options` gained an injected `Floor` reader
   instead. Exporting a toggle would have put a production-reachable "turn the
   check off" in the API, which is worse than one more injection point in a
   struct that already has five.
3. **Multi-signature rather than a sequencing rule.** The documented
   alternative — sign with the old key until the fleet has rolled over, then
   switch — works and depends entirely on an operator getting the order right
   once, with a stranded fleet as the cost of getting it wrong. ~150 lines in
   two languages removes the trap. Not a PKI: no certificates, no chains, no
   expiry, and the bound is 8 signatures.

---

## Defects found

| # | Where | Severity | Status |
| --- | --- | --- | --- |
| 1 | `install.Upgrader` read the archive twice by path, from a directory the unprivileged agent owns | **privilege escalation** | fixed, `eda8c39` |
| 2 | No anti-rollback: a compromised controller could install any older signed release, and `allow_downgrade` made it one flag away | high | fixed, `eda8c39` |
| 3 | `resolveArchive` used a prefix test, satisfied by a path through a symlinked subdirectory the agent can create | medium (arbitrary-file read as root) | fixed, `eda8c39` |
| 4 | No replay protection on the handoff: a compromised agent account could re-apply the last instruction indefinitely | low (self-DoS, root-driven) | fixed, `eda8c39` |
| 5 | `metric_series.unit` was `varchar(16)`; `operations_per_second` is 21 characters. Every unit test passed (no database), the integration suite passed (no test stored one), and the first real agent reporting real disk throughput raised `StringDataRightTruncationError` inside the WebSocket handler — which the agent saw only as `protocol_error` and retried for ever | high (silent, total telemetry loss for a module) | fixed, `592f675`; guard in `tests/test_metric_models.py` |
| 6 | `demo.sh` printed a closing message containing backticks inside a double-quoted string, which **ran the demo again** | low | fixed, `592f675` |
| 7 | `demo.sh` waited on `metric_samples`, which does not exist — the raw tier is `metric_samples_raw` — so a working demo reported "no telemetry" | low | fixed, `592f675` |
| 8 | `== 15 ==` in `update-e2e.sh` asserted a plan outcome immediately after a controller restart, so a slow agent reconnect made it `unknown_version`. A CI flake | low (test) | fixed, `f9ae09d` |
| 9 | The filesystem table headed two different columns `Used` | low (a11y) | fixed, `0ebae9c` |
| 10 | `demo.sh up --rebuild` killed the controller without waiting, then refused its own port as "in use by something that is not this demo" | low | fixed, `b4357c2` |
| 11 | The demo leaked `HCC_AGENT_*` into the controller, which refuses unknown `HCC_` variables — a good rule tripped by the demo's own sloppiness | low | fixed, `b4357c2` |
| 12 | A demo database created before a module existed silently refuses it, because an applied migration is never re-run; surfaced as HTTP 500 with `CheckViolationError` | low (development only) | guarded, `b4357c2` |
| 13 | `systemctl show` answers for units it has never heard of, so a typo in the watch list became a series permanently reporting a stopped service that does not exist, restart count zero | medium (fabricated measurement) | fixed, `e4f6aa6` |
| 14 | `monitoring-e2e.sh` used a relative `$VENV` after a `cd`, producing `command not found` reported against a line that looked like a comment | low (test) | fixed in this commit |

---

## Remaining work

Phases B–P. See the mission brief.

---

## Next task

Phase B — real staged rollouts. It is the mission's Priority 1 and the
largest remaining gap: Evergreen evaluates every eligible host together, and
the replacement needs canary selection, batches, maintenance windows,
pause/resume/cancel, durable progress across a controller restart, and health
criteria that do not equate "the process restarted" with "the upgrade
succeeded".
