# Releasing

How a release is built, what is verified before it exists, and what a user can
check for themselves afterwards.

> **No release has been published.** There are no tags, no GitHub Releases and
> no container images in any registry. Everything below is implemented and
> exercised in CI against artifacts that stay inside the workflow run. The
> steps marked **manual** have therefore never been performed, and say so.

Related: [`ROADMAP.md`](ROADMAP.md) Phase 10,
[`plans/simple-installation-and-agent-distribution.md`](plans/simple-installation-and-agent-distribution.md)
§4 — the design this implements.

---

## 1. What a release consists of

| Artifact | Built by | Published by |
| --- | --- | --- |
| `hcc-agent_<version>_linux_amd64.tar.gz` | `scripts/build-agent-artifacts.sh` | manual |
| `hcc-agent_<version>_linux_arm64.tar.gz` | same | manual |
| `hcc-agent_<version>_linux_arm.tar.gz` | same | manual |
| `checksums.txt` | same | manual |
| `artifacts.json` | same | manual |
| `hcc-agent_<version>.sbom.spdx.json` | same | manual |

Archives are **flat**: they contain `hcc-agent` and `LICENSE` and no leading
directory, because the documented install command is `tar xzf …` followed by
`./hcc-agent install` and a leading directory would make that command wrong.

---

## 2. Versions

`VERSION` at the repository root is the single source of truth. Three other
files carry the same number and `scripts/check-versions.sh` — run by
`make check` and by CI's hygiene and artifact jobs — fails if any of them
disagrees:

* `apps/server/pyproject.toml`
* `apps/server/hcc/__init__.py` (what `GET /api/v1/info` returns)
* `apps/web/package.json`

A tag must match too. `./scripts/check-versions.sh --tag v0.2.0` fails against
a tree whose `VERSION` says `0.1.0`, and the release workflow runs exactly that
check before it builds anything.

Bumping a version is therefore four edits and one command:

```bash
$EDITOR VERSION apps/server/pyproject.toml apps/server/hcc/__init__.py apps/web/package.json
make check-versions
```

---

## 3. Building

```bash
make artifacts            # into dist/agent
make check-artifacts      # build, verify, and prove the verifier's refusals fire
make check-reproducible   # build twice, compare every digest
```

`scripts/build-agent-artifacts.sh` takes `--version`, `--output`, `--arch` and
`--clean`. It writes files and nothing else: there is no code path in it that
uploads, tags or publishes anything.

### Reproducibility

Two builds of one commit, with one toolchain, produce byte-identical archives.
`make check-reproducible` proves it and CI runs it on every change. What makes
it hold:

* `-trimpath` — no build-machine paths in the binary
* `CGO_ENABLED=0` — static, no host toolchain, works on glibc and musl alike
* `SOURCE_DATE_EPOCH` pinned to the commit date, used for the stamped build
  date *and* every archive mtime
* `tar --sort=name` with fixed uid, gid and mode
* `gzip -n` — no filename and no timestamp in the gzip header

What it does **not** claim: that a different Go version produces the same
bytes. It will not. `artifacts.json` records the toolchain that built the
artifacts, so a mismatch is diagnosable rather than mysterious.

---

## 4. Verification, before anything is published

`scripts/verify-artifacts.sh` is the release gate. It checks, for a directory
of artifacts:

| Check | What it catches |
| --- | --- |
| Manifest schema and field types | A consumer reading a shape it does not understand |
| Directory contents exactly match the manifest | A stale archive from a previous version uploaded by accident |
| `checksums.txt` matches the manifest and the files | A digest list that has drifted from what it describes |
| Archive digest and size | A corrupted or substituted archive |
| `gzip` header has no filename and no timestamp | A build that is not reproducible |
| Archive members are exactly `hcc-agent` and `LICENSE` | A stowaway file in a release tarball |
| No absolute paths, no `..`, no links, uid 0 | A release tarball that is a filesystem write primitive |
| Member mtimes equal `SOURCE_DATE_EPOCH` | A non-reproducible archive that still matched its digest |
| Binary mode is 755 | An agent that cannot be executed, or one anybody can rewrite |
| ELF class, endianness and `e_machine` per architecture | An archive labelled `arm64` carrying an `amd64` binary |
| ELF `e_type` is `ET_EXEC` | A build that picked up cgo and now needs a dynamic loader |
| `hcc-agent version` reports the manifest's version, commit and platform | A binary built from something other than what the manifest says |
| SBOM is SPDX 2.3 and describes this version | A stale SBOM carried over from a previous build |

`--execute` runs each binary: `amd64` natively on the runner, `arm64` and
`arm` under `qemu-user-static` from Ubuntu's own archive. That is the
difference between *cross-compiled* and *runs*, and it is as far as CI can
honestly go without hardware — see §6.

### The verifier is itself tested

`./scripts/verify-artifacts.sh --self-test` takes a real artifact set, makes
nineteen specific corruptions to a throwaway copy, and requires every one to
be rejected **by the check it is aimed at** rather than merely to produce a
non-zero exit. It runs in CI immediately after the real verification, because
a gate whose refusals have never fired is a gate nobody has tested.

---

## 5. The release workflow

`.github/workflows/release-prepare.yml`.

```
workflow_dispatch        a dry run, any branch, optional version override
push: tags v*            the candidate build, named by the tag
```

It builds, verifies, self-tests the verifier, executes every binary under
emulation, proves reproducibility, writes the checksums into the run summary,
and uploads the artifact set to the workflow run with a 14-day retention.

**It publishes nothing.** There is no `gh release create`, no registry push and
no `git tag` in it. Permissions are `contents: read` for every job except the
provenance job, which asks for `id-token: write` and `attestations: write` and
runs only when the trigger was a tag *and* the dispatcher explicitly asked for
attestation.

Deliberately not triggered by `pull_request`: a fork's pull request must never
reach a workflow that holds `id-token: write`. Per-change confidence that the
build still works comes from the `artifacts` job in `ci.yml`, which runs on
every pull request with `contents: read` and nothing else.

### Publishing — manual, and never performed yet

Once a tag's `release-prepare` run is green:

1. Download the artifact set from the run and verify it locally:
   `./scripts/verify-artifacts.sh --expect-version X.Y.Z /path/to/download`
2. Create the GitHub Release and attach the archives, `checksums.txt`,
   `artifacts.json` and the SBOM.
3. Write release notes that state the agent version floor if it moved.

Step 2 is a deliberate human act. A workflow that published on every tag would
mean a mistyped tag is a release, and a release cannot be unpublished from
somebody else's download.

---

## 6. What a platform claim means

`artifacts.json` carries a `tier` per architecture, and the words mean
specific things:

| Tier | What has actually happened |
| --- | --- |
| `supported` | Built, executed, and exercised by the installation matrix |
| `best-effort` | Built and executed under emulation; no hardware, no matrix |

| Architecture | Tier | Evidence |
| --- | --- | --- |
| `linux/amd64` | `supported` | Built and run natively on every CI run; the installation matrix installs and starts the service on it |
| `linux/arm64` | `supported` | Built on every CI run and executed under `qemu-aarch64-static`. **No ARM hardware has ever run it.** |
| `linux/arm` | `best-effort` | Built on every CI run and executed under `qemu-arm-static`, `GOARM=6` |

Cross-compilation succeeding is not a platform claim. Emulation is weaker
evidence than hardware and is labelled as such here rather than rounded up.

---

## 7. What a user can verify

The checksum path needs nothing but coreutils, which is the point — requiring
a verification tool on a NAS means nobody verifies anything:

```bash
echo '<digest>  hcc-agent_0.1.0_linux_amd64.tar.gz' | sha256sum -c -
```

The digest is rendered by the controller from the manifest it ships with, so it
arrives inside the TLS session the administrator is already in rather than from
the download origin. An attacker who controls the mirror cannot make a
substituted tarball match.

For anyone who wants more, and once a release has been published with
attestation:

```bash
gh attestation verify hcc-agent_0.1.0_linux_amd64.tar.gz \
  --repo felixstaude/homelab-control-center
```

That binds the artifact to the workflow that produced it through a Sigstore
signature recorded in a public transparency log. Keyless, so there is no
private key for a one-maintainer project to store, rotate or lose.

**Unverified:** `gh attestation verify` has never been run against a published
artifact, because none exists.
