# Installing the agent

```bash
# On the machine you want to monitor, as a user who can sudo.
curl -fsSLO <the download URL the control plane shows>
echo '<the digest the control plane shows>  hcc-agent_<version>_linux_amd64.tar.gz' | sha256sum -c -
tar xzf hcc-agent_<version>_linux_amd64.tar.gz

sudo ./hcc-agent install --controller https://your-controller
```

It asks for the enrolment credential, with input hidden, and the host appears
as **Connected** a second or two later.

Four lines, each doing one comprehensible thing. Nothing is piped into a
shell. `sha256sum -c -` exits non-zero, and because these are separate
commands rather than a pipeline, a mismatch stops the paste before the `tar`.

> **The download URL and digest come from the control plane**, on the host's
> page, for the architecture you select. They arrive inside the TLS session
> you are already authenticated to, which is what makes the digest worth
> checking — an attacker who controls the download origin cannot make a
> substituted tarball match it.
>
> **No release has been published yet**, so those values do not exist. Until
> one is, build the binary yourself: [§6](#6-building-the-binary-yourself).

---

## What has actually been verified, and what has not

Stated plainly, because an installation guide whose commands have never been
run is worse than no guide: it is a guide that will be trusted.

**Verified** — by `go test ./internal/install ./internal/doctor`, which runs on
every change. The filesystem is real and rooted at a temporary directory;
systemd and user creation are behind an interface, so what is proved is the
ordering, every refusal, and the whole rollback path:

- `install` places the binary, the unit, the environment file, the
  configuration directory and the credential at the right modes, and hands the
  credential and the directory to the service account.
- Every refusal: no root, no systemd, a `/proc` with the kernel files hidden,
  a *login* account called `hcc-agent`, an unreachable or unverifiable
  controller, an already-complete installation without `--force`, a
  world-readable `--token-file`, `--non-interactive` with no credential source.
- The transaction: a failure before enrolment removes everything this run
  created including the account; a failure after it keeps the credential, and
  re-running the same command finishes the installation **without enrolling
  again**.
- `--dry-run` changes nothing, runs no privileged operation, and prints every
  line of the unit it would install.
- Neither credential appears in any output, in the environment file, or in
  `doctor`'s text or JSON.
- `upgrade` refuses a digest mismatch before touching anything, refuses an
  archive with a path-traversal member, a symlink member, a decompression
  bomb or a binary for the wrong architecture, and rolls back to the previous
  binary when the new one does not reconnect.
- `uninstall` keeps the credential by default and removes it under `--purge`.
- `systemd-analyze verify` accepts every directive in the unit.

**Verified by the installation matrix**, which runs in CI on every change
against a privileged container with systemd as pid 1 — Debian 13, Ubuntu
24.04 and Fedora 42 — with a real controller behind real TLS and a real
internal CA. Sixty-one assertions covering install, collect, diagnose,
upgrade, roll back and uninstall. A mocked systemd proves nothing about
systemd; see [§8](#8-the-installation-matrix) for the whole list.

**Verified earlier**, against a real controller on Fedora 44, by hand:

- Revoking the credential: the agent is disconnected in **23 milliseconds**,
  logs `the controller rejected this agent's credential`, and exits non-zero.
- Disabling the host: the agent is told `host_disabled`, backs off 1.9s, 2.8s,
  4.7s, 13.8s, and reconnects on its own within one wait of being re-enabled.
- Which file and directory permissions the agent accepts and refuses.

**Not verified**:

- **No ARM hardware.** `arm64` and `arm` are cross-compiled on every commit
  and executed under emulation in the release workflow, which proves they run
  and nothing about a Raspberry Pi.
- **No Alpine.** busybox `adduser` is handled in the installer and is not in
  the matrix, because Alpine's systemd packaging is unusual enough that a
  container test of it would mostly be testing the container.
- **`systemd-analyze security`** needs the unit installed on the host being
  scored and has not been run.
- **No published release**, so the download-and-verify half of the command at
  the top of this document has never been performed against a real URL.

---

## 1. What `install` does

Ordered, and each step a refusal rather than a warning when it cannot be done
safely. `hcc-agent install --dry-run` prints all of this, including the exact
unit file, and changes nothing — which is the audit step that
`curl … | sudo bash` cannot offer.

1. **Preflight.** Linux; root; systemd present; `/proc/stat`, `/proc/meminfo`,
   `/proc/loadavg` and `/proc/uptime` readable; the target directories usable;
   enough free space; a credential source available.
2. **Reach the controller** and verify its certificate — *before* creating a
   user or writing a file, so that a typo in a hostname leaves no debris.
3. **Create the `hcc-agent` system account**: no home directory, no login
   shell, uid below 1000. An existing *system* account of that name is reused;
   a *login* account of that name is refused rather than silently adopted.
4. **Create `/etc/hcc-agent`**, mode `0750`, owned by `hcc-agent`.
5. **Install the binary** to `/usr/local/bin/hcc-agent`, mode `0755`,
   root-owned, written to a temporary file in the same directory and renamed
   over the target. Any existing binary is kept as `hcc-agent.previous`.
6. **Write `/etc/hcc-agent/agent.env`**, mode `0644`, root-owned. It holds the
   controller URL and no secret.
7. **Ask for the enrolment credential** and exchange it. The credential is
   never written to disk by the installer: it goes from the terminal to one
   HTTPS request. The long-lived credential it returns lands in
   `/etc/hcc-agent/credential` at mode `0600`, owned by `hcc-agent`.
8. **Write the unit** to `/etc/systemd/system/hcc-agent.service`,
   `daemon-reload`, `enable --now`.
9. **Wait for the connection.** "Installed" means *connected*, not "files
   placed" — proved by the controller's configuration arriving and being
   stored. If it does not arrive, the installer prints the journal and tells
   you to run `hcc-agent doctor`.
10. **Say what is not happening.** No module is enabled, so nothing is being
    collected. That is the default-deny rule, and the installer does not
    quietly improve on it.

### If something fails

The installation is a transaction. Anything **this run** created is removed if
a step fails, and nothing a previous run created ever is — so an interrupted
install cannot destroy a working one.

Two things are never rolled back, deliberately:

* **The credential**, once enrolment has succeeded. A one-time credential was
  spent to obtain it, and deleting it would cost you another to recover from a
  problem that has nothing to do with credentials. Re-running the same command
  finishes the installation **without enrolling again**.
* **A correct installation that has not connected.** The files stay, the error
  says so, and the fault is almost always a firewall or a reverse proxy.

---

## 2. Where the credential comes from

Three sources, most explicit first.

| Form | Leaks to | When to use |
| --- | --- | --- |
| **Prompt on the terminal** | nothing | the default |
| `--token-file /path` | nothing, if the mode is `0600` | automation |
| `HCC_AGENT_ENROLLMENT_TOKEN_FILE=/path` | nothing, if the mode is `0600` | existing scripts |
| `HCC_AGENT_ENROLLMENT_TOKEN=…` | `/proc/<pid>/environ`, and every child process | documented, and second-best |
| `--token …` | **every local user**, via `/proc/<pid>/cmdline` | **does not exist, and will not** |

The prompt reads `/dev/tty` rather than stdin, with echo disabled. That is
deliberate twice over: it keeps working when the command is part of a
pipeline, and it makes the prompt impossible to satisfy from a redirected
file — which is the behaviour you want from something about to mint a
long-lived credential.

For configuration management, where no terminal exists:

```bash
sudo ./hcc-agent install \
  --controller https://hcc.example.com \
  --token-file /run/secrets/hcc-enrolment \
  --non-interactive
```

Write the file with mode `0600`, run this, delete it. `--non-interactive`
makes the absence of a terminal an error rather than a hang.

---

## 3. An internal certificate authority

```bash
sudo ./hcc-agent install \
  --controller https://hcc.internal \
  --ca-file /path/to/your-ca.pem
```

The bundle is copied to `/etc/hcc-agent/ca.pem` at mode `0644` — a CA
certificate is public — and `HCC_AGENT_CA_FILE` is set in the environment
file. It is **additive**: the system roots stay trusted, so moving the
controller behind a public certificate later does not break anything.

There is no flag, variable or setting that disables TLS verification.
`HCC_AGENT_TLS_SKIP_VERIFY` was removed rather than carried forward: the
agent's credential and every heartbeat cross that connection, and a warning
nobody reads is not a mitigation.

---

## 4. Operating it

```bash
hcc-agent doctor            # diagnose; changes nothing; safe to paste anywhere
hcc-agent doctor --json     # the same, machine-readable
hcc-agent status            # is this host enrolled?
systemctl status hcc-agent
journalctl -u hcc-agent -f
```

`doctor` is the troubleshooting section this document used to be. It checks the
host, the installation, the configuration, the credential's mode and owner, the
service, DNS, TCP, the TLS chain, clock skew against the controller, and what
is actually being collected — and it exits non-zero when something is wrong,
so a script can branch on it.

It never prints either credential. That is a test
(`TestOutputNeverContainsTheCredential`), not a convention.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Fine |
| 1 | Runtime failure, including a revoked credential |
| 2 | Bad configuration |
| 3 | Not enrolled |

### Upgrading

```bash
curl -fsSLO <the URL the control plane shows>
sudo hcc-agent upgrade \
  --from hcc-agent_<version>_linux_amd64.tar.gz \
  --sha256 <the digest the control plane shows>
```

The digest is **required**, and there is no flag that skips it. The upgrade
verifies the archive, extracts only a member named exactly `hcc-agent`, checks
it is an ELF object for this architecture, keeps the running binary as
`hcc-agent.previous`, restarts the service, and waits for it to reconnect. If
it does not, the previous binary is put back and the service restarted with
it. The credential and the stored configuration are never touched.

Upgrades are never automatic.

### Rotating the credential

Issue a new enrolment credential in the control plane with *Allow
re-enrolment* confirmed, then:

```bash
sudo hcc-agent install --controller https://hcc.example.com --force
```

`--force` is required because enrolling an already-enrolled host revokes the
credential the running agent is using.

### Uninstalling

```bash
sudo hcc-agent uninstall             # service, unit, binary; keeps the credential
sudo hcc-agent uninstall --purge     # also /etc/hcc-agent and the account
sudo hcc-agent uninstall --dry-run   # print what would go
```

The default keeps `/etc/hcc-agent`, because a credential file is the only
thing standing between a reinstall and a re-enrolment.

**Then revoke the credential in the control plane.** Removing the agent does
not untrust it: that file is only a secret for as long as nobody has a copy of
it, and a decommissioned disk is a copy. Uninstalling deletes **no**
controller-side data — every metric this host reported is still there.

---

## 5. What the unit file is protecting against

The installed unit is
[`agents/linux/internal/install/hcc-agent.service`](../internal/install/hcc-agent.service).
It is embedded in the binary and written verbatim, and there is deliberately
**no second copy** in this repository: `hcc-agent install --dry-run` prints
exactly those bytes, so what you review is what gets installed.

Each directive is commented in the file itself. The ones worth knowing about:

- **Never root, no capabilities.** `CapabilityBoundingSet=` is empty, so even
  an `ExecStart` changed to run as root would gain nothing.
- **`ProtectSystem=strict` with exactly one writable path.** The whole
  filesystem is read-only to this service except `/etc/hcc-agent`, which the
  agent needs because it persists the configuration the controller sends it.
- **`ProcSubset=pid` is deliberately absent.** It mounts `/proc` with
  `subset=pid`, which hides `/proc/stat`, `/proc/meminfo`, `/proc/loadavg` and
  `/proc/uptime` — every file the agent reads. It was in this unit until the
  installer took ownership of it; the unit had never been *started*, only
  checked with `systemd-analyze verify`, which does not execute anything. With
  it, the service starts, connects, reports itself healthy and collects
  nothing.
- **`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`.** No `AF_NETLINK` and
  no `AF_PACKET`: this agent does not read the host's network configuration
  and does not sniff traffic.
- **`Restart=on-failure`, not `always`.** The agent exits non-zero for the two
  conditions a restart cannot fix.
- **`MemoryMax=128M`, `TasksMax=64`.** A monitoring agent that becomes the
  reason a server fell over has failed at its job in the most embarrassing way
  available.

Score the hardening on your own machine:

```bash
systemd-analyze security hcc-agent.service
```

---

## 6. Building the binary yourself

Until a release is published, or for an architecture that is not packaged:

```bash
git clone https://github.com/felixstaude/homelab-control-center
cd homelab-control-center
make agent-build                       # agents/linux/bin/hcc-agent
```

Or the whole artifact set, reproducibly, exactly as CI builds it:

```bash
make artifacts                         # dist/agent/
make check-artifacts                   # and verify them
```

Then copy the binary to the host and run `sudo ./hcc-agent install` as above.
The installer installs **itself**, so there is no separate step and nothing
else to trust: the artifact you checksummed is the one that performs the
installation and then is the installed program.

Cross-compiling one architecture by hand:

```bash
cd agents/linux
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -o bin/hcc-agent-arm64 ./cmd/hcc-agent
```

---

## 7. Installing by hand

There is no supported manual procedure any more, and that is deliberate: the
forty lines of shell this document used to contain were retyped slightly
differently by every operator, and the installer does the same work in a
language with types and tests. If you want to see what it would do, run
`install --dry-run` — it prints every path, every mode, the service account,
and the complete unit file.

If you need something the installer does not do — a different prefix, a
read-only `/usr`, a distribution without `useradd` — open an issue. A flag
added for a real deployment is better than a procedure nobody tests.

---

## 8. The installation matrix

`scripts/install-matrix.sh` is the test that proves the paragraphs above. It
runs in CI on every change, once per distribution, and locally with:

```bash
make db-up
make install-matrix                     # debian:13
./scripts/install-matrix.sh --image fedora:42
```

What it stands up: a **privileged container with systemd as pid 1**, a real
controller against real PostgreSQL, a real internal CA, a real TLS terminator
in front of the controller, and the release artifacts built by the real build
script. Then it runs the actual commands:

| Step | What is asserted |
| --- | --- |
| `install --dry-run` | prints the unit, verifies the certificate, creates nothing, prints no credential |
| `install` | every path, mode and owner; the service is active and enabled; the process is **not** root; the controller reports the host connected; the agent stored its configuration |
| enable `cpu` | a real sample reaches the database — the check that the sandbox does not hide `/proc` |
| `doctor` | exits zero on a healthy host, prints no credential, and `--json` parses |
| a second `install` | refused, naming `upgrade` |
| `upgrade` | the new version is installed, the previous kept, the service active, the controller told |
| a wrong digest | refused, and nothing changed |
| `upgrade` with the controller unreachable | fails, rolls back, and the credential is untouched |
| the controller returning | the agent reconnects on its own |
| `uninstall` | unit and binary gone, credential kept, the revoke reminder printed, **and the controller still holds every sample** |
| `uninstall --purge` | the configuration directory and the account gone |

Sixty-one assertions. The container is removed on every exit path, the
database is dropped, and the TLS terminator listens on exactly one address —
the host's loopback under rootless Podman, the container bridge's gateway
under Docker — never on `0.0.0.0`.

The container is privileged, because systemd as pid 1 needs it. That is why
this is a separate script rather than part of `make check`.

---

## 9. Turning collection on

A freshly enrolled agent connects and reports nothing, because every module is
off until an administrator enables it for the host. That is the
[default-deny rule](../../../docs/MONITORING_MODULES.md), not a gap in the
flow.

A complete worked example from a bare host to a chart with real measurements:
[`docs/development/ENABLING_MONITORING_DEBIAN.md`](../../../docs/development/ENABLING_MONITORING_DEBIAN.md).
