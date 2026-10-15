# Updates

How Homelab Control Center keeps its agents and itself up to date, what it
refuses to do, and what to look at when an update does not happen.

> **Current status.** Agent update management is **implemented and verified
> end to end**: `scripts/update-e2e.sh` installs `0.1.0-test` on a real
> systemd host in a disposable container, has the controller authorize an
> update, and watches the agent download, verify, install and restart itself
> as `0.1.1-test` — then proves that an archive substituted after signing is
> refused and the host keeps running. 52 assertions, run in CI on every push.
>
> **Controller updates are documented and manual.** There is no mechanism by
> which this controller replaces itself, by design — see
> [Updating the controller](#updating-the-controller).
>
> **Automatic policies and maintenance windows are live.** The controller
> evaluates every host's policy every five minutes by default, honours its
> maintenance window, and sweeps expired instructions. Setting
> `HCC_UPDATE_POLICY_INTERVAL_SECONDS=0` disables all of it, and the
> controller says so at startup.
>
> **Staged rollouts are not implemented.** The database carries the tables and
> the API carries the vocabulary, but nothing drives a canary or a batch: the
> policy pass starts every eligible host in one pass.
> [What is not built](#what-is-not-built) says so explicitly rather than
> leaving an administrator to find out.

## Contents

- [The shape of the system](#the-shape-of-the-system)
- [What establishes that a release is genuine](#what-establishes-that-a-release-is-genuine)
- [What stops a host going backwards](#what-stops-a-host-going-backwards)
- [Enabling automatic updates on a host](#enabling-automatic-updates-on-a-host)
- [Update policies](#update-policies)
- [Release channels](#release-channels)
- [Maintenance windows](#maintenance-windows)
- [Starting an update by hand](#starting-an-update-by-hand)
- [What happens during an update](#what-happens-during-an-update)
- [When an update fails](#when-an-update-fails)
- [Updating the controller](#updating-the-controller)
- [Publishing a release](#publishing-a-release)
- [Troubleshooting](#troubleshooting)
- [What is not built](#what-is-not-built)

## The shape of the system

Five parties, and the division of responsibility between them is the whole
design:

| Party | Decides | Cannot decide |
| --- | --- | --- |
| **The publisher** | what a release *is* — which bytes, for which architecture | whether any host installs it |
| **The controller** | *whether* and *when* a host is offered an update | what the bytes are |
| **The host's operator** | whether this host acts on an instruction at all | what gets installed |
| **The agent** | refusing, on four local grounds | anything the publisher did not sign |
| **The privileged unit** | performing one bounded install as root | which release that is |

The controller is deliberately demoted. It holds the fleet's credentials and
is reachable from a browser, so it is the most likely thing to be compromised
— and a compromised control plane that could choose what gets installed as
root on every host would be the worst outcome this product could have. It
authorizes; it does not vouch for bytes.

## What establishes that a release is genuine

A **detached Ed25519 signature over the release manifest's exact bytes**, made
by a key the publisher holds and the controller does not.

```
agent-releases.json       the manifest: versions, URLs, digests, constraints
agent-releases.json.sig   {"algorithm":"ed25519","key_id":"…","signature":"…"}
```

The agent carries the publisher's **public** key, stamped into the binary at
link time. When it receives an update instruction it:

1. verifies the signature over the metadata bytes it was sent, against that
   key;
2. reads the expected SHA-256 for its own architecture **out of** that
   verified metadata;
3. downloads the archive and hashes it.

So there is no step at which the controller, or the download origin, or
anything on the network, gets to say what the bytes should be. A checksum
fetched from the same place as a binary establishes nothing, and this system
never relies on one.

**A key is stamped in at build time, not configured.** `buildinfo.releaseKeys`
is set by `scripts/build-agent-artifacts.sh --trust-key`. A trust anchor that
lives in configuration is a trust anchor the configuration can be made to
change. An operator who builds and signs their own agents can add a key with
`HCC_AGENT_RELEASE_KEYS`, which is *added* to the stamped one rather than
replacing it, and which needs root on the host to set.

**A build with no stamped key trusts nothing and declines every update**, with
that as the reason. That is the state of every build of this repository that
is not an official release, and it is correct: such a binary genuinely cannot
tell a real release from a forged one.

The controller verifies the same signature independently, reports the result on
the Updates page, and **will not offer an update it cannot authenticate** — not
even for a manual start, because the agent would refuse it anyway and a button
that fails a minute later is worse than one that is absent.

| Status | Shown as | Automatic updates |
| --- | --- | --- |
| `valid` | Signed | permitted |
| `absent` | Unsigned | refused |
| `untrusted_key` | Untrusted key | refused |
| `invalid` | Signature invalid | refused |
| `malformed` | Signature unreadable | refused |
| `unsupported_algorithm` | Unsupported algorithm | refused |

Configure the key the controller trusts as a JSON array, like the CORS
origins:

```bash
HCC_RELEASE_SIGNING_KEYS=["SIzhD3FvCS6dVNTkzvyl74u7ICRaelCqbJNzZxHFkiY="]
```

A **public** key is configuration, not a secret. It is in the agent binaries
already.

## What stops a host going backwards

A signature says the publisher built these bytes. It says nothing about
*when*, and nothing about whether this is the version a host should be
running. The signature on 0.1.0 is as valid today as it was the day it was
made — so a controller that has been taken over cannot forge a release, and
can perfectly well offer a genuine old one from before a security fix.

Each host therefore keeps its own **version floor**: the oldest version it
will accept.

```
/var/lib/hcc-agent-update/version-floor.json     root:root, 0644
```

```json
{
  "schema_version": 1,
  "floor": "0.2.0",
  "highest_installed": "0.2.0",
  "last_instruction_id": "0b1d…",
  "updated_at": "2026-10-14T21:40:11Z"
}
```

### Three versions, and they are not the same number

| | What it means | Where it lives |
| --- | --- | --- |
| **Running version** | what the binary executing right now reports | the build stamp; `hcc-agent version` |
| **`highest_installed`** | the newest version this host has ever run to a successful, health-checked conclusion | the floor file |
| **`floor`** | the oldest version an update may target | the floor file |

They come apart in ordinary operation. After a rollback the running version is
*below* `highest_installed`, and that gap is the thing an operator needs to
see rather than something the data model should forbid. `floor` is separate
from `highest_installed` because lowering it is a legitimate root-authorised
act that must not erase the record of what has run.

### The rules

- **It rises only on success.** An install that is attempted and fails leaves
  the floor where it was. Raising it on an attempt would lock a host that
  failed to reach 0.2.0 out of 0.1.5 as well.
- **It never falls on its own.** Installing something older — which only root
  can authorise — records the instruction and leaves the floor alone.
- **No instruction field opens it.** `allow_downgrade` governs the step
  between the floor and the running version; it does not reach below the
  floor. A gate the controller can unlock is not a gate.
- **Absence is permissive.** A host installed before this existed has no
  floor and keeps updating normally. The first privileged update seeds the
  floor at *what is already running*, before it judges the request — so that
  update is protected, not the one after it.
- **A file that exists and is wrong is a refusal.** Unparseable, symlinked,
  not owned by root, writable by anyone but root, in a directory writable by
  anyone but root: all refused, loudly, with the path named. If "unreadable"
  meant "no floor", `echo garbage >` would be the bypass.

### Where it is enforced

In the **privileged** installer, which is the authority, and advisorily in the
agent beforehand so the controller's update history says *why* rather than
timing the instruction out. A compromised agent account can skip its own
check; it cannot skip root's, and it cannot write the floor file.

### Deliberately going back

Root on the host, and nowhere else:

```bash
sudo hcc-agent upgrade \
    --from hcc-agent_0.1.9_linux_amd64.tar.gz \
    --sha256 <digest> \
    --version 0.1.9 \
    --allow-downgrade
```

This lowers the floor to 0.1.9 and leaves `highest_installed` alone, so the
host still reports that it has run something newer. `--version` is required
with `--allow-downgrade`: lowering the floor means recording what it is being
lowered to, and the archive's own version is not read until after the floor
has been consulted.

### What this does not claim

Nothing here resists a compromised **root** account on the host. Root can
rewrite the floor, replace the binary, or remove the unit, and no file on that
machine can prevent it. The property defended is narrower and exact: a
compromised **controller**, or a compromised **unprivileged agent account**,
cannot move a host to an older release.

`hcc-agent doctor` prints the floor under `installation`.

## Enabling automatic updates on a host

**Off by default, and the installer does not offer to turn it on.** An
installer that enabled automatic updates would be signing a host up to have
its root-owned binary replaced without anybody saying so.

It is a per-host decision, made on the host, in a file the controller cannot
write:

```bash
sudo nano /etc/hcc-agent/agent.env
#   HCC_AGENT_AUTO_UPDATE=true
sudo systemctl restart hcc-agent
```

The agent logs what it decided on startup:

```
INFO controller-driven updates are enabled channel=stable pinned="" trusted_keys=1
```

or

```
INFO controller-driven updates are disabled on this host enable_with=HCC_AGENT_AUTO_UPDATE=true
```

With it false, the Updates page still shows that a newer release exists and an
administrator can still *start* an update — and the host declines it within a
heartbeat, with "automatic updates are not enabled on this host
(HCC_AGENT_AUTO_UPDATE is false in /etc/hcc-agent/agent.env)" recorded in the
history. Silence would be worse: somebody pressed a button and is owed an
answer.

The other three host-side settings, all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HCC_AGENT_UPDATE_CHANNEL` | `stable` | the most instability this host accepts |
| `HCC_AGENT_PINNED_VERSION` | unset | hold this host at one exact version |
| `HCC_AGENT_RELEASE_KEYS` | unset | extra publisher keys, base64, comma-separated |

An unparseable value for any of them is a **startup failure with a readable
message**, not a silent default. `HCC_AGENT_AUTO_UPDATE=yes` is refused rather
than read as false, because reading it as false would leave a host an
administrator believes is updating itself and is not.

## Update policies

Set per host, by an administrator, on the Updates page. This is the
controller's half: it decides whether to *send* an instruction. The host's
`HCC_AGENT_AUTO_UPDATE` decides whether to act on one. Both must agree.

| Policy | What the controller does |
| --- | --- |
| **Manual** | nothing automatically. The default. |
| **Notify only** | shows the update, starts nothing |
| **Automatic patch** | starts patch and pre-release steps without asking |
| **Automatic minor** | also minor steps |
| **Pinned** | holds the host at one version, whatever is published |

Policies are evaluated every `HCC_UPDATE_POLICY_INTERVAL_SECONDS` (default
300). **Setting it to 0 disables automatic updates entirely**: no host updates
itself whatever its policy says, and expired instructions are not swept. The
controller logs a warning at startup when it is off, because a deployment
where automatic updates are configured and silently never happen is the worst
available state.

The interval is also the precision of a maintenance window: a window of
02:00–04:00 starts an update within five minutes of 02:00, and a window
shorter than the interval may be missed entirely.

A policy makes at most three attempts at a given target version
(`MAX_POLICY_ATTEMPTS`), counted **per version** so a newer release gets a
fresh set — one bad release must not poison a host until somebody resets
something. A manual attempt does not count: an administrator retrying is a
human who has decided to, and the limit bounds automation rather than people.

**There is deliberately no option to install major versions automatically.**
A major version is the publisher saying something broke compatibly; a fleet
that crossed that boundary unattended would be a fleet nobody chose to
upgrade. The step size is measured on the version's core triple, so
`0.1.0 → 0.1.1-beta.1` is a *patch* step that also happens to be a
pre-release, and the policy checks both facts separately.

Whatever the policy says, an automatic update is also refused when:

- the release declares no protocol versions (unknown is not permission);
- the release speaks no protocol version this controller supports;
- the release needs a newer controller than this one;
- the release needs a database migration that has not been applied;
- the release publishes no archive for the host's architecture;
- the target is in a channel the host has not opted into;
- the release metadata is not authentically signed;
- it would be a downgrade, and nobody authorized one.

Every blocker is reported, not just the first. An administrator fixing one
wants to know about the second now rather than after another attempt.

## Release channels

Three, ordered from most to least stable: `stable`, `beta`, `alpha`.

A host's channel is **the most instability it accepts**, not an exact
selection. A host on `beta` still receives newer `stable` releases — which is
what somebody setting `beta` means, and the alternative would strand a beta
host on an old pre-release while a newer stable one existed.

A channel the agent does not recognise is treated as *less* stable than any it
does, so it is refused unless the host accepts everything. Failing closed is
the only safe direction for a value a newer publisher might introduce.

`recommended` — what the interface tells somebody to install on a fresh host —
can only be moved by a `stable` release. A beta that became the
recommendation would hand a pre-release to every new installation.

## Maintenance windows

Optional, per host. A window has a time zone, a start, an end, and a set of
weekdays (none selected means every day).

The time zone is **the host's window's zone, not the reader's**: an
administrator configuring 02:00 means their own 02:00, and the times mean what
was typed wherever somebody looks at them.

A window may cross midnight. `22:00–02:00` on Sunday is Sunday night, and is
matched **on the day the window started** — so an update at 00:30 on Monday is
inside Sunday's window, which is what somebody who wrote "Sunday night" meant.

A host waiting for its window shows **Deferred**, which is `neutral` and not a
failure. Conflating the two would make a healthy fleet look broken, which is
the single most likely way this interface could mislead somebody.

## Starting an update by hand

Updates → the host's row → **Manage** → **Install `<version>` now**.

The dialog names the step, not just the version: "a minor update. The host
picks it up on its next heartbeat and restarts its agent." "Install 0.2.0" and
"install 0.2.0, a minor update, over 0.1.4" are different sentences, and the
second is the one somebody needs before restarting a service on a machine they
care about.

A **downgrade** needs explicit authorization and is recorded separately in the
audit trail (`update.downgrade_authorized`).

**Cancel** is offered up to `staged` and then disappears. From `installing`
onwards the controller cannot stop the host — the binary is already being
replaced — so the interface stops offering a control it cannot honour, and
the explanation replaces it.

Every role sees every number on the Updates page. Only an administrator sees
the controls, and the server refuses the rest regardless: the hiding is a
kindness, not the boundary.

## What happens during an update

```
controller                      agent                     privileged unit
    |                             |                              |
    |-- heartbeat_ack ----------->|                              |
    |   (instruction_id)          |                              |
    |<------- update_request -----|                              |
    |-- update_instruction ------>|                              |
    |   signed metadata           | verify signature             |
    |<-- downloading -------------|                              |
    |                             | fetch over TLS, bounded      |
    |<-- verifying ---------------| digest vs signed metadata    |
    |<-- staged ------------------| /var/lib/hcc-agent/…         |
    |<-- installing --------------|--- update-request.json ----->|
    |<-- restarting --------------|                              | verify again
    |                             X  killed by the restart       | install, restart
    |<== reconnect, reporting the new version ===================|
    |   succeeded (controller)    |                              |
```

Three things in that diagram are load-bearing.

**The notification rides on the heartbeat acknowledgement, and the agent asks
for the instruction.** A notification is not evidence of anything; only an
instruction whose signature verifies says anything about what to install. The
same shape monitoring configuration already uses.

**The privileged unit exists because the agent service is unprivileged.** An
unprivileged process cannot replace a root-owned binary or restart its own
unit. So the agent does everything it can do safely — fetch, bound, hash,
verify — and then leaves a request that `hcc-agent-update.path` notices and
`hcc-agent-update.service` acts on as root.

The request carries the **signed metadata and no digest**. The privileged unit
verifies the signature itself and derives the expected hash from the verified
metadata, so a compromised agent account can cause root to install a genuinely
published, publisher-signed release and nothing else. See
[`docs/security/2026-10-14-update-management-review.md`](security/2026-10-14-update-management-review.md)
for the full argument.

**No process reports its own successful replacement.** The install ends with
`systemctl restart`, which kills the agent performing it. The states the agent
can report stop at `restarting`; the controller concludes the job when the
replacement connects — the target version means success, the previous version
means the agent's own rollback ran, and a third version means something
changed the host outside the update system.

Progress on the Updates page is derived from the state the agent last
reported. A stuck download shows a bar that does not move. A bar that advanced
on a timer would be inventing progress, and somebody would believe it.

## When an update fails

The host keeps working. That is the property everything else is arranged
around.

| State | What happened | What to do |
| --- | --- | --- |
| **Failed** | refused or could not be installed; nothing changed | read the reason in the history |
| **Rolled back** | installed, did not come up, previous binary restored | `journalctl -u hcc-agent` on the host |
| **Deferred** | waiting for a maintenance window | nothing; this is not a failure |
| **Cancelled** | an administrator stopped it before `installing` | nothing |

The reason is shown **in the history row**, not behind a click. "Why did this
fail" is the only question anybody asks of a history, and hiding the answer
one interaction away makes the table decorative.

The agent's own rollback is what restores service: `hcc-agent.previous` is kept
beside the installed binary, and the upgrader puts it back when the new binary
does not reconnect within its timeout. The credential is never touched by an
upgrade or a rollback — an update that required re-enrolment would not be an
update path.

A job that reaches a non-terminal state and never concludes is swept by the
policy pass, which expires instructions whose expiry has passed **before** it
evaluates anything — because an expired job holds the host's one open slot and
nothing else can start until it is cleared. `POST /api/v1/updates/expire` does
the same thing on demand.

## Updating the controller

**By hand, deliberately.** There is no mechanism by which this controller
replaces itself, and adding one is explicitly out of scope: a control plane
that could rewrite its own code is a control plane whose compromise is
permanent. The Updates page shows the running version and the applied database
revision so that an administrator knows what they have, and that is all it
does.

The procedure is in [`DEPLOYMENT.md`](DEPLOYMENT.md). In outline:

1. Take a backup (`scripts/backup.sh`). Verify it restores.
2. Pull the new image or the new source.
3. Run `alembic upgrade head`.
4. Restart the controller.
5. Check the Updates page: the version and the revision should both have
   moved.

Order matters: the controller is upgraded **before** its agents, not after. A
newer controller reads an older manifest and speaks an older protocol — the
asymmetry is deliberate, and `SUPPORTED_PROTOCOL_VERSIONS` and
`SUPPORTED_MANIFEST_SCHEMA_VERSIONS` are both wider than what this build
writes. The reverse is not supported.

A release that needs a migration says so in its manifest
(`schema_requirement`), compared against what the database actually reports
rather than against what the controller was built with. A host whose target
needs an unapplied revision is blocked with "run the controller's migrations
first" rather than being offered an update that would fail.

## Publishing a release

Three commands, and the key never touches a build machine.

```bash
# 1. Build the artifacts, stamping in the publisher's PUBLIC key.
./scripts/build-agent-artifacts.sh --version 0.2.0 \
    --trust-key "$(cat release.pub)"

# 2. Derive the manifest from the build's own artifacts.json.
./scripts/build-release-manifest.py \
    --artifacts dist/agent/artifacts.json \
    --base-url https://github.com/…/releases/download/v0.2.0 \
    --protocol-versions 1,2 \
    --min-controller 0.1.0 \
    --merge-into dist/agent-releases.json \
    --output dist/agent-releases.json

# 3. Sign it, somewhere the signing key lives.
./scripts/sign-release.py sign \
    --manifest dist/agent-releases.json --private-key release.key
./scripts/sign-release.py verify \
    --manifest dist/agent-releases.json --public-key release.pub
```

`--protocol-versions` is **required**. An empty list means "unknown", which
blocks every automatic update — a release built without it installs fine by
hand and is then silently never picked up by any host with an automatic
policy. Everything works, nothing happens, and nobody notices until a version
is checked by hand.

Make a keypair once:

```bash
./scripts/sign-release.py keygen --private-key release.key --public-key release.pub
```

The private key is written mode 0600 and is the only secret in this system
that matters. Keep it off the machine that builds releases: a build machine
that could sign would make the signature worth no more than the build machine.

`sign` verifies the signature it just wrote, against the key that made it, so
"it is signed" is never an untested claim.

Full release procedure, including the GitHub side:
[`RELEASING.md`](RELEASING.md).

## Troubleshooting

### The Updates page says "No release data"

No manifest is configured, so the controller does not know what versions
exist. This is the state of a source checkout: nothing has been published.
Point `HCC_AGENT_RELEASE_MANIFEST` at a manifest.

It is shown as `unknown`, not as "up to date", because a controller with no
release metadata has not performed a comparison — and showing that as up to
date would be the interface asserting something nothing checked.

### "Untrusted key" or "Unsigned"

The controller has metadata it cannot authenticate. Set
`HCC_RELEASE_SIGNING_KEYS` to the publisher's key, or find out who signed what
you have. Automatic updates are refused until this is resolved, and so are
manual ones — the agent would refuse them too.

### An update sits in "Queued" for ever

The agent is not asking for it. Check, in order:

```bash
systemctl is-active hcc-agent              # is it running at all
journalctl -u hcc-agent -n 50              # what did it decide on startup
grep AUTO_UPDATE /etc/hcc-agent/agent.env  # is it allowed to
```

A host with `HCC_AGENT_AUTO_UPDATE=false` declines within a heartbeat and the
history says so, so a job that stays *queued* means the agent is not connected
or not receiving acknowledgements.

### An update sits in "Installing" or "Restarting"

The privileged unit is where to look:

```bash
journalctl -u hcc-agent-update -n 50
systemctl status hcc-agent-update.path
ls -l /var/lib/hcc-agent/
```

A request file still present in `/var/lib/hcc-agent/` means the path unit is
not running — `systemctl enable --now hcc-agent-update.path`. The installer
enables it, and says so if it could not.

`update-outcome.json` in that directory is what the privileged unit last
reported, including the reason for a failure.

### "this build trusts no publisher key"

The agent binary was not stamped with a key. Builds of this repository are not
stamped unless `--trust-key` was passed, and such a binary cannot tell a real
release from a forged one. Either use an official release, or build your own
with your own key and add it with `HCC_AGENT_RELEASE_KEYS`.

### The host reports a version the controller cannot compare

Shown as **Version unknown**, rendered as the host reported it rather than
hidden. A development build reports `dev`, which is not a semantic version and
cannot be compared with published releases. Nothing is wrong; nothing can be
planned either.

## What is not built

Stated here rather than left to be discovered.

- **Staged rollouts.** `update_rollouts` and `update_rollout_members` exist in
  the database and the audit vocabulary has `update.rollout_created` and
  `update.rollout_controlled`, but **nothing drives a canary, a batch, a pause
  or a resume.** The policy pass starts every eligible host at once. The tables
  are the shape the scheduler will need; they are not a feature.
- **Backoff between retries.** A policy makes up to
  `MAX_POLICY_ATTEMPTS` (three) attempts at a given target version, counted
  per version so a newer release gets a fresh set. There is no *delay* between
  them beyond the evaluation interval, so three failures happen within about
  fifteen minutes rather than being spread out.
- **Canary ordering.** The policy pass starts every eligible host in one pass.
  It does not stage a fleet: one host, wait, then the rest.
- **Automatic controller updates.** Not planned. See
  [Updating the controller](#updating-the-controller).
- **Automatic database rollback.** Not implemented and not promised. A
  migration is reversible if its own `downgrade()` is, which is verified per
  migration in CI; nothing automates it as part of an update.
- **Windows or macOS agents.** Linux only.

## See also

- [`docs/plans/evergreen-update-architecture.md`](plans/evergreen-update-architecture.md)
  — the design document, including the threat model and what was ruled out
- [`docs/security/2026-10-14-update-management-review.md`](security/2026-10-14-update-management-review.md)
  — the security review of this feature
- [`docs/AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md) — the agent, the
  protocol and the installer
- [`docs/SECURITY.md`](SECURITY.md) — the security model this sits inside
- [`docs/RELEASING.md`](RELEASING.md) — the release procedure
