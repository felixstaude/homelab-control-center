# Contributing

Thank you for looking. This document covers how to propose a change, what the
quality gates are, and what a review will look for.

**The architecture is still settling.** Phases 0–3 are partly delivered and
the decisions underneath them are still moving. So: **open an issue before
writing code.** A pull request that arrives without a discussion is likely to
collide with something unbuilt, and rejecting work somebody already wrote is a
worse outcome for everyone than a short conversation first.

Small, obviously-correct fixes — a typo, a broken link, a wrong error message
— do not need that conversation. Send them.

---

## Reporting things

| What | Where |
| --- | --- |
| A bug | [Open an issue](https://github.com/felixstaude/homelab-control-center/issues/new/choose) using the bug template |
| A feature idea | An issue using the feature template — check [`docs/ROADMAP.md`](docs/ROADMAP.md) and [what this will never do](docs/PRODUCT_VISION.md#what-this-is-not) first |
| A security vulnerability | **Not** a public issue. See [`SECURITY.md`](SECURITY.md) |
| A question | [Discussions](https://github.com/felixstaude/homelab-control-center/discussions), or an issue if you are not sure |

When reporting a bug, the three things that make it actionable are: what you
expected, what happened, and the exact command or steps. Version and platform
matter for agent bugs — include `hcc-agent version` output and the
distribution.

### Things that are already known

Before filing, check [`docs/SECURITY.md`](docs/SECURITY.md#known-gaps) for the
known security gaps and [`docs/ROADMAP.md`](docs/ROADMAP.md) for what is
deliberately unbuilt. "Docker does not work" and "there is no alerting" are
correct observations about a pre-release, not bugs.

---

## Setting up

Full instructions, including troubleshooting, are in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). The short version:

```bash
make setup       # Python venv + npm install
make env         # .env files from the committed examples
make db-up       # PostgreSQL via Docker or Podman
make migrate
make bootstrap   # the initial administrator
```

You need Python 3.12+, Node 20.19+/22.12+, Go 1.24+, GNU Make, and Docker or
Podman.

---

## The gates

Run this before you open a pull request:

```bash
make check
```

That is formatting, linting, type-checking and the unit suites for all three
languages, plus the documentation and cross-language contract checks. It must
pass.

CI runs more than `make check` does, and you can run those locally too:

| Gate | Command | Needs |
| --- | --- | --- |
| Backend integration | `make test-integration` | `make db-up` |
| Real server over HTTP | `make smoke` | `make db-up` |
| Browser end to end | `make e2e` | `make db-up`, Playwright |
| Real agent, real `/proc` | `make monitoring-e2e` | `make db-up`, Go |
| Failure recovery | `make reliability-e2e` | `make db-up`, Go |

If you cannot run one of these, say so in the pull request rather than
guessing. CI will run them.

### Changes that need a regeneration step

Some artefacts are generated and checked for drift. CI fails if you change the
source without regenerating:

| If you change | Run |
| --- | --- |
| Any API route, request or response model | `make openapi` |
| The monitoring module registry in `hcc/monitoring/modules.py` | `make contracts` |
| A SQLAlchemy model | `make migration m="what it does"` |
| A vendored Go dependency | `go mod vendor` (must leave no diff) |

---

## What a review looks for

[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#conventions) has the per-language
conventions in full, and they are not advisory — several are enforced by
linters and tests. The ones that get raised most often:

1. **Never swallow an error.** Log it with context or propagate it.
2. **Unknown is a real state.** Missing data renders as *No data*, never as
   zero and never as healthy. This is the single most important rule in the
   project: a monitoring tool that reports a dead host as fine is worse than
   no monitoring tool.
3. **No credentials in code or in a command-line argument.** Configuration
   comes from the environment; secrets come from files read at the point of
   use. `/proc` makes argv readable by every local user.
4. **Unbuilt means labelled.** No placeholder that looks functional, no mock
   row that looks real. If a section does not exist, it says so and names the
   milestone.
5. **New behaviour has tests**, at the tier that can actually fail — a unit
   test for logic, an integration test for anything touching the database, and
   a real-process gate for anything crossing a language boundary.

### Architecture decisions

A change that settles a question rather than implementing a settled one needs
an **ADR** in
[`docs/ARCHITECTURE.md §13`](docs/ARCHITECTURE.md#13-architecture-decision-records).
Numbering is contiguous and checked by `make check-docs`.

A decision that contradicts an existing ADR must **supersede it explicitly** —
name the ADR, say why it was wrong or what changed. Quietly disagreeing with a
recorded decision is how a codebase ends up with two architectures.

### Scope

One concern per pull request. A refactor bundled with a fix makes the fix
unreviewable, and reverting it later takes the refactor with it.

---

## Commits and pull requests

Commit messages: a short subject in the imperative or descriptive mood, then a
body explaining **why** if it is not obvious. The history here is written to
be read — `git log` is the project's most accurate changelog, so a message
that says what the diff already says is a wasted line.

Pull requests should describe the change, say which gates you ran, and link
the issue discussing it. The template prompts for this.

Keep your branch rebased on `main`. Merge commits from `main` into a feature
branch make the history harder to read for no benefit on a project this size.

---

## Licensing

This project is [AGPL-3.0-or-later](LICENSE). By contributing, you agree that
your contribution is licensed under the same terms.

There is no CLA. You keep the copyright in what you write.

Be aware of what AGPL **§13** means if you deploy a modified version: anyone
using it over a network must be offered the source of your version. That
obligation is on the person running the modified service, not on this project.

---

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
