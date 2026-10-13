# Enabling CPU and memory monitoring on a Debian host

Exact commands, from a host nothing is installed on to a chart with real
measurements. Written against **Debian 12 (bookworm)** and **Debian 13
(trixie)**; everything here is the standard library plus `systemd`, so it works
unchanged on Ubuntu 22.04 and later.

Nothing in this guide requires editing a configuration file on the host. The
agent is told what to collect by the controller, which is the whole point of
the configuration channel — so after enrolment, every change in this guide is
made in the web interface.

> **Read this first if you are monitoring a machine somebody else uses.**
> Enabling a module starts reading that machine. CPU and memory utilisation
> are not sensitive on their own, but they reveal when a person is working,
> when they stopped, and roughly what they were doing. That is a decision to
> make deliberately rather than a default to accept.

---

## 1. What you need before starting

* A running Homelab Control Center controller, reachable from the host over
  HTTPS. If you have not set one up, see
  [`DEVELOPMENT.md`](../DEVELOPMENT.md).
* An **Administrator** account on it. Enabling collection is
  administrator-only — an Operator who could enable a module could start
  collecting data about a machine without an administrator being involved.
* `root` on the Debian host, for installing a systemd unit. The agent itself
  does **not** run as root; see §4.

Check the controller is reachable from the host before anything else, because
every later step fails confusingly if it is not:

```bash
curl -fsS https://hcc.example.com/healthz
```

```
{"status":"ok"}
```

The version is deliberately absent: `/healthz` is public, and a release number
is of no use to a legitimate anonymous caller and of obvious use to somebody
matching your deployment against known vulnerabilities.

If that fails, fix it now. A DNS name that does not resolve, a firewall, or a
certificate the host does not trust all produce enrolment errors that look
like credential problems.

---

## 2. Register the host in the controller

In the web interface:

1. Go to **Hosts** and press **Register host**.
2. Give it the name you will recognise it by — the hostname is the obvious
   choice. Optionally set an environment label such as `production`.
3. Press **Register**.

The host appears with three separate pieces of state, and all three matter:

| State | Now | Means |
| --- | --- | --- |
| Lifecycle | **Registered** | The controller knows the name. No agent has claimed it. |
| Enabled | **Yes** | An agent would be allowed to connect. |
| Connectivity | **Offline** | Nothing is connected. |

Registering does **not** create a credential. That separation is deliberate:
otherwise every mistyped host name would also mint a live secret.

### Issue the enrolment credential

On the host's page, press **Issue enrolment credential**. A credential is
shown **once** and cannot be retrieved again — the controller stores only its
digest, so there is no request that could return it a second time. Copy it now.

It expires in fifteen minutes and is single-use.

---

## 3. Install the agent on the Debian host

Build or obtain `hcc-agent` for `linux/amd64` and put it in place:

```bash
sudo install -o root -g root -m 0755 hcc-agent /usr/local/bin/hcc-agent
hcc-agent version
```

```
hcc-agent 0.1.0 (commit 1a2b3c4, built 2026-10-13T01:00:00Z, go1.24.13, linux/amd64)
```

A binary built straight from a source checkout without version stamping
reports `hcc-agent dev (commit unknown, built unknown, …)` instead, which is
fine for trying this out and not what you want in `/usr/local/bin` on a real
host. `make agent-build` stamps it.

### Create the user the agent runs as

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin hcc-agent
sudo install -d -o hcc-agent -g hcc-agent -m 0700 /etc/hcc-agent
```

A system user with no shell and no home directory. The agent needs to read
`/proc`, which any user can, and to write two files in `/etc/hcc-agent`, which
only this user can.

---

## 4. Enrol the host

The credential goes in a **file**, never on a command line — `/proc` makes
every process's arguments readable by every local user on the machine, so a
credential passed as a flag is a credential handed to anyone with a shell. The
agent refuses a flag for it.

```bash
sudo install -o hcc-agent -g hcc-agent -m 0600 /dev/null /etc/hcc-agent/enrollment
sudo tee /etc/hcc-agent/enrollment >/dev/null <<'EOF'
paste-the-credential-here
EOF
sudo chown hcc-agent:hcc-agent /etc/hcc-agent/enrollment
sudo chmod 600 /etc/hcc-agent/enrollment
```

Now enrol, as the agent's own user:

```bash
sudo -u hcc-agent env \
  HCC_AGENT_CONTROLLER_URL=https://hcc.example.com \
  HCC_AGENT_ENROLLMENT_TOKEN_FILE=/etc/hcc-agent/enrollment \
  /usr/local/bin/hcc-agent enroll
```

```
Enrolled as "debian-nas" (host 3f7c1e90-5a4b-4c2e-9d81-6b0f2a7c4e55).
Credential stored in /etc/hcc-agent/credential (mode 0600).

Start the agent with:
  systemctl enable --now hcc-agent
```

Delete the spent enrolment credential — it is single-use and now worthless,
and leaving secrets on disk is a habit worth not having:

```bash
sudo rm /etc/hcc-agent/enrollment
```

Confirm, and note that `hcc-agent status` reads local state only; it does not
contact the controller:

```bash
sudo -u hcc-agent /usr/local/bin/hcc-agent status
```

```
Controller:      https://hcc.example.com
Credential file: /etc/hcc-agent/credential
Config file:     /etc/hcc-agent/config.json
Enrolled:        yes
Protocol:        v1
Configuration:   none yet; the controller sends one on connect
```

In the web interface the host's lifecycle is now **Enrolled**. Connectivity is
still **Offline** — enrolling does not start the agent.

Once the agent has run and received a configuration, the same command reports
`Configuration:   v2` and a `Modules:` line, which is the quickest way to see
from the host what it believes it is collecting.

---

## 5. Run the agent as a service

```bash
sudo tee /etc/systemd/system/hcc-agent.service >/dev/null <<'EOF'
[Unit]
Description=Homelab Control Center agent
Documentation=https://github.com/felixstaude/homelab-control-center
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hcc-agent
Group=hcc-agent
Environment=HCC_AGENT_CONTROLLER_URL=https://hcc.example.com
Environment=HCC_AGENT_LOG_FORMAT=json
ExecStart=/usr/local/bin/hcc-agent run
Restart=always
RestartSec=5

# The agent reads /proc and writes two files. Nothing else.
ProtectSystem=strict
ReadWritePaths=/etc/hcc-agent
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictSUIDSGID=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now hcc-agent
```

`CapabilityBoundingSet=` is empty on purpose: the agent needs no capability at
all for CPU and memory collection, so it is given none. `ProtectSystem=strict`
with a single `ReadWritePaths` means the only writable path in its view of the
filesystem is its own state directory.

### What has been verified about this unit, and what has not

Stated precisely, because a hardening block that looks thorough and silently
prevents the service from starting is worse than none:

* **The unit parses with no errors.** `systemd-analyze verify` accepts every
  directive above.
* **`SystemCallFilter=@system-service` and `MemoryDenyWriteExecute=yes` were
  run against this agent binary** in a transient unit, and it works. Those
  are the two options most likely to break a runtime that allocates
  executable memory, and this one does not.
* **`CapabilityBoundingSet=`, `ProtectSystem=strict` and `ReadWritePaths` have
  not been exercised on a live system unit.** Doing that means installing a
  unit as root on a real machine, which is outside what was verified here.
  They are standard directives used as documented, and if the service fails
  to start with `status=218/CAPABILITIES` or a permission error writing
  `/etc/hcc-agent`, those are the lines to comment out first while you
  investigate.

Check it connected:

```bash
systemctl status hcc-agent --no-pager
sudo journalctl -u hcc-agent -n 20 --no-pager
```

You are looking for a line like:

```
{"time":"...","level":"INFO","msg":"connected","component":"agent"}
```

The exact fields depend on the release; what you are checking is that a line
at `INFO` says it connected and that nothing after it says it disconnected.

In the web interface, the host's connectivity becomes **Connected** within a
second or two, and the **Agent** card fills in the version and platform the
agent reports about itself.

---

## 6. Nothing is being collected yet, and that is correct

Look at the host's page now. The **Resources** card says:

> **No measurements have arrived yet.** Nothing is enabled for this host, so
> there is nothing to measure.

And the **Monitoring** card shows all five modules as **Off**.

This is the default-deny rule, not a fault. Enrolling a host gives the
controller permission to talk to it; it does not start reading it. A
monitoring system that began collecting because a machine was registered would
be making a decision that is not its to make.

---

## 7. Enable CPU and memory collection

On the host's page, in the **Monitoring** card:

1. Find **CPU utilisation**. Change its control from **Off** to **On**.
2. Find **Memory**. Change it to **On**.

Each change saves immediately. Watch the banner at the top of the card — it
will briefly read:

> **Saved, not yet confirmed.** Version 2 has been sent and the agent has not
> acknowledged it yet. This normally resolves within a heartbeat.

and then:

> **Applied** — The agent confirmed version 2 just now. What is below is what
> the host is doing.

That distinction is not decoration. The controller saving a configuration has
not changed what the machine is doing; the agent has to receive it, validate
it and apply it, and it can refuse. **Applied** is the only state that means
collection has actually started.

### Choosing "inherit" instead

The control has a third position, **Inherit**, and it is not a tidier spelling
of **Off**:

* **Off** — this host does not collect CPU, whatever the installation default
  says.
* **Inherit** — the installation default decides. If you later enable CPU
  globally under **Settings → Monitoring**, this host starts collecting.

Use **Inherit** for hosts you want to manage as a fleet, and **Off** for a
machine you have deliberately excluded.

---

## 8. Watch the first measurements arrive

The default interval is ten seconds. The first memory sample appears on the
next tick; the first **CPU** sample takes two ticks, because utilisation is
computed from the difference between two readings of `/proc/stat` and a single
reading has nothing to compare against.

Within about twenty seconds the **Resources** card shows tiles:

```
CPU UTILISATION          MEMORY                   MEMORY
2.4 %                    13.1 GiB                 41.2 %
CPU utilisation · now    Memory · now             Memory · now
```

Underneath, a **History** chart with a series picker and a range selector, and
a **Collection** section listing every module — including the three that are
still off, each reading **No data**, so that "this module has no data" and
"this module does not exist" stay different answers.

Confirm from the host's side too:

```bash
sudo journalctl -u hcc-agent -n 5 --no-pager | grep telemetry
```

```
{"time":"...","level":"DEBUG","msg":"telemetry stored","seq":12,"samples":5}
```

`DEBUG` — so this line only appears if you set
`HCC_AGENT_LOG_LEVEL=debug`. At the default level a working agent is quiet,
which is the right default for something that runs for ever.

---

## 9. Changing how often it collects

In the **Monitoring** card, each module's line states its floor:

```
Every 10 s · set on this host · no faster than every 1 s
```

To change the interval for one host, the API is the surface today — the
interface exposes per-host interval editing only through the global defaults
page (**Settings → Monitoring**), which sets it for every host that has not
overridden it.

To set it globally, go to **Settings → Monitoring**, type a number into the
module's **Interval (seconds)** field and press **Save interval**.

An interval below the module's floor is **refused**, not quietly rounded up:

> CPU utilisation cannot run more often than every 1 s.

The value you typed is not saved. That is deliberate — a setting silently
replaced by a different one is a setting you cannot reason about.

### What the floors are, and why

| Module | Default | Floor | Why that floor |
| --- | --- | --- | --- |
| CPU utilisation | 10 s | 1 s | Below a second the two `/proc/stat` readings differ by a handful of jiffies and the result is mostly rounding. |
| Memory | 10 s | 1 s | `/proc/meminfo` is cheap; a second is below any rate of change worth watching. |
| Swap | 10 s | 1 s | Same source as memory. |
| Load average | 30 s | 5 s | The kernel's load average is already a smoothed value over one, five and fifteen minutes. Sampling it faster measures the smoothing. |
| System uptime | 300 s | 60 s | A counter that increases by one per second. Sampling it faster stores a number you could have calculated. |

---

## 10. Turning it off again

Set the module back to **Off** in the **Monitoring** card. The agent stops
collecting it within a heartbeat, and the next chart you load has a gap
starting from that moment — drawn as a break in the line, because nothing was
measured there.

**The history is kept.** Disabling a module is not deleting its data; the
samples already stored stay until retention expires them (seven days raw,
ninety days at five-minute resolution, two years hourly — see
[`DATA_RETENTION.md`](../DATA_RETENTION.md)).

To stop the agent entirely:

```bash
sudo systemctl disable --now hcc-agent
```

The host shows **Offline** within a minute. Its enrolment survives, so
starting the service again reconnects without re-enrolling.

To withdraw trust from the host — a decommissioning, or a credential you think
has been exposed — use **Revoke agent credential** on the host's page instead.
The agent is disconnected within milliseconds and exits rather than retrying,
because a credential that can never work again is not worth a retry loop.

---

## 11. When it does not work

### The host stays "Offline" after starting the service

```bash
sudo journalctl -u hcc-agent -n 50 --no-pager
```

| In the log | Cause | Fix |
| --- | --- | --- |
| `x509: certificate signed by unknown authority` | The host does not trust the controller's certificate. | Install the CA in the system trust store, or point `HCC_AGENT_CA_FILE` at the bundle. There is no option to skip verification. |
| `the controller rejected this agent's credential` | The credential was revoked, or the host was re-enrolled elsewhere. | Issue a new enrolment credential and run `hcc-agent enroll` again. |
| `no such host` / `connection refused` | DNS or firewall. | The `curl` check in §1. |
| `host is disabled` | Somebody disabled the host in the interface. | Enable it on the host's page. The agent reconnects on its own. |

### The host is "Connected" but no measurements arrive

Check the **Monitoring** card first — the overwhelmingly common cause is that
nothing is enabled, which is the default. If a module *is* enabled and the
banner says **Applied**, look at:

```bash
sudo journalctl -u hcc-agent -n 50 --no-pager | grep -i 'collect\|error'
```

### The banner is stuck on "Saved, not yet confirmed"

The agent has not acknowledged the configuration. Two causes worth checking:

1. **It cannot write its configuration file.** The agent persists what the
   controller sends, and if that write fails it will not acknowledge — it
   logs the failure and keeps running the previous configuration, which is
   true but not obvious from the interface.

   ```bash
   sudo journalctl -u hcc-agent -n 50 --no-pager | grep -i 'cannot store'
   ```

   ```
   cannot store the configuration the controller sent:
     cannot create configuration directory "/etc/hcc-agent": permission denied
   ```

   Fix the ownership of `/etc/hcc-agent` (§3), or point
   `HCC_AGENT_CONFIG_FILE` somewhere the agent's user can write.

2. **It is not connected.** The banner says so explicitly in that case: the
   controller never dials out, so it will apply the change on its next
   connection.

### The banner says "The agent refused this configuration"

The agent validated the configuration and would not run it, and the reasons
it gave are listed under the banner. This means the controller is asking for
something the agent's build does not support — usually a module or schedule
kind from a newer controller than the agent. Upgrade the agent.

---

## 12. What this does not install

Worth stating, so the absences are not mistaken for failures:

* **No disk, network or temperature collection.** Those modules do not exist
  yet. The **Collection** section lists only the five that do.
* **No container discovery.** Docker is Phase 4. The Overview page's container
  card is sample data and says so.
* **No log collection.** Phase 6.
* **No remote command execution of any kind.** The agent runs no command on
  the host, and the protocol has no message type that could ask it to. It
  reads `/proc` and writes two files in its own directory.
