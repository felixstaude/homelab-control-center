# Update management: architecture and threat model

**Status:** design, being implemented on `feature/evergreen-update-management`.

This document exists to make the decisions *before* the code, and to be
specific enough to argue with. It is not a survey of options. Where a decision
rules something out, the reason is stated, because the reasons are what will
still be useful in a year.

The thing being designed: an administrator opens Homelab Control Center, sees
which versions the fleet is running and which are available, configures how
updates should happen, and gets them to happen — safely, visibly, and without
SSHing into every machine.

---

## 1. What already exists

Operation Nightshift II built the installation platform. This builds on it
rather than replacing it.

| Piece | State |
| --- | --- |
| `hcc-agent upgrade --from <archive> --sha256 <digest>` | Works. Local archive only — it does not download. Verifies the digest, extracts exactly one member, checks the ELF header, preserves the previous binary, restarts, waits for reconnection, rolls back on failure. |
| `GET /api/v1/agent-releases` | Serves a manifest (schema v1) the controller ships: `recommended`, `minimum_supported`, and `releases[version][platform]` with `archive`, `url`, `sha256`, `size_bytes`, `tier`. |
| Agent WebSocket protocol | Version 1. Closed message set. Controller-owned monitoring configuration travels on it. |
| `scripts/build-agent-artifacts.sh` | Reproducible three-architecture build with `checksums.txt`, `artifacts.json`, SPDX SBOM. |
| `release-prepare.yml` | Builds, verifies, executes under emulation. Publishes nothing. Its attestation job has never run, because it is gated on a tag. |
| `scripts/backup.sh` / `restore.sh` | Work, exercised in CI. |

Two properties of the existing code constrain everything below.

**`protocol.ParseServerFrame` errors on an unrecognised frame type.** It does
not ignore it. So a controller that pushed a new frame type to the existing
fleet would not be gracefully ignored — every agent would treat it as a
protocol error. Any new controller→agent message must be gated on a negotiated
protocol version. This is the single most important compatibility fact in the
project.

**Go's `json.Unmarshal` ignores unknown object keys.** So adding a field to an
*existing* frame — `heartbeat_ack`, say — is invisible to an old agent. That
asymmetry is what makes a safe migration path possible at all.

---

## 2. Trust boundaries

Five parties, and what each is trusted for.

| Party | Trusted for | Not trusted for |
| --- | --- | --- |
| **Publisher** (whoever holds the release signing key) | Authenticity of release metadata | Anything at runtime |
| **Controller** | Authorizing *which* update a host should take, and when | Vouching for *what the bytes are* |
| **Download origin** (GitHub Releases, a mirror) | Availability | Integrity or authenticity |
| **Agent** | Enforcing its own local policy, and refusing | — |
| **Administrator** | Changing policy, approving an update | — |

The row that matters is the controller's. In the existing design the controller
vouches for the digest, and that is defensible for a human-initiated upgrade:
the administrator reads a digest inside an authenticated TLS session and types
it. It is **not** sufficient for automatic updates, because it means a
compromised control plane can install arbitrary code as root on every monitored
machine, and "the control plane is compromised" is precisely the scenario a
fleet update mechanism must not amplify.

So the controller is demoted. It decides *whether* and *when*; it does not get
to decide *what*.

### The publisher key

Release metadata is signed with **Ed25519**. The agent embeds the trusted
public key at build time (`internal/buildinfo`, same mechanism as the version
stamp). Verification is `crypto/ed25519` from the Go standard library — no new
agent dependency, which matters for a binary that installs itself as root.

The controller also verifies, using `cryptography` (a new server dependency,
justified below), so that the interface can tell an administrator whether the
metadata it is serving is signed, by which key, and whether the signature
verifies. A controller that served unsigned metadata without saying so would
make the whole model decorative.

**Why Ed25519 and not Sigstore/cosign.** The attestation path already in
`release-prepare.yml` binds an artifact to the workflow that produced it, which
is the right thing for someone auditing a download. It is the wrong thing for a
7 MB Go binary verifying a signature on a NAS with no network policy and no
`cosign`: it needs an OIDC-aware verifier, a transparency-log lookup, and a
trust policy expressed in certificate identities. Ed25519 over a canonical JSON
document needs 32 bytes of public key and one standard-library call. The two
are complementary and both are kept — attestation for humans auditing a
release, a detached signature for agents verifying one.

**Why not require signatures for the existing local-archive path.** Because
that path has a human in it who got the digest from an authenticated session,
and breaking it would mean there is no way to recover a host when release
metadata is unavailable. It stays, scoped: see §5.

---

## 3. Release metadata

### Source

`releases.json`, shipped **inside the controller image** and pointed at by
`HCC_AGENT_RELEASE_MANIFEST`, exactly as the schema-v1 manifest is today. Not
fetched from the internet by the controller at runtime.

**Why not fetch from the GitHub API.** A controller that reaches out to a
public API to discover releases is a controller that (a) does not work in the
air-gapped and firewalled homelabs this project is for, (b) acquires an
outbound dependency on a third party for a security-critical function, and (c)
becomes an SSRF surface the moment the URL is configurable. Shipping the
metadata with the release that was built alongside it keeps the discovery
problem where it belongs: in the release pipeline, offline, verifiable.

Consequence, stated plainly: **a controller only knows about agent releases
that existed when it was built.** Learning about a newer agent means updating
the controller. That is a real limitation and it is the right trade for this
product. An optional signed-bundle import (§12) covers the case where somebody
wants a newer agent without a controller upgrade.

### Schema v2

v1 has no channels, no compatibility model and no signature. v2 adds them.
`schema_version` is 2; the loader accepts **both** 1 and 2, because a v1
manifest is a valid thing for an older release to have shipped and refusing it
would turn a documentation example into a startup failure.

```
{
  "schema_version": 2,
  "generated_at": "2026-10-14T…Z",
  "channels": { "stable": "0.1.0", "beta": "0.2.0-beta.1", "alpha": "…" },
  "minimum_supported": "0.1.0",
  "releases": {
    "0.1.0": {
      "channel": "stable",
      "released_at": "…",
      "notes_url": "https://…",
      "protocol_versions": [1, 2],
      "min_controller": "0.1.0",
      "min_agent_for_controller": "0.1.0",
      "schema_requirement": "0009",
      "platforms": {
        "linux/amd64": { "archive": "…", "url": "…", "sha256": "…", "size_bytes": 1, "tier": "supported" }
      }
    }
  }
}
```

Signed as a **detached signature over the exact file bytes**
(`releases.json.sig`, base64 Ed25519), not over a re-serialisation. Two JSON
libraries do not agree on key order, integer rendering or non-ASCII escaping,
and a canonicalisation step would turn a formatting difference into something
that looks exactly like tampering. This is the same reasoning the config
snapshot already uses for its checksum, and it was learned the same way.

---

## 4. Versions and compatibility

Semantic versioning, compared **by parsed components, never lexicographically**
— `0.10.0` is after `0.9.0`, and a string comparison says otherwise.
Pre-release identifiers order per semver 2.0.0: `0.2.0-alpha.1` <
`0.2.0-beta.1` < `0.2.0`. Build metadata is parsed and ignored for ordering.

The comparison answers one of a closed set of outcomes, because "is there an
update" is not a boolean an interface can render usefully:

| Outcome | Meaning |
| --- | --- |
| `up_to_date` | Running the newest in its channel |
| `update_available` | Newer, and compatible |
| `update_available_incompatible` | Newer, and something blocks it — named |
| `downgrade` | Older than installed; needs explicit authorization |
| `pinned` | A version pin holds this host where it is |
| `unknown_version` | The host reports something not in the metadata |
| `unsupported` | Below `minimum_supported` |
| `metadata_unavailable` | No manifest, or it would not load |

Compatibility is a conjunction, and every failing clause is reported rather
than just the first, because an administrator fixing one blocker wants to know
about the second one now:

1. The target's `protocol_versions` intersects what the controller supports.
2. The controller's version is at least the target's `min_controller`.
3. The target version is at least the controller's own `min_agent`.
4. The database schema is at or past `schema_requirement`.
5. The host's architecture appears in the target's `platforms`.

Channels are ordered `stable` < `beta` < `alpha` by *stability*, and a host on
`stable` is never offered a pre-release. Opting into a pre-release channel is
explicit and per host.

---

## 5. Agent update lifecycle

A durable state machine, persisted on the controller, with the agent reporting
transitions. States:

```
available → queued → downloading → verifying → staged → installing
          → restarting → verifying_health → succeeded
                                          ↘ failed → rolled_back
  (any non-terminal) → cancelled | deferred
```

`deferred` is not a failure: it is what a maintenance window produces outside
its hours, and conflating the two would make a healthy fleet look broken.

The agent's own sequence, with the gates in order:

1. Receive an instruction (or find one on reconnect — §6).
2. **Local policy gate.** Auto-updates enabled? Channel permitted? Pin
   satisfied? Outside a maintenance window? → `deferred` or refusal. *This gate
   is on the agent, and that is the point:* a compromised controller cannot
   install a version the host's own configuration forbids.
3. **Compatibility gate.** Architecture, protocol, version ordering. A
   downgrade without the explicit flag is refused.
4. **Metadata authenticity.** Verify the Ed25519 signature over the release
   metadata against the embedded key. If authenticity is required and no valid
   signature exists, **refuse** — there is no fallback to checksum-only, and no
   flag that creates one.
5. Download to a temporary file on the same filesystem as the target, bounded
   by `size_bytes` and by `MaxArchiveBytes`.
6. Verify the digest from the *signed* metadata.
7. Extract exactly one member named `hcc-agent`; refuse links, non-flat names,
   anything else. Check the ELF class and machine. Run `--version` and compare.
8. Move the running binary to `<path>.previous`. Rename the new one in.
9. `systemctl restart`. Wait for the controller's configuration to be written —
   the same "installed means connected" proof the installer uses.
10. On failure within the window: restore `.previous`, restart, report
    `rolled_back`.
11. Report the result. Preserved throughout: the enrollment credential, the
    configuration file, monitoring settings, the service account, the CA bundle.

Concurrency: one update per host, enforced by a PostgreSQL advisory lock on the
same lock class host writes already use, so an update and a credential
revocation serialise against each other rather than racing.

### What rollback does not cover

Rolling back the binary is safe because the agent's on-disk state is a
credential and a config file, and neither has ever been migrated. That is why
agent rollback is automatic and controller rollback is not.

---

## 6. Controller ↔ agent protocol

**Protocol version 2.** The controller supports `{1, 2}`. Update frames are
exchanged only when the negotiated version is ≥ 2; a v1 agent is never sent
one, which is what keeps the existing fleet working.

Notification piggybacks on `heartbeat_ack` via an added `omitempty` field
(`update_instruction` id). A v1 agent ignores an unknown key; a v2 agent
responds. This mirrors how configuration drift is already detected, and it
means a missed push costs one heartbeat interval rather than a stuck update.

New frames, all typed and allow-listed:

| Frame | Direction | Carries |
| --- | --- | --- |
| `update_instruction` | → agent | instruction id, target version, platform, archive url, digest, size, signed-metadata document and signature, `expires_at`, `allow_downgrade` |
| `update_request` | → controller | nothing; "send me the instruction you mentioned" |
| `update_progress` | → controller | instruction id, state, optional detail |
| `update_result` | → controller | instruction id, state (terminal), resulting version, error summary |
| `update_cancel` | → agent | instruction id |

**There is nothing executable in any of them.** No command, no script, no path
to run, no shell. `update_instruction` is data describing *which published
release to install*, and the agent decides whether to. The existing
`TestNoMessageTypeCarriesAnythingExecutable` is extended to cover them.

Failure handling:

- **Expiry.** An instruction has `expires_at`. An agent that was offline for a
  day does not wake up and install last week's decision.
- **Replay.** The instruction id is a UUID the controller issued once. The
  agent records the last instruction it acted on and refuses a repeat; the
  controller refuses a result for an instruction it does not have open.
- **Lost acknowledgement.** Reconnect reconciliation: on connect, a v2 agent
  reports the instruction id and state it last knew, and the controller
  reconciles. An update that completed while the socket was down is recorded
  from the agent's reported version, not lost.
- **Conflicting requests.** One open instruction per host. A second is refused
  while one is open, rather than queued — queueing update instructions is how
  a host ends up applying a decision made three states ago.
- **Controller restart.** State is in PostgreSQL. The scheduler reconciles on
  start; nothing lives only in memory.

---

## 7. Automatic updates and rollouts

**Off by default.** Every policy defaults to manual, on every host, and
enabling it is an administrator action that is audited.

Policies: `manual`, `notify`, `patch` (automatically install compatible patch
releases), `minor` (also minor), `pinned` (hold a version). None of them ever
crosses a compatibility boundary — a target that fails any clause in §4 is
never installed automatically, whatever the policy says.

Maintenance windows carry a timezone, allowed weekdays, and a start and end
time, and a window that crosses midnight is normal rather than an error.

Rollouts are staged: a **canary** host, then a first batch, then the rest. A
batch does not start until the previous one has hosts in `succeeded` and none
in `failed` beyond the tolerance. An offline host does not hold a rollout open
forever — it has its own deadline, after which it is `deferred` and the
rollout proceeds. A rollout that cannot finish is a rollout that gets paused
with a reason, not one that hangs.

The scheduler is a durable reconciliation loop, modelled on the existing metric
maintenance task: it reads state from the database, decides the next action,
and writes it back. Restarting the controller mid-rollout loses nothing.

---

## 8. Controller updates

The controller does **not** update itself. This is the firmest decision in the
document.

A web application that can replace its own container needs a Docker socket, and
a Docker socket in a web application is root on the host reachable through
whatever the next authentication bug is. There is no version of "mount
`/var/run/docker.sock` into the API" that is acceptable here, and no
unrestricted container management is exposed through the API.

There is also a plainer problem: a process cannot reliably supervise its own
termination. The step after "stop the controller" has to run somewhere that is
still running.

So: **an assisted workflow.** The controller detects the available release,
checks compatibility and migration requirements, and produces an *update plan*
— an ordered, human-readable list of the exact commands, with the pre-update
backup step first and the verification steps last. `scripts/controller-update.sh`
runs that plan on the host, outside the container, and is the only thing that
touches Compose. The administrator runs one command; the controller tells them
which one and what it will do.

### Migration rollback

Stated once, plainly, because getting this wrong loses data: **restoring an
older controller image does not reverse a database migration.** Alembic
downgrades exist for some revisions and are not a general recovery mechanism —
a migration that dropped a column cannot invent the data back. The supported
recovery from a failed controller upgrade is: restore the pre-update backup,
then run the older image. The plan takes that backup first and verifies it
before anything is stopped, and the documentation does not promise automatic
database rollback, because there is not one.

---

## 9. Offline hosts, concurrency, and recovery

| Situation | Behaviour |
| --- | --- |
| Host offline when a rollout starts | Instruction is created with an expiry; host is `queued`. If it connects in time, it proceeds; otherwise `deferred` and the rollout continues. |
| Host offline mid-update | The controller sees no progress. The instruction expires. The agent, on reconnect, reconciles its real state. |
| Controller restarts mid-rollout | Scheduler reconciles from the database. |
| Agent restarts mid-install | systemd restarts it; the agent finds `.previous` and a staged binary and reports the state it is actually in. Not every interrupted operation is automatically reversible, and the agent says which it is rather than guessing. |
| Two administrators both start a rollout | Advisory lock per host; the second gets a refusal naming the open job. |
| Disk full during download | Download is to a temporary file, bounded; failure is reported before anything is replaced. |

---

## 10. Authorization

Administrator only, and audited: enabling automatic updates, changing a
channel, approving an upgrade, starting or cancelling a rollout, changing a
pin, triggering a rollback, changing a maintenance window.

Operators and viewers may *read* update status — a viewer who cannot see that
the fleet is out of date cannot do their job — and may change nothing. This
matches how `hosts` and `monitoring` already split read and write routers.

No endpoint returns a credential, and no update response carries an internal
URL that is not already public release metadata.

---

## 11. The new dependency

`cryptography` is added to the server. It is the de-facto standard Python
cryptography library, installs from a manylinux wheel in under a second, and
the alternative is hand-rolling Ed25519 verification, which is not an
alternative. The agent gains no dependency: Go has `crypto/ed25519` in the
standard library.

---

## 12. Explicitly out of scope for this milestone

* **Fetching release metadata from the internet.** §3.
* **Controller self-replacement.** §8.
* **Automatic database downgrade.** §8.
* **Windows or macOS agents.** There are none.
* **Offline/air-gapped signed bundle import.** The signature design makes it
  straightforward later — a bundle is metadata, a signature and archives — and
  it is not needed to make updates work.
* **Publishing anything.** No tag, no release, no image push. The release
  pipeline is validated by dry runs and test inputs; where a path genuinely
  cannot be exercised without an authorized publication, it is documented as a
  blocker rather than claimed.

---

## 13. How this will be known to work

Not by assertion. The acceptance tests are:

1. **Two genuinely different versions.** A `0.1.0-test` and `0.1.1-test` agent,
   built from the same tree with different version stamps, proved different by
   their reported version and their digests — not the same binary twice.
2. **A real upgrade, end to end.** Old controller, old agent, enroll, collect
   telemetry, back up, upgrade the controller, migrate, upgrade the agent,
   verify connectivity, telemetry continuity, credential preservation,
   monitoring configuration preservation, and update history.
3. **A real failed upgrade**, with a rollback that restores service.
4. **A v1 agent against a v2 controller**, proving the fleet keeps working.
5. **Failure injection** across the list in the mission brief, each with a
   timeout so one hang cannot consume the CI budget.
