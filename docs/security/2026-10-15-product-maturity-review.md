# Security review: product maturity — 2026-10-15

A focused review of what Operation Horizon added: the agent's anti-rollback
floor, multi-signature release verification, six new monitoring collectors
(two of which reach outside `/proc`), staged rollouts, the controller-upgrade
surface, collector health reporting, and the demo environment.

**Scope is the new attack surface.** [`SECURITY.md`](../SECURITY.md) is the
baseline and was not re-reviewed, nor were the
[installation platform](2026-10-14-installation-platform-review.md) or
[update management](2026-10-14-update-management-review.md) reviews repeated.
The question here is whether anything added since `b480d5c` weakens the model,
and whether the new code has problems of its own.

**No secret values appear in this document.** The release keypairs referenced
are generated per run into temporary directories and removed on every exit
path. The demo's administrator password is generated per run and printed to
the operator's terminal; it is not in the repository and not in this file.

---

## 1. Summary

This run added two things that increase privilege and four that increase
*reach*:

- **Privilege**: a root-owned version floor the agent cannot raise, and a
  Docker socket the agent may be granted.
- **Reach**: staged rollouts, which turn one administrator's click into
  software installed on an entire fleet in sequence; a controller-upgrade
  path; six collectors that read more of the host than before; and a health
  surface that renders host-supplied text in a browser.

Nine findings. Four are fixed, four are accepted with stated reasoning, and
one is a residual limit of the design that is stated rather than mitigated.

| # | Finding | Severity | Status |
| --- | --- | --- | --- |
| 1 | `install.Upgrader` read the archive twice by path from an agent-owned directory | **Privilege escalation** | Fixed, `eda8c39` |
| 2 | `resolveArchive` used a prefix test satisfied through a symlinked subdirectory | Medium (arbitrary root read) | Fixed, `eda8c39` |
| 3 | No anti-rollback: a compromised controller could install any older signed release | High | Fixed, `eda8c39` |
| 4 | No replay protection on the privileged handoff | Low (self-DoS, root-driven) | Fixed, `eda8c39` |
| 5 | A reconnection erased the host's reported version, so the planner saw `unknown_version` | Low (availability of correct planning) | Fixed, `2ab478e` |
| 6 | Host-supplied collector errors are now stored and rendered | — | Accepted, bounded twice and escaped |
| 7 | The Docker socket is root-equivalent on the host | — | Accepted by design, off by default, read-only |
| 8 | `upgrade-controller.sh` needs Docker on the operator's machine | — | Accepted; the alternative is worse |
| 9 | The floor does not protect against a compromised **root** account | — | Residual, stated |

No finding required revoking or rotating anything: no release has been
published, no signing key exists outside a test run, and no deployment of this
code exists outside a developer's machine and CI.

---

## 2. The anti-rollback floor

### 2.1 What it is arranged against

The previous review's §2.1 recorded one unmitigated capability of a
compromised controller: **offer an older signed release**. The signature
establishes what the bytes are; it says nothing about *when*, so a genuinely
published release with a known vulnerability stays signed for ever.

`internal/install/floor.go` is the mitigation. A monotonic version floor,
written by the **privileged installer** and refused by the agent if it is not
root-owned:

- The state file lives in `/var/lib/hcc-agent-state`, a **sibling** of the
  agent's own directory rather than a subdirectory of it.
- `LoadState` refuses to read a file the agent account could have written:
  `checkRootOwnedDirectory` and `checkRootOwnedFile` both require uid 0 and
  no group or other write bit.
- The check is gated by a package variable `requireRootOwnership`, which is
  **not exported**. A test in the same package can relax it; nothing outside
  can, and `TestTheOwnershipRequirementIsOnByDefault` asserts the default.

A sibling rather than a sticky-bit subdirectory, because `+t` on
`/var/lib/hcc-agent` is a subtle property that one `chmod` undoes silently,
and writing to `/var/lib` needs privileges the agent does not have. Recorded
as a decision in the progress log, with the alternative that was rejected.

### 2.2 What it does not protect against

**Root on the host.** An attacker who is root can rewrite the floor, the
binary, the unit file and the trust store. The floor is arranged against a
*compromised controller*, which is a different and much more likely
adversary: it is reachable from a browser and holds the fleet's credentials.

Stated plainly here because the opposite claim is easy to make by accident.
`docs/UPDATES.md` says the same, and the Go doc comment on `State` says it
first.

### 2.3 The TOCTOU that the floor work uncovered — finding 1

**What.** `install.Upgrader` opened the staged archive **twice by path**:
once to compute its digest and once to extract it. The directory it opened
from is writable by the unprivileged agent account, so the agent could
present a signed archive for the digest check and swap it for another before
the extraction — getting arbitrary content installed as root.

**Why it was not obvious.** Each open is correct in isolation, and both are
inside the privileged installer, which reads as "this is the trusted side".
The defect is the gap between them.

**The first fix failed its own test.** Passing a single `*os.File` through
both steps removes the second open — but a test that rewrote the file in
place with `os.WriteFile` still saw the new content, because `os.WriteFile`
truncates and rewrites the *same inode* and the descriptor follows it. The
test was right and the fix was not.

**The fix.** The installer copies the archive into a **root-owned private
staging file** bounded by the signed `size_bytes`, and extracts from that.
The descriptor is kept as well. The test now uses `os.Rename` for the rename
form, which is the attack the descriptor defeats, and keeps the in-place
rewrite, which is the one the private copy defeats.

### 2.4 The symlink in the prefix test — finding 2

`resolveArchive` accepted a path if it had the staging directory as a
prefix. The agent account can create a symlinked subdirectory inside its own
staging directory, so `/var/lib/hcc-agent/staging/evil/../../../etc/shadow`
satisfied the prefix test. Fixed: the path must be a **direct child**, and
the open uses `O_NOFOLLOW`.

### 2.5 Replay — finding 4

The handoff instruction carried no identity, so a compromised agent account
could re-invoke the privileged installer with the last instruction
indefinitely. Bounded now: the floor state records `LastInstructionID` and
`PermitsInstruction` refuses a repeat. The impact was low — the installer
would reinstall a version already installed — but it is unbounded work a
non-root account can cause root to do.

---

## 3. Multi-signature verification

`MaxSignatures = 8`, and a document with more is refused rather than
truncated. The verifier walks the set and applies **worst-first precedence**:
a document with one trusted signature and one that is malformed reports the
malformed one.

That ordering is the security-relevant part. The attractive alternative —
"accept if any signature verifies" — makes a document with one good
signature and seven forgeries indistinguishable from a clean one, and hides
the fact that somebody is trying. The bound exists because signature
verification is the one place an unbounded list turns into CPU an attacker
chooses.

Not a PKI: no certificates, no chains, no expiry, no revocation. A rotation
is "sign with both keys for one release", and the agent trusting either is
what makes the overlap work. `contracts/release-signing.json` replays 24
vectors in both languages so the Python and Go verifiers cannot drift.

---

## 4. The new collectors

Six modules were added. Four read files, one runs a command, one opens a
socket.

### 4.1 What they read, and what they do not

| Module | Reads | Notes |
| --- | --- | --- |
| `filesystem` | `/proc/self/mountinfo`, `statfs` | Network and pseudo filesystems excluded before measurement, not after |
| `diskio` | `/proc/diskstats`, `/sys/block` | Whole disks only; `/sys/block` membership is the test |
| `network` | `/proc/net/dev`, `/sys/class/net` | Interface names are checked for `/` and `\` before being used in a sysfs path |
| `sensors` | `/sys/class/hwmon` | Read-only, no writes to any `*_enable` node |
| `systemd` | `systemctl` output | §4.2 |
| `docker` | Docker Engine API socket | §4.3 |

None of them writes anything. None interpolates a value it read into a shell.
`network.reportable` refuses a name containing a path separator *before*
joining it into a sysfs path, which is the one place a kernel-supplied name
reaches the filesystem API.

### 4.2 `systemctl` is a command, and the argv is fixed

Running a command from a monitoring agent needs justifying, and the
alternative — talking D-Bus to pid 1 — means either a D-Bus library or
hand-rolled wire framing against a privileged socket. `systemctl
--output=json` is a structured interface and a smaller dependency.

The protections, each with a test:

- **Only read-only subcommands.** `TestOnlyReadOnlySubcommandsAreInvoked`
  asserts the set is `list-units` and `show`, both directions.
- **A unit name that could become an option is refused.**
  `TestAUnitNameThatCouldBecomeAnOptionIsRefused` — a watched unit called
  `--version` would otherwise change what the command does.
- **Only named properties are requested**, so a future `systemctl` that
  gained a secret-bearing property does not start reporting it.
- **The binary is resolved from a fixed list**, `/usr/bin/systemctl` or
  `/bin/systemctl`, never from `PATH`.
- **A timeout**, because `systemctl` talks to pid 1 and a hung pid 1 must not
  hang the agent.

The unit list comes from `HCC_AGENT_SYSTEMD_UNITS`, which is set in the
systemd unit file by root. An agent that could edit its own environment could
already edit its own binary.

### 4.3 The Docker socket — finding 7, accepted

**Membership of the `docker` group is root on the host.** The socket accepts
`POST /containers/create` with a bind mount of `/`, and anything that can
reach it can obtain root. That is a property of Docker, not of this product,
and no amount of care in the client changes it.

What this product does about it:

- **Off by default and not switchable from the web.** `Socket == ""` disables
  the module entirely, and the socket path comes from
  `HCC_AGENT_DOCKER_SOCKET` — an environment variable in the unit file. The
  controller cannot set it. An administrator enabling the `docker` module on
  a host whose agent has no socket gets a module that reports *why* it cannot
  collect, not one that starts working.
- **The registry marks it `requires_privileges=True`**, and it is the only
  module that does. A test asserts the set in both directions, so a second
  privileged module cannot be added without the test naming it.
- **Read-only requests only.** `TestOnlyReadOnlyRequestsAreEverMade` asserts
  the client issues `GET` and nothing else. There are no start, stop or
  restart controls in this release, deliberately — the brief forbade them for
  this phase and the absence is enforced by there being no code for it.
- **A missing socket and an ungranted one are different answers**, so an
  operator can tell "Docker is not installed" from "the agent is not in the
  group".

**Residual:** an attacker who can edit the agent's unit file can grant the
socket. They could also replace the binary. This is not a boundary the
product claims.

---

## 5. Staged rollouts

### 5.1 The reach

One administrator action now installs software on up to `MAX_ROLLOUT_HOSTS`
(500) machines over time. That is the largest blast radius in the product.

What bounds it:

- **Administrator only.** `updates.router` is mounted on the admin gate; the
  read endpoints are on the authenticated gate. Create, pause, resume and
  cancel are all audited.
- **Creating a rollout starts nothing.** It is `pending` until a background
  pass picks it up, so a mistaken creation can be cancelled before any host
  is touched. The end-to-end scenario asserts this.
- **The canary gate is the real bound.** A batch starts only when the
  previous one has installed **and stayed healthy** over a settling period,
  judged on four separate questions — connected now, reporting the target
  version, acknowledging its configuration at the current revision, and
  delivering telemetry since the install. A release that breaks hosts stops
  after one machine.
- **The failure gate re-fires.** Resuming a rollout the gate stopped pauses
  it again unless `override_failures` is passed, and that flag is recorded on
  the rollout rather than implied by the resume — so "resumed after a
  failure" and "resumed after an administrator paused it" cannot look alike
  in an audit trail.
- **The agent still decides.** A rollout produces ordinary update jobs, which
  a host with `HCC_AGENT_AUTO_UPDATE` unset declines. The rollout cannot make
  a host act; it can only ask more hosts in sequence.

### 5.2 What a compromised controller gains

Nothing it did not have. It could already create an update job per host in a
loop. A rollout is a scheduler for jobs it could already create, and every
constraint from the previous review — the signature, the digest from verified
metadata, the version floor — applies unchanged to each job.

### 5.3 The advisory lock

`run_pass` takes `pg_advisory_xact_lock(0x48434352)`, so two controller
processes cannot both advance a rollout. Transaction-scoped, so a crash
releases it. There is no in-memory state: a pass reads rows, decides, writes
rows, and a test constructs a *fresh service object* between passes to prove
a restarted controller reaches the same conclusion.

---

## 6. Controller upgrades — finding 8, accepted

`GET /api/v1/controller/upgrade-readiness` reports and never acts, and a test
asserts `POST`, `PUT`, `PATCH` and `DELETE` all answer 405.

**The controller cannot replace itself**, and that is the design rather than
a limitation. Replacing the container the application is served from needs
the Docker socket, which is root on the host — so the application would have
to hold root in order to upgrade itself. `scripts/upgrade-controller.sh` runs
on the operator's machine, with the operator's Docker access, and the
endpoint tells them what the upgrade would involve.

There is no remote-command API for the same reason. Adding one would make
every other protection in this document contingent on the controller never
being compromised.

**The backup is verified by restoring it** into a scratch database in the
same instance, with its table count compared against the live one. A dump
whose checksum matches is intact; that is not the same as one that restores.

**No automatic database rollback**, stated rather than implied: an Alembic
downgrade that drops a column is data loss performed by a script at the worst
possible moment, and some of this project's own downgrades refuse rather than
destroy.

### 6.1 The endpoint lied in every production image — fixed

Found by `deployment-e2e.sh`, which is the only test that runs against the
built image.

`hcc/updates/schema.py` located the Alembic history at `parents[2] /
"alembic"` — correct in a checkout. In the container the package is installed
into the venv, so the same expression resolves to `site-packages/alembic`,
which is **Alembic's own library package**, and the only check was
`is_dir()`.

The failure was silent. `ScriptDirectory` does not object to a directory with
no `versions/`: it constructs and reports a history with no revisions. So the
ancestry came back empty with no exception and no log line, `head_revision()`
returned `None`, and the endpoint answered *"The schema is current and nothing
is in flight"* to a database reporting a revision from the future — the one
direction a backup taken now cannot recover from, and the question the
endpoint exists to answer.

The resolution now validates (`env.py` **and** `versions/`, because either
alone has a plausible impostor) and searches the image's layout too. An
unreadable history now reports `schema_is_current: false` and says it is a
packaging fault, because "cannot tell" is nearer to "not current" than to
"current" and that field is what a script would check.

This is a **false-reassurance** defect rather than a privilege one, and it is
the kind this product should be most careful about: an operator acting on it
would have deployed an older image over a newer schema.

---

## 7. Collector health — finding 6, accepted

The new `host_module_health` table stores a string the **agent** supplies and
the host page renders it. That is untrusted text from a machine that may be
compromised, reaching a browser.

What bounds it:

- **Two length bounds.** The protocol refuses a frame whose `error` exceeds
  `MAX_COLLECTOR_ERROR_LENGTH` (256), and the service truncates to the same
  length before the UPSERT. The second is not redundant: the controller
  writes its own refusals into the same column, and those are f-strings over
  values from the frame. A string one character too long raises
  `StringDataRightTruncationError` inside the WebSocket handler, which the
  agent sees only as `protocol_error` and retries for ever — which is exactly
  what `metric_series.unit` did earlier in this run.
- **Rendered, never interpreted.** React escapes it; there is no
  `dangerouslySetInnerHTML` anywhere in the client. A test stores
  `<img src=x onerror=alert(1)>` and asserts the text appears and no element
  does.
- **Stored verbatim, not pre-escaped.** Escaping belongs at the point of
  rendering; a column holding pre-escaped text is one where every later
  reader has to guess the encoding.
- **No SQL path.** The UPSERT is parameterised and the value travels as a
  `text[]` bind parameter.
- **The module id is not attacker-chosen.** An entry for a module the host
  has not enabled is rejected before any health row is written, the registry
  is checked, and the column has a CHECK constraint listing the known ids. An
  agent cannot create rows for modules an operator never turned on.

**Residual:** an operator reading a hostile error string could be socially
engineered by its content — "contact support at …". That is true of every
field a host supplies, including its own name, and is not specific to this
one.

---

## 8. A reconnection erased self-description — finding 5, fixed

`EnrollmentService.authenticate` wrote `agent_version`, `platform` and
`agent_started_at` unconditionally on every connection accept. The connect
handler has a credential and a protocol version and nothing else, so an
ordinary reconnection wrote three NULLs over what the host had reported.

Not cosmetic: `agent_version` is what the update planner compares against the
manifest, so between a reconnect and the first heartbeat a host's plan is
`unknown_version` — the controller cannot plan updates for it, and a rollout
pass landing in that window mis-handles it. After a controller restart the
whole fleet is in that state at once.

The rule `record_heartbeat` already applied now applies here: **an agent that
does not say must not be read as saying nothing.** The same rule the
protocol's `omitempty` fields are documented by.

Found by `update-e2e.sh` §16, forty milliseconds after a reconnect.

---

## 9. The demo environment

The brief's constraints, and what enforces each:

| Constraint | Enforcement |
| --- | --- |
| No hardcoded public administrator password | Generated per run with `secrets`, printed once to the terminal, never written to a file in the repository |
| Not exposed on public interfaces by default | `127.0.0.1` binding, hardcoded in `scripts/demo.sh`; changing it is an edit, not a flag |
| No real hosts enrolled automatically | The demo enrols **the developer's own machine** only when `--with-agent` is passed, into a disposable state directory under `.demo/`, and prints what it is doing |
| No fabricated telemetry | Every sample in the demo comes from a real agent reading the real kernel. There is no seeded-data path |

`.demo/` is in `.gitignore`. `make demo-clean` removes the database and the
state directory.

The demo runs the real controller with a real PostgreSQL, not a mock. That
means it is a real deployment and should not be left running on a shared
machine — stated in `docs/DEMO.md` rather than prevented, because preventing
it would mean the demo not being the real thing.

---

## 10. What was deliberately not done

- **The rollup bucket bound was not raised.** `docs/PERFORMANCE.md` §4
  measures a ceiling of 5,000 series, and raising the constant trades
  transaction length for headroom in a way that is not measured. An
  installation past the ceiling now gets a `metric_rollup_behind` warning;
  before, it got silence.
- **No container start/stop/restart controls.** The brief forbade them for
  this phase, and the absence is structural: there is no write path in the
  Docker client and a test asserts it.
- **No release was published**, no tag created, no image pushed.
  `scripts/prepare-release.sh` supports an ephemeral key for validating the
  pipeline, and CI asserts the marker that says the artefacts it produced are
  **not** publisher-signed.

---

## 11. Residual risks, restated

1. **Root on a host defeats the floor.** Stated in §2.2 and in the code.
2. **A compromised publisher key is total.** Unchanged from the previous
   review; there is still no revocation.
3. **A compromised controller can still withhold updates**, and can now
   withhold them in a more organised way. Availability, not integrity.
4. **The Docker socket is root.** Off by default, ungrantable from the web,
   read-only in this client, and still root for anything that reaches it.
5. **The rollup ceiling is a capacity limit, not a security one** — but an
   installation past it has monitoring data that silently stops being
   current, which is the kind of quiet wrongness this product exists to
   avoid. Now signalled.
