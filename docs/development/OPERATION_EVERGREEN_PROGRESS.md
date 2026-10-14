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

---

## Measured results

| Command | Result |
| --- | --- |
| `make check` | ✓ all gates, 1051 backend unit, 561 frontend |
| `go test ./internal/version/ ./internal/release/` | ✓ |
| `./scripts/contracts.sh --check` | ✓ stable across regeneration |

---

## Not started yet

Phases C through O. Next: the update state machine and the durable persistence
it needs, because everything after it reports into those tables.
