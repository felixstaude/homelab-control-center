# Security review: the installation platform — 2026-10-14

A focused review of what Operation Nightshift II added: the agent's
self-installer, the release artifact pipeline, the controller container image
and Compose deployment, the agent-release manifest, and the install
instructions the interface renders.

**Scope is the new attack surface.** The existing model —
[`SECURITY.md`](../SECURITY.md) — is the baseline and was not re-reviewed. The
question here is whether anything added since `093ce32` weakens it, and
whether the new code has its own problems.

**No secret values appear in this document.**

---

## 1. Summary

Six findings. Four were fixed in this run; two are accepted with stated
reasoning.

| # | Finding | Severity | Status |
| --- | --- | --- | --- |
| 1 | `ProcSubset=pid` in the systemd unit hid every file the agent reads | High (availability) | Fixed, with a test that starts the service |
| 2 | `credentials.Load` refused root reading a service-owned credential | Medium (diagnostics) | Fixed |
| 3 | Manifest values were interpolated into a root-shell command unvalidated | Medium (injection) | Fixed |
| 4 | The installer followed a symlink at the configuration directory | Low | Fixed |
| 5 | The database password file is mode `0644` | Low | Accepted, documented |
| 6 | `HCC_TRUSTED_PROXIES` defaults to three wide private ranges | Low | Accepted, documented |

No finding required revoking or rotating anything: nothing has been published,
no credential has been issued by a release, and no deployment of this code
exists outside a developer's machine and CI.

---

## 2. Findings

### 2.1 `ProcSubset=pid` hid the agent's entire data source — fixed

**What.** The committed systemd unit set `ProcSubset=pid`, which mounts
`/proc` with `subset=pid`. That hides every non-process file, including
`/proc/stat`, `/proc/meminfo`, `/proc/loadavg` and `/proc/uptime` — the whole
data source for all five modules that exist.

**Consequence.** The service starts, connects, reports itself healthy, accepts
a configuration, acknowledges it, and then fails every collection. A
monitoring agent that looks fine and measures nothing is the worst available
failure mode for this product, because the thing that would normally tell you
something is wrong is the thing that is broken.

**Why nothing caught it.** The unit had never been *started*. It had been
checked with `systemd-analyze verify`, which parses and does not execute, and
the agent had only ever been run in the foreground. The deployment README said
so honestly at the time: "the `systemd` service itself has not been installed
and started".

**Fix.** The directive is removed, with the reasoning in the unit file so it
is not reintroduced. `ProtectProc=invisible` stays — it hides other
processes' directories and does not affect the kernel's global files.

**How it is now caught.** `scripts/install-matrix.sh` starts the real service
in a real container, enables the `cpu` module, and asserts a row appears in
the database. Three distributions, every CI run. A unit test could not have
found this and a mocked systemd could not either.

### 2.2 `credentials.Load` refused root reading a service-owned credential — fixed

**What.** The owner check accepted a credential file owned by the reading
process's uid, or by root. `hcc-agent install` writes the credential as root
and then hands it to the `hcc-agent` account, which is what the hardened unit
needs — so a root-run `hcc-agent status` or `hcc-agent doctor` on a correct
installation was told the agent would refuse its own working credential.

**Consequence.** Not a disclosure. A *diagnostic* reporting a fault on
something that is right, which sends an operator to fix what is already
correct and erodes trust in the one command they have for diagnosis. It
affected shipped `hcc-agent status` as well as the new `doctor`.

**Analysis.** For root the check cannot prevent what it exists to prevent. The
question it answers is "could an unprivileged user have substituted this
file?", and the set of users who could is exactly the set who can write the
file or its directory — which the mode check and the directory check already
answer. Root can read the file whatever its owner, so the owner tells root
nothing further.

**Fix.** Root accepts any owner. An unprivileged reader still refuses a file
somebody else owns, which is the case the check was written for. The mode
check (`0o077`), the symlink refusal, the `O_NOFOLLOW` open and the
world-writable-directory refusal are all unchanged.

**Found by** the installation matrix, on a healthy installation.

### 2.3 Manifest values reached a root-shell command unvalidated — fixed

**What.** The interface renders an install command from the agent-release
manifest:

```
curl -fsSLO <url>
echo '<digest>  <archive>' | sha256sum -c -
tar xzf <archive>
sudo ./hcc-agent install --controller <origin>
```

`url` and `archive` come from the manifest. Neither was checked for characters
that mean something to a shell. A `;`, a backtick, a `$(`, or a `'` closing
the quoting around the digest line is command injection into a shell the
operator is about to paste into — and they will paste it as root.

**Reachability.** Low. The manifest ships inside the controller's own image,
so an attacker needs to have influenced the build. But "the value is trusted"
is precisely the assumption that makes a supply-chain compromise worth more
than it should be: the manifest's whole purpose is to be the thing the
operator trusts, and a compromised build should be able to substitute *bytes*
(which the digest then pins) rather than to run arbitrary commands on every
host in the fleet.

**Fix.** `hcc/agents/releases.py` refuses either field if it contains any of
`' " \` $ ; & | < > ( ) \` , whitespace or a NUL. Refused rather than escaped:
the interface would have to know which shell, and a release artifact whose
name needs quoting is a release artifact that should be renamed. Eleven
parametrised cases, plus one asserting a real GitHub release URL is still
accepted.

The URL is separately required to be `https`, except on loopback for a local
mirror.

### 2.4 The installer followed a symlink at the configuration directory — fixed

**What.** `ensureConfigDirectory` used `os.Stat` and then `MkdirAll`, `Chmod`
and `Chown`. All three follow a symlink, so a link planted at
`/etc/hcc-agent` pointing at, say, `/etc/ssh` would have the installer set
mode `0750` and ownership `hcc-agent` on somebody else's directory.

**Reachability.** Very low: planting a symlink in `/etc` needs root, and an
attacker with root has already won. The value of the fix is that the installer
stops being a tool that could be used to do it.

**Fix.** `Lstat`, and a refusal naming the link. A path that exists and is not
a directory is refused too. Two tests, one of which asserts the link's target
is still mode `0700` afterwards.

Every other write the installer performs already used
`O_CREATE|O_EXCL|O_NOFOLLOW` into a temporary file followed by `rename(2)`,
which is why this was the only instance.

### 2.5 The database password file is mode `0644` — accepted

**What.** `deploy/production/secrets/db_password` is `0644`, not `0600`.

**Why.** Three containers with three different uids read it: the generator
(root), PostgreSQL (uid 70 in the alpine image) and the control plane (uid
10001). A `0600` file owned by one of them is unreadable by the other two, so
`0600` here would not be stricter — it would be broken.

**What protects it instead.** The containing directory is inside the
deployment directory the operator owns, and the mount is read-only in both
consumers. This is the same guarantee Docker's own secret mounts give: files
under `/run/secrets` are mode `0444`.

**Residual.** Any process on the host that can read the deployment directory
can read the database password. That is also true of `.env` in every
Compose deployment in existence, and the alternative — a password in the
container's *environment*, which `docker inspect` prints and people paste into
issue reports — is worse.

### 2.6 `HCC_TRUSTED_PROXIES` defaults to three private ranges — accepted

**What.** `compose.yaml` presets it to `172.16.0.0/12`, `192.168.0.0/16` and
`10.0.0.0/8`, so forwarded headers are believed from any of them.

**Why.** On single-host Compose the reverse proxy is a sibling *container*,
whose address is assigned from the bridge network — which the operator does
not choose and cannot predict. Loopback is the wrong answer here and is
documented as such in `apps/server/.env.example`. Leaving it empty would make
every client collapse onto the proxy's single login-throttle budget, which is
the lockout the three-budget design exists to prevent.

**Residual.** Anything on those networks can forge `X-Forwarded-For` and
bypass per-address login throttling. The per-username and
per-address-per-username budgets still apply, so this widens the
brute-force surface rather than removing it.

`.env.example` and `DEPLOYMENT.md` both say to narrow it to the proxy's actual
address. It is not narrowed by default because a default that breaks the
common deployment is a default people work around by widening it further.

---

## 3. What was reviewed and found sound

Recorded so the next review knows what has already been looked at.

### Credential handling

| Channel | Verdict |
| --- | --- |
| `--token` flag | **Does not exist.** Asserted by a Go test and a Playwright test, both of which check the usage text. `--token-file` takes a path and is permitted. |
| Prompt | `/dev/tty` with `ECHO` cleared, `ICANON` and `ISIG` kept. Restored by a deferred function *and* by a signal handler, so Ctrl-C does not leave a terminal with echo off. |
| Environment | Supported and documented as second-best, with the `/proc/<pid>/environ` reason stated. |
| On disk | The installer never writes the enrolment credential. It goes from the terminal to one HTTPS request. |
| In output | Asserted absent from `install`, `install --dry-run`, `uninstall`, `doctor --text` and `doctor --json`, across a healthy installation and four broken ones. |
| In the environment file | Asserted absent. That file is `0644` and holds the controller URL; the credential is in its own `0600` file, which is what lets an administrator read the configuration without `sudo`. |

### Artifact integrity

* `scripts/verify-artifacts.sh` refuses an archive with an absolute path, a
  `..` component, a link, a non-zero uid, a wrong mode, a wrong ELF machine,
  a dynamically-linked binary, or a member that is not one of the two
  expected. A release tarball with any of those is a filesystem write
  primitive, and the install instructions tell somebody to run `tar xzf` as
  themselves.
* `--self-test` makes nineteen specific corruptions and requires each to be
  caught **by the check it is aimed at** rather than merely to produce a
  non-zero exit. A gate whose refusals have never fired is a gate nobody has
  tested.
* `hcc-agent upgrade` requires `--sha256` and has no flag that skips it.
  Extraction reads only a member named exactly `hcc-agent`, refuses a link,
  bounds the read against a decompression bomb, and checks the ELF machine
  before anything is swapped. Six tests, including a repacked archive whose
  bookkeeping was made consistent so the scenario reaches the check it is
  about.

### Privilege boundaries

* The installer runs as root and refuses otherwise. The service runs as a
  system account with uid below 1000 and a `nologin` shell; a *login* account
  of that name is refused rather than adopted, because running the service as
  somebody's user would give them read access to the credential.
* The unit has an empty `CapabilityBoundingSet=`, `NoNewPrivileges=yes`,
  `ProtectSystem=strict` with one `ReadWritePaths`,
  `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
  `MemoryDenyWriteExecute=yes` and a `@system-service` syscall filter. The
  installation matrix reads the service's uid out of `/proc` rather than
  trusting the unit file, and asserts it is not 0.
* The container runs as uid 10001 with a read-only root filesystem,
  `cap_drop: ALL` and `no-new-privileges`. The deployment scenario proves the
  root filesystem is read-only by trying to write to it.

### TLS

* No flag, variable or setting disables verification, in the agent or in the
  manifest reader. `HCC_AGENT_TLS_SKIP_VERIFY` was removed before this run and
  has not come back; a test asserts the usage text offers nothing like it.
* `--ca-file` is additive: the system roots stay trusted. The installation
  matrix exercises the whole internal-CA path — a real CA, a real certificate
  for a `.test` name, a real TLS terminator — because that is the most common
  real enrolment failure and it had no test at all.
* The installer verifies the controller's certificate **before** creating a
  user or writing a file, so a typo in a hostname leaves no debris. A test
  asserts no privileged operation ran when the probe failed.

### The API surface

* `GET /api/v1/agent-releases` is Administrator-only: anonymous 401, Viewer
  403, Operator 403. It is deliberately *not* under `/api/v1/agents`, where
  every route authenticates a per-host agent credential and refuses a browser
  session — one session-gated exception there would make that module's
  invariant false for one path.
* The contract test asserts no field on that endpoint matches
  `credential|token|secret|private|password`, so a future field holding a
  secret fails in the schema rather than in a browser.
* The web client's catch-all answers the application's own JSON 404 for
  anything under the API prefix or the operational probes. A 200 HTML page for
  `/readyz` would make a broken deployment report itself healthy.
* The fallback serves one fixed file and never a path from the request, which
  makes traversal structurally impossible rather than checked. `/assets` is
  Starlette's `StaticFiles`, which bounds every path inside the root; four
  traversal shapes are tested and assert no host file content in the body.
* Only `GET` reaches the client. A `POST` to an unknown path is a 405, so a
  client posting to the wrong URL does not see success.

### Supply chain

* No third-party GitHub Action is used anywhere. First-party actions are
  pinned to major tags, which is GitHub's own recommendation for actions it
  maintains.
* `release-prepare.yml` has **no** `pull_request` trigger, so a fork's code
  never reaches a workflow holding `id-token: write`. The provenance job is
  the only one with that permission and runs only for a tag whose dispatcher
  explicitly asked for it.
* Every other job is `contents: read`.
* The release workflow publishes nothing: no `gh release create`, no registry
  push, no `git tag`.
* The agent vendors one dependency and `go mod verify && go mod vendor && git
  diff --exit-code` already checks the vendored tree matches `go.mod`.

---

## 4. Checks performed

| Check | Result |
| --- | --- |
| `make check` | pass |
| `make test-integration` | 1029 passed |
| `go test -race ./...` (agents/linux) | pass, 14 packages |
| `./scripts/verify-artifacts.sh --self-test` | 19/19 corruptions caught by the intended check |
| `./scripts/install-matrix.sh --image debian:13` | 61 passed |
| `./scripts/install-matrix.sh --image fedora:42` | 61 passed |
| `E2E_REQUIRE_AGENT=1 make e2e` | 151 passed |
| `git diff 093ce32..HEAD` for credential-shaped literals | only test fixtures and a per-run throwaway |
| `git ls-files` for key, certificate and dump extensions | none (the hygiene job enforces it) |
| GitHub secret scanning | no alerts |

### The secret scan of this branch

Every match for a credential-shaped literal in the branch's diff is one of:

* A test fixture whose *name* says what it is —
  `THE-AGENT-CREDENTIAL-WHICH-MUST-NEVER-BE-PRINTED`,
  `ENROLMENT-CREDENTIAL-VALUE`, `TEST-ENROLMENT-CREDENTIAL-…`. Each exists so
  that a test asserting its *absence* from output cannot pass by coincidence.
* A per-run throwaway for a database that lives for minutes, generated from
  the clock and the pid, in a script that drops the database on every exit
  path. The same pattern the existing `monitoring-e2e.sh` uses.

No real credential is committed. `deploy/production/secrets/` is gitignored,
as is `deploy/secrets/`, and both were already covered before this run.

---

## 5. Known risks that this run did not change

Carried forward from [`SECURITY.md`](../SECURITY.md#known-gaps), restated
because the installation path makes two of them easier to hit.

* **No password reset.** A forgotten administrator password is unrecoverable
  by design. `hcc bootstrap-admin` does not reopen, so a lost password means
  a restore from backup or a new installation. The deployment documentation
  does not yet say this loudly enough.
* **First-to-present-it-wins enrolment.** A credential pasted on the wrong
  machine enrols the wrong machine, and the operator finds out when the right
  one fails. The install command being easy to paste makes this *more*
  likely, not less. The design document records the mitigation not taken — a
  pending state the administrator approves — and the two that are in place:
  single use, and a short window.
* **One controller process.** Login throttling is per process. The image
  exposes no worker knob, which is the enforcement, but nothing stops an
  operator running two containers behind one proxy.
* **The container does not defend against the Docker socket.** Anything that
  can talk to it can read the password file, the volume and the process's
  environment. That is inherent and is stated in `DEPLOYMENT.md`.

---

## 6. If this is reviewed again

Start with what has no test rather than with what does:

1. **`compose.tls.yaml` has never obtained a certificate.** Caddy's issuance
   needs a publicly resolvable name. What is tested is that the Caddyfile
   carries the directives the agent connection needs.
2. **No ARM hardware has run the agent.** `arm64` and `arm` are built on
   every commit and executed under `qemu-user-static`. Emulation is weaker
   evidence than hardware and the manifest's `tier` field says which is
   which.
3. **No Alpine.** busybox `adduser` is handled in the installer and is not in
   the matrix.
4. **`systemd-analyze security`** needs the unit installed on the host being
   scored and has not been run.
5. **Nothing has been published**, so the download-and-verify half of the
   install command has never been performed against a real URL, and
   `gh attestation verify` has never run against a real artifact.
