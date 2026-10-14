# Operation EVERGREEN — progress

Working state for the run that builds update management. Factual and terse:
this file records *what was done and verified*, so work is not repeated after a
context loss.

**This file is not evidence that anything passed.** Every test count below
names the command that produced it. Re-run the command rather than trusting the
number.

Branch: `feature/evergreen-update-management`, from `main` at `3994787` (the
PR #7 merge commit, verified rather than assumed).

Design: [`docs/plans/evergreen-update-architecture.md`](../plans/evergreen-update-architecture.md).

---

## Phase log

### Phase A — architecture and threat model — complete

[`docs/plans/evergreen-update-architecture.md`](../plans/evergreen-update-architecture.md).

Audited before writing it: the existing `upgrade.go`, the schema-v1 manifest
and its endpoint, the agent WebSocket protocol, the host model, the audit
service, the monitoring config service (as the model for durable
controller-owned state), the connection registry, the metric maintenance task
(as the model for a durable background loop), and the Alembic history.

**Two findings constrain everything else, and both came from reading rather
than assuming:**

1. **`protocol.ParseServerFrame` errors on an unrecognised frame type.** It
   does not ignore it. So pushing a new controller→agent frame to the existing
   fleet would make every agent report a protocol error and disconnect. Any new
   push has to be gated on a negotiated protocol version. This is the single
   most important compatibility fact in the project.
2. **Go's `json.Unmarshal` ignores unknown object keys.** So adding a field to
   an *existing* frame — `heartbeat_ack` — is invisible to an old agent. That
   asymmetry is what makes a safe migration path exist at all, and it is why
   update notification piggybacks on the heartbeat acknowledgement rather than
   arriving as a new frame.

Decisions taken, each against the alternative, with the reasoning in the
document: the controller is demoted from vouching for bytes to authorizing
*which* update and *when*; release metadata is signed with Ed25519 and the
agent carries the key; metadata ships in the image rather than being fetched
(so air-gapped installs work and there is no SSRF surface); the controller
never replaces its own container.

### Phase B — versions, compatibility and authenticity — in progress

**Semantic versioning, both languages** (`hcc/updates/version.py`,
`agents/linux/internal/version`).

Written because `"0.10.0" < "0.9.0"` is true for a string comparison, and an
update system built on one is correct for nine releases and then offers a
downgrade as an upgrade. Full semver 2.0.0 ordering including pre-release
precedence; build metadata parsed and excluded from precedence. It refuses
rather than coerces — `v1.2.3`, `1.2` and `latest` are errors.

Kept in step by `contracts/version-ordering.json`: **576 pairwise comparisons,
576 step kinds, 24 parses and 14 rejections**, generated from the Python
implementation and replayed by the Go tests. Verified non-vacuous by inverting
the pre-release rule in the Go implementation, which produced 60 failures.

**Manifest schema v2** (`hcc/agents/releases.py`).

Channels (`stable` < `beta` < `alpha`), per-release compatibility
(`protocol_versions`, `min_controller`, `schema_requirement`), `released_at`
and `notes_url`. v1 is still read and **upconverted** — its releases become
`stable` entries declaring no constraints, which is what they claimed — because
a controller upgraded ahead of its manifest is every controller for some
window, and breaking that would break the install command on every existing
deployment.

**Release metadata authenticity** (`hcc/updates/signing.py`,
`agents/linux/internal/release`).

Ed25519 over the manifest's **exact bytes**, detached. Six named outcomes
rather than a boolean, because "not valid" collapses "nobody signed this" — a
missing release step — with "somebody signed this badly", which is an attack.
There is no fallback to checksum-only trust and no flag that creates one.

Kept in step by `contracts/release-signing.json`: **16 vectors across all six
outcomes**, deterministic because the contract's test key is a fixed ASCII
seed and Ed25519 signatures are deterministic — a random key would make
`contracts.sh --check` fail on every run.

---

## Decisions that changed existing behaviour

Recorded because they are not refactors.

1. **A manifest whose `recommended` names a missing release is now refused at
   parse time.** It used to be tolerated, with the endpoint reporting
   "unavailable". The endpoint already turns a `ManifestError` into exactly
   that, with the error's own text — so refusing loses no reporting location
   and gains a precise message. It matters more than it did: a dangling
   reference used to cost one unavailable install command, and now the update
   planner reads the same manifest, where it would become "an update is
   available" followed by a 404. `test_agent_releases.py` documents the change.
2. **`MANIFEST_SCHEMA_VERSION` is 2**, and `SUPPORTED_MANIFEST_SCHEMA_VERSIONS`
   is `(1, 2)`. Reading more than is written is what makes a schema change
   deployable rather than a flag day.

## New dependency

`cryptography>=43,<47` on the server, for Ed25519 verification. Installs from a
manylinux wheel in under a second; the alternative is hand-rolling signature
verification, which is not an alternative. **The agent gains nothing**: Go has
`crypto/ed25519` in the standard library, which matters for a binary that
installs itself as root.

A wider version range than the project's other pins, deliberately: this
library's majors are frequent and almost always confined to backend and
platform support, while the two calls used here have been stable since 2.0.

---


### Phase C — the agent's update engine — complete

`agents/linux/internal/updater/updater.go`. Four gates the agent owns and the
controller cannot override: policy, expiry, authenticity, architecture and
direction. 37 tests, plus 13 wiring tests in `internal/agent`.

Everything is injectable — trust store, fetch, upgrade, clock, system — so the
whole decision table is testable without a network, a filesystem or root.

### Phase E — the protocol — complete

`PROTOCOL_VERSION = 2`, `SUPPORTED_PROTOCOL_VERSIONS = (1, 2)`. Five new frame
types, gated on `UPDATE_PROTOCOL_VERSION` because `ParseServerFrame` **errors**
on an unknown type rather than ignoring it. The notification rides on
`heartbeat_ack`, whose unknown keys Go ignores, so a v1 agent is never sent a
frame it would reject. `TestAVersionOneAgentKeepsWorking` asserts a v1 agent
handshakes, receives configuration, and is never pointed at a queued update
that genuinely exists.

### Phase F — database and API — complete

Migration `0010_update_management`: five tables, a partial unique index for
"one open job per host", three `set_updated_at` triggers. Verified upgrade →
`alembic check` → downgrade → upgrade on a disposable database that was
dropped afterwards.

Eight endpoints, read open to every role, writes administrator-only by router
rather than by a check in each handler.

### Phase G — the interface — complete

`apps/web/src/routes/updates.tsx` and three modules beside it, in the existing
design language. 23 tests through the real API client.

The test file caught a defect in itself worth naming: two cases called
`renderApp` twice, so an admin tree stayed mounted underneath the viewer tree
and the viewer case passed by finding the admin's buttons. A role test that
cannot fail is worse than no role test.

### Phase C/E wiring — the engine was never constructed — complete

The engine had existed since commit 8 with 37 tests and **nothing called it**.
`hcc-agent run` passed no options to `agent.New`, so `WithUpdateApplier` had
no caller and a real agent would have logged "the controller has an update"
and asked for nothing, for ever.

Now joined up in `cmd/hcc-agent/main.go`, with the trust anchor stamped at
link time (`buildinfo.releaseKeys`, set by `--trust-key`) and the host's own
veto in `HCC_AGENT_AUTO_UPDATE`, false by default and written into
`agent.env` as `false` by the installer.

### Phase J — release tooling — partial

- `scripts/sign-release.py`: keygen, sign, verify. Separate from the
  controller's CLI because a control plane that could mint signatures would be
  a control plane that could sign releases. `sign` verifies what it just
  wrote.
- `scripts/build-release-manifest.py` writes schema v2: channels, protocol
  versions, compatibility constraints. `--protocol-versions` is required.
- `scripts/build-agent-artifacts.sh --trust-key` stamps the publisher key.

**Not done:** the release workflow itself does not yet call any of these. No
tag, no release, no image was published — the brief prohibits it without
approval.

### Phase K — the mandatory end-to-end upgrade test — complete

`scripts/update-e2e.sh`, 52 assertions, exit 0, wired into CI as `update-e2e`.

Three real agent builds (`0.1.0-test`, `0.1.1-test`, `0.1.2-test`), a
per-run Ed25519 keypair, a signed v2 manifest, a disposable PostgreSQL, a real
controller behind a TLS terminator, an **artifact mirror behind a second TLS
endpoint**, and a privileged container with systemd as pid 1.

What it proves:

| # | Assertion |
| --- | --- |
| 10 | a host with updates disabled declines and names the setting |
| 12 | `0.1.0-test` → `0.1.1-test` installs, the service comes back, the host reports the new version |
| 13 | the event log is `downloading verifying staged installing restarting succeeded`, five from the host and the last from the controller |
| 14 | an archive substituted after signing is refused by digest and the host keeps running |
| 15 | a manifest signed by an untrusted key is refused by *both* halves |
| 16 | the fleet summary shows the host's own report |

### Phase L — the privileged installation path — complete

Found by running Phase K: the agent service is unprivileged, so it cannot
replace a root-owned binary or restart its own unit. The run ended with
"upgrading needs root and this process is uid 996".

Closed by `hcc-agent-update.path` + `hcc-agent-update.service` +
`hcc-agent apply-update`, with the request carrying signed metadata and **no
digest**. 24 tests in `internal/install/handoff_test.go`, most of them
refusals. [ADR-061](../ARCHITECTURE.md#adr-061-a-path-triggered-root-unit-installs-updates-because-the-agent-is-unprivileged).

### Phase D (partial) — the automatic policy evaluator — complete

`UpdateService.run_policy_pass` plus a task in `hcc/app.py` on
`HCC_UPDATE_POLICY_INTERVAL_SECONDS` (default 300, 0 disables and logs a
warning).

Found by writing the documentation: `hcc/updates/policy.py` and
`MAX_POLICY_ATTEMPTS` were a decision **nothing asked for**. An administrator
could set a host to "automatic patch" and nothing would ever happen, with no
error anywhere, and nothing called `expire_stale` either — so an expired job
held its host's only slot indefinitely.

The pass expires stale instructions *first*, because an expired job holds the
slot the decision below needs. One host whose start raises does not stop the
other ninety-nine: each is attempted independently and counted.

17 tests, including both halves of the maintenance-window boundary (a Wednesday
noon deferred, the same host at 03:00 on Sunday started) and all three
attempt-limit cases — three failures stop it, a newer version gets a fresh set,
and a manual attempt does not count.

### Phase M — CI — complete

`update-e2e` job, 40-minute timeout, `debian:13`, failure artefacts uploaded.
Not matrixed across distributions like `install-matrix`: the privileged path
is systemd, not distribution-specific, and three agent builds per job is
twelve minutes of CI per distribution for the same assertions.

### Phase N — security review — complete

[`docs/security/2026-10-14-update-management-review.md`](../security/2026-10-14-update-management-review.md).
Eight findings: four fixed, three accepted with reasoning, one open and
documented. The residual risk is stated rather than mitigated: a compromised
controller *or* a compromised agent account can force an older **signed**
release, and the fix is a monotonic version floor the agent remembers.

### Phase O — documentation — complete

- [`docs/UPDATES.md`](../UPDATES.md) — the administrator's guide: signing,
  policies, channels, windows, the sequence diagram, troubleshooting, and a
  "What is not built" section that names the gaps.
- ADR-060, ADR-061, ADR-062.
- Open design question 6 resolved in `ARCHITECTURE.md`.
- Registered in both document indexes.

---

## Phases not built

Stated here and in [`UPDATES.md`](../UPDATES.md#what-is-not-built) so nobody
has to discover them.

**Phase D — staged rollouts.** `update_rollouts` and `update_rollout_members`
exist, the audit vocabulary has `update.rollout_created` and
`update.rollout_controlled`, and **nothing drives a canary, a batch, a pause
or a resume.** The tables are the shape the scheduler will need.

The *policy evaluator* — which is what makes "automatic patch" automatic — is
built (see Phase D-partial below) and starts every eligible host in one pass.

**Phase H — the assisted controller upgrade.** The Updates page shows the
running version and the applied schema revision; `UPDATES.md` documents the
manual procedure. There is no `scripts/controller-update.sh` and no rendered
upgrade plan.

**Phase I — pre-update backup integration.** `scripts/backup.sh` and
`scripts/restore.sh` exist from an earlier milestone and are unchanged. Nothing
takes a backup as part of an update, and no automatic database rollback is
implemented or promised.

---

## Defects found, and how

1. **My own test caught my own contract generator.**
   `test_no_request_handling_code_can_sign_a_release` scanned all of `hcc/` for
   `generate_keypair` and `sign_manifest` and found `hcc/cli/contracts.py`.
   That generator is build-time tooling invoked by `scripts/contracts.sh` and
   never runs in the serving process, so the test is now scoped to the real
   boundary — `api/`, `services/`, `agents/`, `security/` — with the exclusion
   named rather than implied. It also surfaced dead code: the generator called
   `generate_keypair()` and then discarded the result in favour of the
   deterministic seed.

2. **The event log could not be ordered.** `now()` is the *transaction*
   timestamp, so both events `start_update` writes shared it and the UUID
   tie-break randomised the order. `UpdateEvent.id` is now
   `BIGINT GENERATED ALWAYS AS IDENTITY`.

3. **`restarting → succeeded` was missing from the transition table.** The
   first reconciliation test failed with "a job cannot go from restarting to
   succeeded", which was the table telling me the model was wrong:
   `systemctl restart` kills the installing process, so the agent can never
   report `verifying_health` in the controller-driven path.
   [ADR-062](../ARCHITECTURE.md#adr-062-no-process-reports-its-own-successful-replacement).

4. **Committed on a red gate** at `d76768b`. Wrong, acknowledged, fixed in
   `4405232` with the failures named.

5. **A racy rollup assertion** in `scripts/monitoring-e2e.sh` failed in CI:
   the agent keeps writing into a bucket the assertion had sealed. Replaced
   equality with containment, plus an exactness check scoped to buckets whose
   `sample_count` matches the current raw count, reported as *skipped with the
   reason* when none qualify. The same commit added a missing `dim()` helper
   that would have been `command not found` at the moment the branch fired.

6. **An edit silently missed its target.**
   `test_no_message_type_carries_anything_executable` was given the five new
   frame types by a replacement whose indentation did not match, so the check
   never saw them. Found by ruff complaining about an unused import, then
   verified by checking the test would catch a `run_command` field.

7. **`SCHEMA_VERSION` was two schemas sharing one number.** Bumping the
   manifest to v2 broke reading `artifacts.json`, which is a different schema
   that merely also happened to be version 1. Caught by its own test.

8. **Four defects the end-to-end scenario found on its first real run**, none
   of them visible to 1200 unit tests:
   - the artifact download trusted only public roots, so an internal CA —
     the documented and tested deployment — failed with
     `x509: certificate signed by unknown authority`;
   - a host with updates disabled went silent instead of declining, leaving a
     job in `queued` for two minutes;
   - the controller offered updates it had not authenticated;
   - `restarting` was reported *after* the restart that kills the reporter.

9. **Two more on the second run**, both the same class as (8d):
   - the handoff's reports were *queued* onto a buffered channel and the
     process was killed before the loop drained it, so the job showed
     `downloading` for a host that had finished updating;
   - `downloading → installing` is not a legal transition — the table requires
     `verifying` and `staged` between them — so every report after the
     download was being refused server-side.

10. **The staged archive was deleted before root could read it.** `Apply`'s
    deferred cleanup ran on the handoff path too, so the request named a file
    that had existed a millisecond earlier.

---

## Measured results

Every number names the command that produced it. Re-run the command rather
than trusting the number.

| Command | Result |
| --- | --- |
| `make check` | ✓ all gates; 1205 backend unit, 585 frontend, Go clean, gofmt clean, docs 10/10 |
| `./scripts/test-integration.sh` | ✓ 1094 integration tests against real PostgreSQL |
| `go test ./...` (agent) | ✓ every package, race-clean |
| `./scripts/update-e2e.sh` | ✓ **52 passed, 0 failed**, exit 0, `debian:13` |
| `./scripts/install-matrix.sh` | ✓ 61 passed, 0 failed, `debian:13` — unchanged by this work |
| `./scripts/contracts.sh --check` | ✓ stable across regeneration |
| `./scripts/check-docs.sh` | ✓ 10/10, 62 ADRs contiguous |

Safety, verified rather than asserted: the developer's own `hcc` database was
untouched (every scenario creates and drops its own), no tag or release was
created, no image was pushed, and automatic updates were enabled only inside
disposable containers by writing those containers' own environment files.
