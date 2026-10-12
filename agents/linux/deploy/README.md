# Installing the agent

Written against Debian, because that is what the mission this was built for
names. The only Debian-specific parts are `adduser` and the paths.

The procedure assumes you have already **registered the host** in the control
plane and **issued an enrollment credential** for it — Hosts → Add host, then
*Issue enrollment credential* on its page. The credential is shown once and
cannot be retrieved again.

### What has actually been verified, and what has not

Stated plainly, because an installation guide whose commands have never been
run is worse than no guide: it is a guide that will be trusted.

**Verified** — against a real controller and this repository's own agent
binary, on Fedora 44:

- `hcc-agent enroll` spending a one-time credential, writing
  `/etc/hcc-agent/credential` at mode 0600, and the host appearing as
  **Connected** in the control plane within two seconds.
- `hcc-agent status` before and after enrollment, including its exit codes.
- Which file and directory permissions the agent accepts and refuses (the
  table under step 4 is measured, not assumed).
- Revoking the credential: the agent is disconnected in **23 milliseconds**,
  logs `the controller rejected this agent's credential`, and exits non-zero.
- Disabling the host: the agent is told `host_disabled`, backs off 1.9s, 2.8s,
  4.7s, 13.8s, and reconnects on its own within one wait of being re-enabled.
- `systemd-analyze verify` on the unit file, which is how the misplaced
  `StartLimitIntervalSec` in it was found.

**Not verified** — stated so you know which commands to read carefully:

- Nothing has been run on Debian. Fedora 44 is what was available. Nothing
  here is Debian-specific beyond `adduser` and the paths, and both are stable
  across every systemd distribution — but "should work" is not "was run".
- The `systemd` service itself has not been installed and started. The agent
  has only been run in the foreground, so the unit file is reviewed and
  verified-by-analyser rather than exercised.
- No ARM hardware. `arm64` and `arm` are cross-compiled in CI on every commit,
  which proves they build and nothing more.

---

## 1. Build the binary

On a machine with Go 1.24 or newer — not necessarily the monitored host:

```bash
git clone https://github.com/felixstaude/homelab-control-center
cd homelab-control-center/agents/linux
make -C ../.. agent-build
```

The binary lands in `agents/linux/bin/hcc-agent`, version-stamped. Confirm it:

```bash
./bin/hcc-agent version
```

### Cross-compiling

The agent is a static Go binary with no libc dependency, so one build per
architecture covers every distribution:

```bash
GOOS=linux GOARCH=arm64 go build -o bin/hcc-agent-arm64 ./cmd/hcc-agent
```

`amd64`, `arm64` and `arm` are built in CI on every commit, so a Raspberry Pi
is a supported target rather than a hopeful one.

---

## 2. Install it on the host

```bash
# As root on the monitored host.
install -m 0755 hcc-agent /usr/local/bin/hcc-agent

# A dedicated system account: no login shell, no home directory, no password.
adduser --system --group --no-create-home --shell /usr/sbin/nologin hcc-agent

# The configuration directory. 0750 and group-readable, so the agent can
# traverse it and no other local user can.
install -d -o root -g hcc-agent -m 0750 /etc/hcc-agent
```

`--system` matters: it allocates a uid below 1000, which keeps the account out
of the range `login.defs` treats as a human being and out of most "list the
real users" tooling.

---

## 3. Configure it

```bash
install -o root -g root -m 0644 agent.env.example /etc/hcc-agent/agent.env
editor /etc/hcc-agent/agent.env   # set HCC_AGENT_CONTROLLER_URL
```

`0644` is correct here and is not an oversight: this file holds **no secret**.
The credential lives in a separate 0600 file, which is what lets an
administrator read the configuration without sudo and still not be able to
read the credential.

---

## 4. Enroll

Put the enrollment credential in a file rather than in the environment. The
environment of a running process is readable through `/proc/<pid>/environ` and
is inherited by every child it spawns; a file with mode 0600 is neither.

```bash
# As root. The credential is the value the control plane showed you once.
install -m 0600 /dev/null /root/hcc-enrollment-token
editor /root/hcc-enrollment-token      # paste the credential, nothing else

HCC_AGENT_ENROLLMENT_TOKEN_FILE=/root/hcc-enrollment-token \
  hcc-agent enroll
```

Expected output:

```
Enrolled as "homeserver" (host 3f2c8a14-....).
Credential stored in /etc/hcc-agent/credential (mode 0600).

Start the agent with:
  systemctl enable --now hcc-agent
```

Then remove the spent credential — it cannot be used again, but there is no
reason to leave a used secret on disk:

```bash
shred -u /root/hcc-enrollment-token
```

The credential has to be readable by the service account, which is not root.
Transfer **ownership** rather than loosening the mode:

```bash
chown hcc-agent:hcc-agent /etc/hcc-agent/credential
```

The file stays 0600. `hcc-agent enroll` runs as root and writes it 0600
root-owned; the `chown` hands it to the account the service runs as, and the
mode never changes.

> **Why not 0640 with group `hcc-agent`.** That is the conventional pattern for
> service secrets and the agent refuses it, deliberately. Group-readable means
> "readable by whoever is in that group", and group membership is not visible
> at the point the agent checks the file — so the check would have to either
> enumerate the group or trust the operator to have made it a dedicated one.
> Owner-only needs neither. Verified: modes 0640, 0644 and 0660 are all
> refused, with the `chmod` to run printed in the error.
>
> The containing directory is checked too, because 0600 on the file is no
> defence if another user can unlink it and put their own file there. 0700,
> 0750 and 0755 are accepted; 0770 and 0777 are refused.

### If enrollment fails

| Message | Cause | Fix |
| --- | --- | --- |
| `the controller rejected this credential` | Expired, already used, or revoked. The controller says which to the audit trail and not to you, deliberately. | Issue a new credential. |
| `refusing to use "http://..."` | A plaintext controller URL that is not loopback. | Use `https://`. |
| `x509: certificate signed by unknown authority` | An internal CA the host does not trust. | Install your CA, or set `HCC_AGENT_CA_FILE`. |
| `enrollment succeeded but the credential could not be stored` | A permissions or disk problem. **The credential has been spent.** | Fix the problem, issue a *new* credential, run enroll again. |

---

## 5. Install the service

```bash
install -m 0644 hcc-agent.service /etc/systemd/system/hcc-agent.service
systemctl daemon-reload
systemctl enable --now hcc-agent
```

Confirm it:

```bash
systemctl status hcc-agent
journalctl -u hcc-agent -f
```

A healthy start looks like:

```
msg="agent starting" version=... controller_url=https://hcc.example.com
msg="no data is collected in this release"
msg="connected to the controller"
```

The host's page in the control plane shows **Connected** within a second or
two, with a real heartbeat timestamp and the agent's real version.

---

## 6. Verify, without trusting the output

```bash
# Is this host enrolled? Answers without starting anything, and never
# prints the credential.
hcc-agent status
```

Exit codes are distinct so a script can branch on them:

| Code | Meaning |
| --- | --- |
| 0 | Fine |
| 1 | Runtime failure, including a revoked credential |
| 2 | Bad configuration |
| 3 | Not enrolled |

---

## Operating it

### The host shows as *stale*

A connection is recorded and the heartbeats have stopped. The controller
genuinely does not know whether the agent died or the network did, and says so
rather than guessing. Check `journalctl -u hcc-agent` on the host.

### The host shows as *offline* while the agent is running

Look at the agent's log. The two common causes name themselves:

- `this host is disabled in the control plane` — somebody disabled it. Enable
  it; the agent reconnects on its own within one backoff interval.
- `the controller rejected this agent's credential` — the credential was
  revoked. The agent exits non-zero; re-enroll.

### Rotating the credential

Issue a new enrollment credential in the control plane with *Allow
re-enrollment* confirmed, then:

```bash
HCC_AGENT_ENROLLMENT_TOKEN_FILE=/root/new-token hcc-agent enroll -force
systemctl restart hcc-agent
```

The `-force` is required because enrolling an already-enrolled host revokes
the credential the running agent is using. The restart is not strictly
necessary — the agent re-reads its credential on every connection attempt —
but it makes the changeover immediate rather than waiting for the old
credential to be refused.

### Uninstalling

```bash
systemctl disable --now hcc-agent
rm /etc/systemd/system/hcc-agent.service
systemctl daemon-reload
rm -rf /etc/hcc-agent
rm /usr/local/bin/hcc-agent
deluser --system hcc-agent
```

Then revoke the credential in the control plane. Removing the agent does not
revoke anything: the credential is only a secret as long as nobody has a copy
of that file, and a decommissioned disk is a copy.

---

## What the unit file is protecting against

The hardening in `hcc-agent.service` is not decoration. Each directive is
commented in the file itself; the ones worth knowing about:

- **Never root, no capabilities.** `CapabilityBoundingSet=` is empty, so even
  an `ExecStart` changed to run as root would gain nothing.
- **`ProtectSystem=strict` with no writable path.** The running agent writes
  nothing. Enrollment writes the credential and is run by an administrator
  directly, not through this unit, so the unit needs no write access to `/etc`
  at all.
- **`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`.** No `AF_NETLINK` and
  no `AF_PACKET`: this agent does not read the host's network configuration
  and does not sniff traffic.
- **`Restart=on-failure`, not `always`.** The agent exits non-zero for the two
  conditions a restart cannot fix. `always` would hide both behind a restart
  loop while `systemctl status` reported the service as running.
- **`MemoryMax=128M`, `TasksMax=64`.** A monitoring agent that becomes the
  reason a server fell over has failed at its job in the most embarrassing way
  available.

Verify the hardening took effect on your own machine:

```bash
# Reports which directives are missing or ineffective, with a score.
systemd-analyze security hcc-agent.service
```

`systemd-analyze verify` was run against this unit during development and is
how the `StartLimitIntervalSec` in it was found to be in the wrong section —
systemd reads `StartLimit*` from `[Unit]` and ignores it in `[Service]`
without complaint, so the restart limit the file appeared to set did nothing.
`systemd-analyze security` needs the unit installed and has not been run.
