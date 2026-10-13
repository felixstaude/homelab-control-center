# Simple installation and agent distribution

**Status: design. None of this is implemented.**

This document specifies how Homelab Control Center should be installed — the
controller with Docker Compose, and agents with a single command copied out of
the web interface. It exists to be argued with before any of it is built.

Nothing here changes the running system. Where it proposes a decision that
would outlive its implementation, that decision is marked **[ADR]** and
becomes a numbered record in
[`ARCHITECTURE.md §13`](../ARCHITECTURE.md#13-architecture-decision-records)
when the work lands — not before, because ADR numbering is contiguous and
reserving numbers for work that may never happen is how that invariant breaks.

Related: [`ROADMAP.md`](../ROADMAP.md) Phase 10 (10.3, 10.4, 10.5, 10.6, 10.8),
[`AGENT_ARCHITECTURE.md`](../AGENT_ARCHITECTURE.md),
[`SECURITY.md`](../SECURITY.md),
[`agents/linux/deploy/README.md`](../../agents/linux/deploy/README.md) — which
documents the manual procedure this is meant to replace.

---

## 1. What "installed" should mean

### The controller

```bash
mkdir hcc && cd hcc
curl -fsSLO https://github.com/felixstaude/homelab-control-center/releases/latest/download/compose.yaml
curl -fsSLO https://github.com/felixstaude/homelab-control-center/releases/latest/download/.env.example
cp .env.example .env
$EDITOR .env                 # one required field: the URL browsers will use
docker compose up -d
docker compose exec hcc hcc bootstrap-admin
```

Six commands, one of which is an editor, and **one required setting**.
Everything else has a working default or is generated.

### An agent

1. Open the web interface, **Hosts → Register host**.
2. Press **Issue enrolment credential**.
3. Copy the install command. Copy the credential — separately, with its own
   button.
4. On the machine to be monitored, paste the command. Paste the credential
   when it asks.
5. The host goes **Connected** within a second or two.
6. Nothing is being collected, because nothing is enabled. The administrator
   turns modules on afterwards, deliberately.

Step 6 is not a gap in the flow. It is the
[default-deny rule](../MONITORING_MODULES.md), and the installer must not
quietly improve on it.

---

## 2. Design constraints, and the one real conflict

The requirements that shape everything below:

| Constraint | Why |
| --- | --- |
| No Go toolchain on the target | The target is a NAS, a Pi, a VPS. Installing a compiler to run a monitoring agent is absurd. |
| Prebuilt `linux/amd64` and `linux/arm64` | Covers x86 servers and every Pi 3 onwards. `linux/arm` already cross-compiles in CI and can ship as a third tier. |
| Artifacts verified before execution | A monitoring agent runs as a persistent system service on every machine you own. It is a supply-chain target with unusually good reach. |
| Dedicated unprivileged user, restrictive permissions, hardened unit | The agent needs no capability at all for the modules that exist. |
| Custom CAs supported; TLS verification never disabled | Homelabs use internal CAs. The answer is to trust the CA, never to skip the check. |
| Enrolment credential never in argv, shell history or logs | `/proc` makes argv world-readable. History files survive. Logs get shipped somewhere. |
| No piping an unaudited remote script into a root shell | — see below |
| Upgrade, uninstall, diagnostics | Software you cannot remove cleanly is software people do not install. |
| Modules off after install | Consent is per module. |

### The conflict

**"Copy a simple installation command"** and **"never pipe an unaudited remote
script into a root shell"** pull against each other. The reason
`curl … | sudo bash` is everywhere is that it is genuinely the shortest path
from a web page to a running service.

It is also three separate problems at once:

1. The script is executed as root without ever being seen.
2. Whatever authenticates the install usually arrives as `-s -- --token XYZ`,
   which is argv, which is world-readable on the box.
3. The whole command lands in `~/.bash_history`, credential included.

The design below solves all three without giving up the single-paste
experience. The mechanism is in §5.

---

## 3. The controller: Docker Compose

### 3.1 What is published per release

| Artifact | Contents |
| --- | --- |
| `ghcr.io/felixstaude/homelab-control-center:<version>` | Multi-arch (`amd64`, `arm64`) control plane image, web client built in |
| `compose.yaml` | The deployment, pinned to that exact image tag |
| `.env.example` | Every setting, documented, with the one required field at the top |

Tags are `<major>.<minor>.<patch>`, plus a moving `<major>.<minor>` and
`latest`. **`compose.yaml` pins an exact version**, never `latest` — a
`docker compose up -d` that silently jumps a major version during an unrelated
restart is how an upgrade happens at the worst possible moment. Upgrading is
editing one line, which is also what makes it reviewable and revertible.

### 3.2 Image

Multi-stage: Node builds the client, Python installs the backend, the final
stage carries neither toolchain.

* Runs as a **non-root** uid, with a read-only root filesystem and a `tmpfs`
  for `/tmp`.
* No shell is required at runtime, but one is kept: `docker compose exec hcc
  hcc bootstrap-admin` is the documented first-run step and
  [ADR-033](../ARCHITECTURE.md#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint)
  makes a local command the *only* way to create the first administrator.
  That remains true in a container — `exec` into the container is the local
  command.
* `HEALTHCHECK` hits `/readyz`, which already reports database reachability.
* The client is served by the control plane itself, same-origin, which is what
  the CORS and CSRF model assumes.
* **One worker**, because login throttling counters are per process
  ([ADR-029](../ARCHITECTURE.md#adr-029-in-process-rate-limiting-plus-a-semaphore-on-argon2),
  [ADR-046](../ARCHITECTURE.md#adr-046-one-controller-process-stated-rather-than-enforced)).
  The image must not expose a worker-count knob that silently breaks a
  security control. When per-process limiting is replaced, the knob arrives
  with it.

### 3.3 Topology

```
            :443
              │
      ┌───────▼────────┐
      │ reverse proxy  │   TLS terminates here. Caddy in the documented
      │   (optional)   │   default; any proxy works.
      └───────┬────────┘
              │ http, bridge network
      ┌───────▼────────┐        ┌──────────────┐
      │      hcc       │───────▶│   postgres   │
      │  (one worker)  │        │  named volume│
      └────────────────┘        └──────────────┘
```

Postgres publishes **no port to the host**. The controller publishes one only
when running without the bundled proxy.

Two documented variants, because the homelab split is real: people who want
TLS handled for them, and people who already run Traefik or nginx and want
one more backend.

* **`compose.yaml`** — controller + database. Publishes `127.0.0.1:8000`.
  Expects an existing proxy.
* **`compose.tls.yaml`** — an overlay adding Caddy, which gets a certificate
  automatically from Let's Encrypt given a public DNS name, or serves an
  internal CA's certificate from a mounted path.

### 3.4 Configuration: one required field

`.env` carries exactly one thing the user must decide:

```ini
HCC_PUBLIC_ORIGIN=https://hcc.example.com
```

This is already mandatory-in-effect — startup fails without it or
`HCC_TRUSTED_PROXIES`, deliberately, because the application otherwise cannot
know which origin browsers use and the CSRF `Origin` check has nothing to
compare against. Putting it first in `.env.example` with that explanation
converts a confusing startup failure into a filled-in field.

`HCC_TRUSTED_PROXIES` is preset to the Compose bridge network in the shipped
file, with the comment that already exists in
[`apps/server/.env.example`](../../apps/server/.env.example) explaining why
loopback is the wrong answer here.

### 3.5 Secrets: generated, not chosen

The database password must not be a documented default, and must not be a
field the user is asked to invent.

**Proposal [ADR]: the Compose stack generates its own database credential on
first start, into a file the user never edits.**

```yaml
services:
  postgres:
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets: [db_password]

secrets:
  db_password:
    file: ./secrets/db_password
```

`./secrets/db_password` is created by a tiny `hcc-init` one-shot service that
writes 32 random bytes, base64, mode `0600`, **only if the file does not
exist**. Idempotent, so it is safe on every `up`.

Why a file rather than an environment variable: a container's environment is
readable by anything that can talk to the Docker socket and shows up in
`docker inspect`, which people paste into issue reports. A file does not.

The alternatives, and why not:

* *Ask the user to invent one.* They will reuse something, or pick
  `postgres`, and the documentation gets a weak password baked into every
  copy of it.
* *Ship a default.* No.
* *Generate into `.env` with a script.* Workable, but then the secret is in
  the file the user is told to edit and will paste into a support thread.

There is **no application signing secret to manage**. Sessions are
server-side and the cookie carries an opaque random token of which only the
SHA-256 digest is stored
([ADR-027](../ARCHITECTURE.md#adr-027-session-tokens-are-hashed-with-sha-256-not-argon2),
[ADR-028](../ARCHITECTURE.md#adr-028-server-side-sessions-not-jwt)).
Nothing is signed, so nothing needs a key, so there is no key to rotate or
leak. That property is worth protecting in review.

### 3.6 Migrations

**Proposal [ADR]: migrations run automatically on container start, before the
application binds a port.**

The entrypoint runs `alembic upgrade head`, then execs uvicorn. If the
migration fails, the container exits non-zero and never serves traffic.

This is safe *here* specifically because one controller process is the rule
([ADR-046](../ARCHITECTURE.md#adr-046-one-controller-process-stated-rather-than-enforced)),
so there is no second replica racing the same migration. A deployment that
ever becomes multi-replica must move this to a separate one-shot service —
and that is the point at which the ADR gets superseded, not quietly violated.

`HCC_AUTO_MIGRATE=false` exists for operators who want to run
`docker compose run --rm hcc alembic upgrade head` themselves and watch it.
The documented upgrade path mentions it; the default does not require it.

Alembic already enforces the parts that matter: CI verifies every migration
applies to an empty database, reverses, and that no model change is missing a
migration.

### 3.7 Persistence and backup

One named volume, `postgres-data`. Nothing else in the stack holds state — the
control plane owns all of it in the database, which is an existing
architectural property rather than a convenience.

Documented backup, which is just `pg_dump`:

```bash
docker compose exec -T postgres pg_dump -U hcc -Fc hcc > hcc-$(date +%F).dump
```

Restore is the inverse into a fresh volume. Both get a tested procedure
(roadmap 10.5) rather than a hopeful paragraph; "documented, tested" means a
script in `scripts/` that CI runs against a throwaway stack.

What is explicitly **not** backed up by that command: `./secrets/db_password`
and `.env`. The documentation must say so, because a restore that cannot
authenticate to its own database is the predictable failure.

### 3.8 Upgrades

```bash
docker compose exec -T postgres pg_dump -U hcc -Fc hcc > pre-upgrade.dump
$EDITOR compose.yaml          # bump the pinned tag
docker compose pull
docker compose up -d
```

Rules that make that reliable:

* **Migrations are forward-only in practice.** Every migration has a
  `downgrade` and CI proves it runs, but a downgrade that drops a column
  destroys data. The upgrade documentation says: to go back, restore the
  dump. It does not advertise `alembic downgrade` as a rollback strategy.
* **One minor version at a time** across a major boundary; within a major,
  any-to-any. Stated in the release notes, and checkable because the
  controller records its own schema revision.
* **The controller refuses to start against a newer schema than it knows.**
  Rolling the image back without restoring the database should fail loudly
  rather than run queries against columns it does not understand.
* **Agents keep working across a controller upgrade.** The protocol is
  versioned (`v1`) and the agent reconnects with backoff; a controller
  restart is already exercised by `make reliability-e2e`. An upgrade that
  requires touching every host is not an upgrade path, and the release notes
  must call out the agent version floor when one moves.

---

## 4. Agent distribution

### 4.1 Release artifacts

Built by a tagged GitHub Actions workflow, attached to a GitHub Release:

```
hcc-agent_<version>_linux_amd64.tar.gz
hcc-agent_<version>_linux_arm64.tar.gz
hcc-agent_<version>_linux_arm.tar.gz      # third tier, best effort
checksums.txt                              # sha256, all artifacts
checksums.txt.sigstore                     # cosign keyless bundle
hcc-agent_<version>.sbom.spdx.json
```

Build flags: `CGO_ENABLED=0`, `-trimpath`, version/commit/date stamped through
the existing `internal/buildinfo` package, `SOURCE_DATE_EPOCH` pinned to the
tag's commit date. Static binaries, so one artifact per architecture works on
glibc and musl alike, which matters for Alpine-based NASes.

The agent already cross-compiles for all three targets in CI; this is
packaging that, not new portability work.

### 4.2 Signing

**Proposal [ADR]: sign with Sigstore/cosign keyless, and publish SLSA
provenance.**

Keyless signing binds the signature to the GitHub Actions workflow identity
through an OIDC token, with the record in a public transparency log. The
decisive property for a one-maintainer project is that **there is no private
key to store, rotate, or lose** — a long-lived GPG key kept on a laptop is a
worse guarantee than it looks, and a lost one is unrecoverable.

Verification, for anyone who wants it:

```bash
cosign verify-blob \
  --bundle checksums.txt.sigstore \
  --certificate-identity-regexp '^https://github\.com/felixstaude/homelab-control-center/\.github/workflows/release\.yml@refs/tags/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  checksums.txt
```

That command is documented, but it is **not** on the critical path for a
normal install, because requiring `cosign` on a NAS to install a monitoring
agent would mean nobody verifies anything. §4.3 is how the normal path gets a
real guarantee without it.

### 4.3 The controller is the trust anchor

This is the central idea of the whole design.

The administrator is already authenticated to the controller over TLS. They
trust it — it is the thing holding their fleet's credentials. So the
controller, not the download origin, should be what vouches for the agent
binary.

**Proposal [ADR]: the controller image ships a manifest of the agent releases
it was built alongside, and the install command carries the expected digest.**

```json
{
  "recommended": "0.3.0",
  "minimum_supported": "0.2.0",
  "releases": {
    "0.3.0": {
      "linux/amd64": { "url": "https://github.com/…/hcc-agent_0.3.0_linux_amd64.tar.gz",
                       "sha256": "9f2c…" },
      "linux/arm64": { "url": "…", "sha256": "4ab1…" }
    }
  }
}
```

Baked into the image at build time from the release workflow's own
`checksums.txt`. Three consequences, all good:

1. **No runtime dependency on GitHub for trust.** The controller does not
   fetch checksums when asked; it already has them.
2. **The expected digest travels inside the TLS session the admin is already
   in.** An attacker who controls the download origin, or the network between
   the host and GitHub, cannot make a substituted tarball match.
3. **Air-gapped and mirrored installs work.** Point `url` at an internal
   mirror; the digest still pins the bytes.

A new endpoint, `GET /api/v1/agents/install-manifest`, returns it to the
interface. Authenticated and Administrator-only — it reveals the controller's
version and what it expects to talk to, which is not something an anonymous
caller needs.

---

## 5. The installer

### 5.1 Rejected designs

| Design | Why not |
| --- | --- |
| `curl … \| sudo bash -s -- --token XYZ` | All three failures at once: unaudited code as root, token in argv, token in history. |
| `curl … \| sudo bash` with an interactive prompt | Fixes the token. Still executes unseen code as root, and a script reading a prompt while *itself* arriving on stdin is a pipeline bug waiting to happen. |
| Download script, verify, read it, run it | Correct, and five commands. Also verifies the **script**, which then downloads the **binary** — two artifacts to trust where one would do. |
| A `.deb`/`.rpm` only | Right answer eventually (roadmap 10.6), wrong first answer: it needs a repository, a signing key, and per-distribution packaging, and leaves out Alpine, Arch and every NAS firmware. |
| `go install` | Requires Go on the target. Excluded by constraint. |

### 5.2 Chosen design: the binary is its own installer

**Proposal [ADR]: no installation shell script exists. `hcc-agent` grows
`install`, `upgrade`, `uninstall` and `doctor` subcommands, and the thing you
verify is the thing you run.**

The artifact the user checksums is the artifact that performs the
installation and then *is* the installed program. There is no second thing to
trust. A shell script's entire job — create a user, place a binary, write a
unit, set modes — is work the agent can do for itself, in a language with
types, tests and no quoting rules.

This also deletes an entire class of bug. The current manual procedure in
`agents/linux/deploy/README.md` is correct and about forty lines of shell that
every operator retypes slightly differently.

### 5.3 What the user copies

The interface shows **two** things, with separate copy buttons, and explains
why they are separate.

**The command** — no secret in it, safe in history, safe in a chat message
while asking a colleague to run it:

```bash
curl -fsSLO https://github.com/felixstaude/homelab-control-center/releases/download/v0.3.0/hcc-agent_0.3.0_linux_amd64.tar.gz
echo '9f2c…  hcc-agent_0.3.0_linux_amd64.tar.gz' | sha256sum -c -
tar xzf hcc-agent_0.3.0_linux_amd64.tar.gz
sudo ./hcc-agent install --controller https://hcc.example.com
```

Four lines, each of which does one comprehensible thing. The digest is
rendered by the controller from §4.3, for the architecture the user picked
from a dropdown. Nothing is piped into a shell. `sha256sum -c -` **fails
non-zero**, and because these are separate commands rather than a pipeline, a
mismatch stops the paste before the `tar`.

**The credential** — copied separately, pasted into a prompt, never on a
command line:

```
HCC-ENROL-7K2M-… (shown once, expires in 15 minutes, single use)
```

The install command pauses and asks:

```
Enrolment credential (paste, input hidden):
```

Read from `/dev/tty`, not stdin, with echo disabled. Not in argv. Not in the
environment. Not in `~/.bash_history`. Not in the systemd journal.

### 5.4 Why a prompt is the right primitive

| Channel | Leaks to | Verdict |
| --- | --- | --- |
| `--token XYZ` | Every local user, via `/proc/<pid>/cmdline` | Refused by the agent today, and stays refused |
| `HCC_…_TOKEN=XYZ cmd` | `/proc/<pid>/environ`; inherited by children | Supported for automation, documented as second-best |
| Heredoc into a file | Shell history | What the manual procedure does today |
| `--token-file /path` | Nothing, if modes are right | The automation path |
| **Prompt on `/dev/tty`** | **Nothing** | **The default** |

Reading `/dev/tty` rather than stdin is deliberate: it keeps working when the
command is part of a pipeline, and it makes the prompt impossible to satisfy
accidentally from a redirected file — which is the behaviour you want from
something that is about to mint a long-lived credential.

For configuration management, where no terminal exists:

```bash
sudo hcc-agent install \
  --controller https://hcc.example.com \
  --token-file /run/secrets/hcc-enrolment \
  --non-interactive
```

Ansible, cloud-init and the like write the file with mode `0600`, run this,
and delete it. `--non-interactive` makes the absence of a terminal an error
rather than a hang.

### 5.5 What `install` does

Ordered, and each step a refusal rather than a warning when it cannot be done
safely:

1. **Preflight.** Linux; `/proc` readable; systemd present; root; architecture
   matches the binary. Refuse early, with the reason.
2. **Validate the controller URL.** `https`, except loopback. Resolve it,
   open a TLS connection, verify the chain — *before* creating users or
   writing files, because a typo in the hostname should not leave debris.
   `--ca-file` is read here and copied to `/etc/hcc-agent/ca.pem` (`0644`,
   root-owned — a CA certificate is public).
3. **Create the system user.** `hcc-agent`, system account, no home, shell
   `/usr/sbin/nologin` or `/sbin/nologin` by distribution. Reuse it if it
   already exists and is a system account; refuse if a *login* user of that
   name exists rather than silently adopting somebody's account.
4. **Place the binary.** `/usr/local/bin/hcc-agent`, root-owned, `0755`.
   Written to a temporary file in the same directory and `rename(2)`d, so
   there is no window where it is half-written — the same discipline the
   agent already uses for its configuration file.
5. **Create the state directory.** `/etc/hcc-agent`, owned `hcc-agent`, mode
   `0750`.
6. **Prompt for the credential and enrol** (§5.3), as the `hcc-agent` user,
   not as root. The long-lived credential lands at
   `/etc/hcc-agent/credential`, mode `0600`, owned by that user. The agent's
   existing refusal to read a credential file that is group-readable, a
   symlink, owned by somebody else, or in a world-writable directory applies
   unchanged.
7. **Write the unit** to `/etc/systemd/system/hcc-agent.service` — the
   hardened one from `agents/linux/deploy/README.md`, which the installer
   now owns so it stops being copy-pasted. `daemon-reload`, `enable --now`.
8. **Wait for the connection,** up to a timeout, and report it. "Installed"
   should mean "connected", not "files placed". If it does not connect, print
   what `doctor` would say rather than exiting 0 and leaving a silent box.
9. **Say what is not happening.** The final line states that no module is
   enabled and that collection starts when an administrator turns one on.

The enrolment credential is never written to disk by the installer at all. It
goes from the terminal to one HTTPS request.

### 5.6 The unit

The installer writes the unit from
[`agents/linux/deploy/README.md`](../../agents/linux/deploy/README.md)
verbatim — `ProtectSystem=strict` with a single `ReadWritePaths`, an empty
`CapabilityBoundingSet=`, `NoNewPrivileges`, `MemoryDenyWriteExecute`,
`SystemCallFilter=@system-service`, `PrivateTmp`, `PrivateDevices`,
`ProtectHome`, `RestrictNamespaces`, `LockPersonality`.

Two things that must change once an installer owns this:

* The document currently states honestly that `CapabilityBoundingSet=`,
  `ProtectSystem=strict` and `ReadWritePaths` have **not** been exercised on a
  live system unit. An installer that writes them on every host makes that
  gap unacceptable. The release gate becomes: install on Debian, Ubuntu,
  Fedora and Alpine-with-systemd in CI containers, start the service, confirm
  it connects and collects.
* `install --dry-run` prints the unit, the user, the paths and the modes, and
  changes nothing. This is the audit step that `curl | bash` cannot offer, and
  it costs one flag.

### 5.7 Upgrade, uninstall, diagnose

```bash
sudo hcc-agent upgrade              # asks the controller what it wants, verifies, swaps, restarts
sudo hcc-agent upgrade --to 0.4.0
sudo hcc-agent uninstall            # stop, disable, remove unit and binary; keeps /etc/hcc-agent
sudo hcc-agent uninstall --purge    # also the state directory and the user
hcc-agent doctor                    # read-only; exits non-zero if something is wrong
```

**`upgrade`** reuses the agent's existing credential to ask
`/api/v1/agents/install-manifest` what version the controller recommends, and
verifies the download against the digest the controller gave it — the same
trust anchor as the install, now with no human in the loop. It is **never
automatic**; roadmap 10.7 makes self-update opt-in and signature-verified, and
this is the manual half of it. The old binary is kept as
`/usr/local/bin/hcc-agent.previous` until the new one connects, and restored
if it does not.

**`uninstall`** keeps `/etc/hcc-agent` by default, because a credential file
is the only thing standing between a reinstall and a re-enrolment.
`--purge` removes it and the user. Either way the command prints a reminder to
**Revoke agent credential** in the interface, because removing an agent does
not untrust it. `--revoke` does that over the API with the credential being
removed, which is strictly better than a reminder — it needs a self-revocation
endpoint, which does not exist yet.

**`doctor`** is the diagnostic the current documentation spends a whole
troubleshooting section replacing, and it touches nothing:

* Controller URL set, syntactically valid, resolves.
* TLS chain verifies — and if not, *which* step failed, since
  `x509: certificate signed by unknown authority` with no further detail is
  the single most common enrolment failure.
* Credential file present, mode `0600`, correct owner, directory not
  world-writable.
* The unit exists, is enabled, is running; last three restarts.
* Clock skew against the controller's `Date` header. A host whose clock is
  days out fails TLS in ways that read as credential problems.
* `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, `/proc/uptime` readable.
* Last applied configuration version and the modules it enables — so "nothing
  is being collected" can be answered on the host.

`doctor` must never print the credential, and must be safe to paste into an
issue report. That is a test, not a convention.

---

## 6. Enrolment credential UX

### 6.1 What the interface shows

The existing dialog is already the only screen in the product that displays a
secret, and it is careful about it — not in the query cache, not in a URL,
discarded when closed. This adds to it rather than redesigning it:

* An **architecture selector** (`amd64` / `arm64` / `arm`), defaulting to
  `amd64`, which picks the URL and digest from the manifest.
* The **install command**, with its own copy button, labelled as containing no
  secret.
* The **credential**, with its own copy button, labelled as the secret, with
  the countdown that is already there.
* One line of explanation: *the command is safe to paste anywhere; the
  credential is not.* Two copy buttons with no explanation invites copying the
  wrong one.

### 6.2 Threats, and what covers them

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Credential read from the admin's clipboard | 15-minute expiry, single use | Real. An attacker with the clipboard has the browser session too. |
| Shoulder-surfed from the screen | Expiry; input hidden at the prompt | Accepted |
| Left in shell history | It never reaches the shell | Closed |
| Read from argv by a local user | No flag accepts it; refused today | Closed |
| Read from `/proc/<pid>/environ` | Prompt is the default; the env var is documented as second-best | Open for the automation path, by the user's choice |
| Captured from logs | Never logged; `doctor` output is credential-free by test | Closed |
| Replayed after use | Single use; the digest is marked spent | Closed |
| Intercepted in transit | TLS, verification never skippable | Closed |
| Pasted into the wrong machine | Single use — the wrong host enrols and the right one fails | **Open.** See below. |

That last row is the interesting one. The current design is
*first-to-present-it-wins*: a credential pasted on the wrong machine enrols
the wrong machine, and the operator finds out when the right one fails.

Two things narrow it, neither requiring a protocol change:

1. **Show what enrolled.** The host page records the source address and the
   platform string from the enrolling agent, and the interface shows it. A
   host that reports an unexpected address is visible immediately rather than
   at the next audit.
2. **Shorten the window.** Fifteen minutes is generous for a paste. Ten is
   still comfortable and the default is one constant
   (`DEFAULT_ENROLLMENT_TTL_SECONDS`). Worth changing when the install flow is
   one paste rather than a manual procedure.

A confirm-on-the-controller step — the agent enrols as *pending*, the
administrator approves it with the fingerprint in front of them — would close
it properly. It also adds a round trip and a state to every install. Recorded
here as the option, not taken, and revisited if hosts start appearing in the
wrong place.

### 6.3 Fleet installs

Registering fifty hosts by hand through a dialog is not a flow, it is a
punishment. Not in the first delivery, but the design must not foreclose it:

* `POST /api/v1/hosts` already exists, Administrator-only. A script can
  register and issue in one pass.
* An **API token** for automation — distinct from a session — is the missing
  piece, and is its own security decision with its own ADR. Not specified
  here.
* The installer's `--token-file --non-interactive` form is the host-side half
  and is specified here, because the same flags serve a single Ansible play
  today.

---

## 7. Work breakdown

Ordered so that each step is independently useful and testable. Nothing here
is started.

| # | Work | Delivers | Roadmap |
| --- | --- | --- | --- |
| 1 | Release workflow: cross-compile, `checksums.txt`, cosign, SBOM, GitHub Release | Verifiable binaries exist at all | new |
| 2 | Agent `install` / `uninstall` / `--dry-run` | One-command install, replacing the manual procedure | 10.6 |
| 3 | Credential prompt on `/dev/tty`, `--token-file`, `--non-interactive` | Enrolment that leaks nothing | 10.6 |
| 4 | Distribution matrix in CI: install, start, connect, collect, uninstall | The unit's unverified directives become verified | 10.6 |
| 5 | Controller image, multi-arch, non-root, auto-migrate entrypoint | `docker compose up -d` | 10.3 |
| 6 | `compose.yaml`, `compose.tls.yaml`, `.env.example`, generated DB secret | A documented installation | 10.4 |
| 7 | `install-manifest` endpoint + manifest baked into the image | The controller becomes the trust anchor | new |
| 8 | Enrolment dialog: architecture selector, two copy buttons, the command | The flow in §1 | new |
| 9 | `hcc-agent doctor` | Diagnosis without a support thread | 10.6 |
| 10 | `hcc-agent upgrade`, previous-binary rollback | Upgrades | 10.7 (manual half) |
| 11 | Backup and restore scripts, exercised in CI | 10.5 | 10.5 |
| 12 | Documented, tested version-to-version upgrade | 10.8 | 10.8 |
| 13 | `.deb` / `.rpm` | Distribution-native installs | 10.6 |

Steps 1–4 are shippable without any controller change and remove the most
error-prone part of the current experience. Steps 5–6 are the other half and
are independent of them. Step 7 is what makes the install command trustworthy
rather than merely convenient, and depends on 1 and 5.

---

## 8. Open questions

1. **Does the controller serve agent binaries itself?** It would make
   air-gapped installs trivial and remove GitHub from the runtime path
   entirely. It also puts a 10 MB binary per architecture in the image and
   makes the controller a download server. Leaning no, with the manifest's
   `url` field pointing wherever an operator wants as the compromise.
2. **Is `latest` ever safe in `compose.yaml`?** Currently no. Some operators
   will want it. A documented `compose.latest.yaml` is one way to let them opt
   in loudly.
3. **How does an agent learn its controller moved?** Nothing today handles a
   controller changing DNS name. `hcc-agent reconfigure --controller …` is
   probably all it needs, but credential validity across that move needs
   thought.
4. **Should `install` refuse a host that already has an agent?** Re-enrolment
   already revokes the running credential and the interface makes that an
   explicit confirmation. The installer should probably mirror that
   confirmation rather than discovering it as a 409.
5. **Windows and macOS agents.** Out of scope, and the `install` subcommand
   design is Linux-shaped. Worth not painting the CLI into a corner.
6. **Is the architecture dropdown necessary?** The command could detect
   architecture with `uname -m` and pick the URL, but then the digest cannot
   be pinned in the copied text — which is the property the whole design rests
   on. Keeping the dropdown.

---

## 9. Requirement traceability

Every requirement this document was written against, and where it is met.

| Requirement | Where |
| --- | --- |
| Documented Compose installation | §1, §3.1, §3.3 |
| Minimal manual configuration | §3.4 — one required field |
| Secure initial setup | §3.5 generated secret; §3.2 `bootstrap-admin` stays a local command |
| Persistent storage | §3.7 |
| Migrations | §3.6 |
| Reliable upgrade path | §3.8 |
| Register host → credential → command → run → connects | §1, §5.3, §6.1 |
| Prebuilt binaries from verified GitHub Releases | §4.1, §4.2 |
| amd64 and arm64 initially | §4.1 |
| Verified by checksum or signature | §4.2 cosign; §4.3 digest pinned by the controller |
| Dedicated unprivileged system user | §5.5 step 3 |
| Restrictive file permissions | §5.5 steps 4–6 |
| Hardened systemd service | §5.6 |
| Custom certificate authorities | §5.5 step 2 |
| Never disable TLS verification | §5.5 step 2 — no flag exists, and none is added |
| No token in argv, history or logs | §5.3, §5.4, §5.7 |
| No unaudited remote script into a root shell | §5.1, §5.2, §5.6 `--dry-run` |
| Upgrades, uninstall, diagnostics | §5.7 |
| Modules disabled by default | §1 step 6, §5.5 step 9 |
| No Go on target machines | §4.1 static prebuilt binaries |
| Safe one-time enrolment | §6.2 |
| Credential distribution UX | §6.1 |
