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

---

## Remaining work

Phases B–P. See the mission brief.

---

## Next task

Phase D — disk and filesystem monitoring, as the first of the real monitoring
modules, followed by Phase E (network) and then Phase K (the demo
environment), which is what makes the result something to actually try.
