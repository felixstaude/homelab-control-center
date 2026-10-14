# Security review: update management — 2026-10-14

A focused review of what Operation Evergreen added: the release signing and
verification path, the agent's update engine, the controller→agent update
protocol, the privileged installation unit, and the administrative surface.

**Scope is the new attack surface.** The existing model —
[`SECURITY.md`](../SECURITY.md) — is the baseline and was not re-reviewed, nor
was the installation platform reviewed in
[the previous review](2026-10-14-installation-platform-review.md). The question
here is whether anything added since `e8e4c59` weakens the model, and whether
the new code has problems of its own.

**No secret values appear in this document.** The test keypairs referenced are
generated per run into temporary directories and removed on every exit path;
the one deterministic key is seeded from the ASCII string
`hcc-contract-test-key-do-not-use` and is in the repository on purpose, so a
contract test produces identical bytes in two languages.

---

## 1. Summary

This feature takes a control plane reachable from a browser and gives it the
ability to cause code to be installed as root on every host in a fleet. That
is the largest privilege increase in the product's history, and the review
below is organised around the single question it raises: **what can an
attacker who owns the controller do?**

The answer is: withhold updates, offer an older signed release, and nothing
else. They cannot choose what bytes get installed.

Eight findings. Four were found by running the end-to-end scenario and are
fixed. Three are accepted with stated reasoning. One is a residual risk of the
design, stated rather than mitigated because mitigating it means not having
the feature.

| # | Finding | Severity | Status |
| --- | --- | --- | --- |
| 1 | The controller offered updates it had not authenticated | Medium | Fixed |
| 2 | The artifact download trusted only public roots | Medium (availability) | Fixed |
| 3 | A host with updates disabled went silent instead of declining | Low (diagnostics) | Fixed |
| 4 | `restarting` was reported after the restart that kills the reporter | Medium (availability) | Fixed |
| 5 | The agent account can trigger a root install | — | Accepted by design, bounded |
| 6 | An operator-configured key is added to the stamped trust store | Low | Accepted, documented |
| 7 | The staged archive is readable by the service group | Low | Accepted |
| 8 | No scheduler calls `/updates/expire`, so stale jobs persist | Low (availability) | Open, documented |

No finding required revoking or rotating anything: no release has been
published, no signing key exists outside a test run, and no deployment of this
code exists outside a developer's machine and CI.

---

## 2. The trust model, and what it is arranged against

### 2.1 What a compromised controller can do

The controller is the most likely thing to be compromised: it holds the
fleet's agent credentials, it is reachable from a browser, and it has an
authenticated administrative API. So the feature is designed on the assumption
that it will be.

A detached Ed25519 signature over the release manifest's **exact bytes**,
made by a key the publisher holds and the controller does not, is what
separates "which release" from "what the bytes are":

- The controller decides *whether* a host is offered an update and *when*.
- The **signature** establishes *what* the release consists of.
- The agent takes the expected SHA-256 out of the verified metadata, never
  from a field the controller could set.

An attacker with full control of the controller can therefore:

| Can | Cannot |
| --- | --- |
| withhold updates indefinitely | install a binary the publisher did not sign |
| offer an older *signed* release | forge metadata an agent accepts |
| cancel an in-flight update before `installing` | change a host's local policy |
| set a host's policy and window | make a host without `AUTO_UPDATE` act |
| see which versions a fleet runs | obtain the signing key, which it never holds |

"Offer an older signed release" is a real and unmitigated capability — a
rollback attack. It is bounded by the agent's own direction check: a downgrade
is refused unless the instruction carries `allow_downgrade`, which the
controller *can* set, so the bound is "an older release that was genuinely
published" rather than "no downgrade". Mitigating it properly needs a monotonic
version floor the agent remembers, which is on the list below as future work.

### 2.2 What a compromised publisher key means

Everything. A publisher key that leaks lets its holder install arbitrary code
as root on every host with automatic updates enabled and that key trusted.
This is the same property every signed software distribution has, and the
mitigations are the same: the key lives off the build machine, `keygen` writes
it mode 0600, and the trust store is a *list* so a replacement key can be
trusted alongside the old one during a rotation.

There is no revocation mechanism. A compromised key is handled by publishing
agents that do not trust it, which requires the compromised key to sign that
release — a bootstrapping problem this product does not solve and does not
pretend to.

### 2.3 Why the signature is detached and over exact bytes

Two JSON libraries do not agree on key order, integer rendering or non-ASCII
escaping. A signature over a re-serialised document would turn a formatting
difference into something indistinguishable from tampering — so the manifest
travels as a string through the protocol, is read from disk verbatim by the
controller, and is verified byte-for-byte by the agent. `signing.py` and
`release.go` are kept in step by a generated contract
(`contracts/release-signing.json`) that both sides replay.

### 2.4 Algorithm agility is absent on purpose

`ALGORITHM = "ed25519"`, one value, no negotiation. A verifier that accepts
"whatever the document says" accepts the weakest thing it knows, which is how
downgrade attacks on signature formats get a foothold. Adding a second
algorithm later means a new field value and a verifier that accepts exactly
two, decided by the verifier rather than by the document.

---

## 3. Findings

### 3.1 The controller offered updates it had not authenticated — fixed

**What.** With a manifest signed by a key the controller did not trust, the
overview correctly reported `untrusted_key` and the per-host plan reported
`update_available`. An administrator could press "Install now", and the agent
would decline a minute later with a message about signatures.

**Why it mattered.** Two reasons, and the second is the serious one.

The surface reason is a button that always fails. The deeper one: the plan is
the controller's statement about what is possible, and a controller that says
"an update is available" for metadata nothing can vouch for is a controller
making a claim it has not checked. An operator who did not read the signature
badge would reasonably conclude the update was fine and the host was broken.

**Fix.** `BlockerCode.METADATA_UNAUTHENTICATED`, raised as clause 0 of
`evaluate_compatibility`, with `plan_update(metadata_authentic=...)` defaulting
to **False** so a caller who has not established authenticity offers nothing.
It blocks a manual start as well as an automatic one, because the agent's
signature gate is unconditional — it has to be, since without a signature
there is nothing establishing what the bytes should be.

The fresh-install command is deliberately unaffected: there the digest travels
inside the administrator's own authenticated session with the controller and a
human verifies it with `sha256sum -c`. An unattended agent has neither.

**Found by.** `scripts/update-e2e.sh` step 15, which re-signs a manifest with
an untrusted key and asserts the plan is not actionable.

### 3.2 The artifact download trusted only public roots — fixed

**What.** `updater.httpFetch` used `http.DefaultClient`. An operator running
an internal CA — which is the documented and tested deployment — mirrors the
release archives behind the same CA, and the download failed with
`x509: certificate signed by unknown authority` after every other part of the
update had worked.

**Severity.** Availability, not confidentiality or integrity: the digest comes
out of signed metadata, so TLS on the download is defence in depth. But an
unreachable mirror is an unreachable mirror, and the failure mode — everything
verifies, then the fetch fails — is one an operator would spend a long time
on.

**Fix.** The downloader shares the control connection's trust via
`controller.NewTransport(caFile)`, wired from `HCC_AGENT_CA_FILE`. A CA file
that cannot be read is a specific error naming the file, deferred to the first
download because the constructor has no error to return.

### 3.3 A host with updates disabled went silent — fixed

**What.** `reconcileUpdate` logged at debug and asked for nothing when no
applier was configured. The reasoning in the comment was that an agent with
updates off should not have the controller record refusals nobody asked for.

**Why that was wrong.** The controller only mentions an instruction when a job
exists, and a job exists because an administrator pressed a button or a policy
fired. There is no "refusal nobody asked for". What there was instead: a job
sitting in `queued` for two minutes with no explanation, until the stale
sweeper — which nothing calls — would have taken it.

**Fix.** The agent reports `failed` with the reason, once per instruction per
session, **without fetching the instruction**: the reason does not depend on
its contents, because this host will decline whatever is in it. The reason
names `HCC_AGENT_AUTO_UPDATE` and the file it lives in, because the remedy is
the useful half of a diagnostic.

### 3.4 `restarting` was reported after the restart — fixed

**What.** `install.Upgrader` called `OnStep(StepRestarting)` after
`systemctl restart` returned. In the operator path that is harmless: the
upgrading process is a separate short-lived CLI invocation. In the
controller-driven path the upgrade runs **inside the service process**, so the
restart terminates the thing executing the function and the report never left.

**Why it mattered.** The job would hold `installing`, which the transition
table deliberately does not allow a reconnection to conclude —
`restarting → succeeded` is permitted and `installing → succeeded` is not,
because an agent that never said it was restarting might have died for an
unrelated reason. A stuck `installing` is recoverable by nothing but the
sweeper.

**Fix.** Reported before the call, with a test that fails if it moves back.
The worst case becomes a job that says "restarting" about a restart that
failed to start, and that path reports its own failure.

A second instance of the same class, found in the same run: the handoff's
reports were *queued* onto a buffered channel and the process was killed
before the session loop drained it. Frames whose delivery matters now carry an
acknowledgement channel and the reporter waits for the write, bounded at ten
seconds so a dead socket cannot stall an install.

### 3.5 The agent account can trigger a root install — accepted, bounded

**What.** `hcc-agent-update.path` watches
`/var/lib/hcc-agent/update-request.json`, a file the unprivileged service
account writes. So the agent account can cause a root unit to run at will.

**Why this is the design and not a defect.** The agent service runs
unprivileged, which is the single most important property of its unit. An
unprivileged process cannot replace a root-owned binary or restart its own
unit, so the alternatives were:

| Option | Why not |
| --- | --- |
| Run the agent as root | a long-lived network-facing root process, for a monitoring agent |
| A setuid helper | a new attack surface, and famously hard to get right |
| Polkit rules | per-distribution configuration, a surprising amount of it |
| A D-Bus service | a daemon to write and harden |
| A path-triggered one-shot | what was built |

**What bounds it.** The request carries the publisher's signed metadata and
its detached signature, **and no digest**. `apply-update` verifies that
signature against the key compiled into the binary and derives the expected
SHA-256 from the verified metadata. There is no digest field to forge, and
`TestThereIsNoDigestFieldToSupply` asserts the field is absent from the type
rather than merely rejected — so the privilege granted is exactly:

> cause root to install a genuinely published, publisher-signed release of
> this agent.

Not "run arbitrary code as root". An attacker who owns the agent account and
wants to run their own code as root through this channel needs the publisher's
signing key.

**Defence in depth, all tested:**

- The archive must be a regular file — `lstat`, so a symlink is refused rather
  than followed — inside the agent's own state directory. A traversing path, a
  relative path and a symlink are each refused with their own message.
- The request must be a regular file (again `lstat`) and not world-writable.
- Unknown JSON fields are refused: this is a privilege boundary, and ignoring
  something is how a check gets skipped.
- Every string is length-bounded before use; the file is bounded at 512 KiB.
- The schema version is checked, because an update that replaces the agent
  mid-flight means the request may be read by a different build than wrote it.
- The request is removed **before** anything is installed. `PathExists`
  re-triggers while the file is there, so a crash mid-install with the request
  present would be an install loop on a service that replaces its own binary.
- The unit drops capabilities to `CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
  CAP_SETUID CAP_SETGID`, sets `NoNewPrivileges`, `ProtectSystem=full`, and
  names three `ReadWritePaths`.
- `StartLimitIntervalSec=600` / `StartLimitBurst=3` as a backstop against the
  loop the removal is meant to prevent.

24 tests in `internal/install/handoff_test.go`, most of them refusals.

**Residual risk.** An attacker who owns the agent account can force an install
of an older signed release — the same rollback capability a compromised
controller has, from a different direction. Both are bounded by "genuinely
published" and neither is fully mitigated.

### 3.6 An operator-configured key is added to the stamped trust store — accepted

**What.** `HCC_AGENT_RELEASE_KEYS` in `/etc/hcc-agent/agent.env` adds keys to
the one stamped into the binary at build time.

**Why acceptable.** The file is root-owned mode 0644 and writing it needs root
on the host. An attacker with root on the host does not need a release
signature to install code. The case the setting exists for is real: an operator
who builds and signs their own agents would otherwise have to choose between
no automatic updates and patching the source.

**Why added rather than replacing.** An operator with their own key should not
have to give up the publisher's.

**Note.** `apply-update` — the privileged half — deliberately uses the
**stamped keys only**, not the environment. The privileged step trusts exactly
what the binary was built to trust, and nothing a configuration change could
add.

### 3.7 The staged archive is readable by the service group — accepted

`/var/lib/hcc-agent` is mode 0750 owned by `hcc-agent:hcc-agent`, and the
staged archive is 0640. Anything in the `hcc-agent` group can read a release
archive that is about to be installed.

A release archive is public by construction — it is published at a URL — so
there is nothing to protect. The group has exactly one member, created by the
installer with no login shell. Not world-readable anyway, because there is no
reason to offer the opportunity to watch for and swap a file whose digest is
checked.

### 3.8 No scheduler calls `/updates/expire` — open

`POST /api/v1/updates/expire` fails jobs whose instruction expiry has passed.
Nothing calls it. A job that reaches a non-terminal state and whose host never
comes back stays in flight, which blocks a new update on that host — the
partial unique index enforces one open job per host.

**Severity.** Availability, and recoverable by hand: an administrator can
cancel a job up to `staged`, and an operator can call the endpoint. Documented
in [`UPDATES.md`](../UPDATES.md#what-is-not-built) rather than left to be
discovered.

**Why not fixed here.** The scheduler is Phase D of the plan and is not built;
adding a bare timer for one endpoint would be the smallest useful part of it
and would have to be removed when the real one arrives.

---

## 4. What was checked and found sound

### 4.1 Nothing in the protocol is executable

`UpdateInstruction` carries an instruction id, a version, a platform, an
expiry, metadata, a signature and a downgrade flag. No command, no script, no
path to run. `test_no_message_type_carries_anything_executable` enumerates
every message type and asserts no field name resembles a command — and it
caught an edit that silently missed the five new frame types, which is why it
now reads the type union rather than a hand-written list.

`ParseServerFrame` errors on an unknown frame type rather than ignoring it, so
the new frames are gated on `UPDATE_PROTOCOL_VERSION` and a v1 agent is never
sent one. `TestAVersionOneAgentKeepsWorking` asserts a v1 agent handshakes,
receives configuration, and is never pointed at a queued update that genuinely
exists.

### 4.2 The expiry is enforced on the side that matters

An instruction carries `expires_at` and the **agent** refuses an expired one
as its first check, before anything is fetched. That is the side that matters:
the controller cannot reach a disconnected host to withdraw an authorization,
so a host that was offline for a day must not wake up and install last week's
decision. The controller enforces it too, which is bookkeeping.

### 4.3 The download is bounded before it is trusted

`MaxDownloadBytes` is checked **while reading**, not after: the size is
compared against the signed metadata as bytes arrive, so a response with a
lying `Content-Length` or no length at all cannot become the process's memory.
The digest is checked after the size, and both before anything is extracted.

### 4.4 https-only, with the loopback exception stated

The updater refuses to fetch from anything but `https://` or loopback. Not
because the transport protects the bytes — the signed digest does that — but
because an agent that would fetch over plaintext is an agent somebody will
point at a plaintext mirror and then reason incorrectly about. The same rule
the manifest reader enforces at parse time, so the failure is at build time
rather than at the first request.

### 4.5 No response carries a secret

`test_no_update_response_has_a_credential_shaped_field` walks every update
response against `credential|secret|private|password|token`. The update
surface has no secret to carry: a public key is configuration, a digest is
public, and the signed metadata is published.

### 4.6 The audit trail records the decisions, not the transitions

Six new actions: `update.policy_changed`, `update.started`,
`update.cancelled`, `update.downgrade_authorized`, `update.rollout_created`,
`update.rollout_controlled`. Deliberately absent: a row per state transition.
Those are in `update_events`, which is append-only with a `BIGINT GENERATED
ALWAYS AS IDENTITY` ordering key — because `now()` is the *transaction*
timestamp and two events written in one transaction shared it, which left the
log unorderable until the identity column replaced the UUID tie-break.

The audit vocabulary is a pinned test, so adding an action is a deliberate
change to a test rather than an edit nobody reviews.

### 4.7 The administrative surface is default-deny

Reads are open to every role; writes are administrator-only, enforced by the
router (`_admin.include_router`) rather than by a check inside each handler.
The interface hides the controls it knows will be refused, and the hiding is a
kindness rather than the boundary — `test_api_updates_integration.py` asserts
a viewer and an operator get 403 on every write.

### 4.8 No Docker socket, no container management, no shell

The brief's prohibitions were checked against the diff rather than assumed:

- `grep -r docker.sock` finds nothing new.
- The update surface has no endpoint that names a container.
- `apply-update` takes **no arguments** — every input comes from a file at a
  fixed path, so there is no flag an attacker who could influence the unit's
  invocation could use to redirect it.
- No scanner exclusion was added; `gosec`, `ruff` and `bandit` run unchanged.

---

## 5. Future work, in priority order

1. **A monotonic version floor on the agent**, so an older signed release
   cannot be forced by either a compromised controller or a compromised agent
   account. The agent would remember the highest version it has run and refuse
   to go below it without an explicit operator action. This is the main
   unmitigated risk in §2.1 and §3.5.
2. **A scheduler**, which closes §3.8 and makes maintenance windows and
   rollouts real rather than vocabulary.
3. **Transparency for the manifest** — a log an agent could check a release
   against, so withholding becomes detectable. Large, and probably not
   proportionate for a homelab product, but it is the honest answer to "a
   compromised controller can withhold updates indefinitely".
4. **Key rotation documentation**, including what an operator does when a
   publisher key is retired. The trust store is a list because rotation needs
   one; the procedure is not written down.

---

## 6. How this was verified

| Claim | Verified by |
| --- | --- |
| A real update installs and the host comes back | `scripts/update-e2e.sh` steps 12–13, 52 assertions, CI |
| A substituted archive is refused and the host keeps running | step 14 |
| Metadata signed by an untrusted key is refused by both halves | step 15 |
| A host with updates disabled declines and says why | step 10 |
| The event log holds states the host reported | step 13, asserted per state |
| The privileged side derives the digest from signed metadata | `TestTheDigestComesFromTheSignedMetadata` |
| There is no digest field to forge | `TestThereIsNoDigestFieldToSupply` |
| Symlinks, traversal and world-writable requests are refused | 6 tests in `handoff_test.go` |
| A build with no stamped key declines everything | 4 tests in `cmd/hcc-agent` |
| The two signature implementations agree | `contracts/release-signing.json`, replayed in Go and Python |
| Nothing in the protocol is executable | `test_no_message_type_carries_anything_executable` |
| No response carries a secret | `test_no_update_response_has_a_credential_shaped_field` |

Unit and integration: 1205 backend unit tests, 1094 integration tests against
real PostgreSQL, 585 frontend tests, Go race-clean across every package.
