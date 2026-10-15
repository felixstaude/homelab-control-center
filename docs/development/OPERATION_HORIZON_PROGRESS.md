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

### Phase A — secure the update system — starting

Mandatory, highest priority, and the reason it is first: Evergreen shipped a
privileged installer, and an installer with a gap is worse than no installer.

---

## Decisions

Recorded as they are taken, with the alternative that was rejected.

---

## Defects found

---

## Remaining work

---

## Next task

Phase A1 — the durable monotonic version floor.
