# Deploying the control plane

Install, configure, upgrade, back up and restore a Homelab Control Center
installation.

> ### What has and has not been verified
>
> **Verified on every change**, by `scripts/deployment-e2e.sh` in CI: the
> image builds, `docker compose up -d` produces a stack you can log in to,
> `hcc bootstrap-admin` creates the first administrator, the web client is
> served from the same origin as the API, the container runs unprivileged with
> a read-only root filesystem, the data survives a restart of the application
> **and** of the whole stack, and `scripts/backup.sh` / `scripts/restore.sh`
> round-trip through a real dump.
>
> **Not verified**: `compose.tls.yaml`. Caddy's certificate issuance needs a
> publicly resolvable name, which CI does not have. The Caddyfile's
> *directives* are checked by `tests/test_deployment.py`; nothing has obtained
> a real certificate with it. Said plainly in [§4](#4-https).
>
> **Not published**: there is no image in any registry, so `compose.yaml`
> names a tag that does not resolve. Use the build overlay —
> [§1](#1-install) — until the first release.
>
> **Not verified**: `podman-compose`. Version 1.6.0 hangs on this stack's
> `depends_on` conditions. `deploy/compose.yaml`, the *development* database,
> still works with both runtimes ([ADR-010](ARCHITECTURE.md#adr-010-compose-file-targets-both-docker-and-podman)).
> This is the production stack and it needs Docker Compose.

---

## 1. Install

```bash
mkdir hcc && cd hcc

# Until a release is published, from a source checkout:
git clone https://github.com/felixstaude/homelab-control-center
cd homelab-control-center/deploy/production

cp .env.example .env
$EDITOR .env                 # one required field: the URL browsers will use

docker compose -f compose.yaml -f compose.build.yaml up -d --build
docker compose exec hcc hcc bootstrap-admin
```

Then open the URL you put in `.env` and sign in.

Once an image is published, the first three lines become two:

```bash
curl -fsSLO https://.../compose.yaml
curl -fsSLO https://.../.env.example
```

and the `up` drops the build overlay.

### The one required setting

```ini
HCC_PUBLIC_ORIGIN=https://hcc.example.com
```

The origin a **browser** uses, scheme included. Compose refuses to start
without it, naming the variable.

It is required rather than defaulted because the application cannot otherwise
know which origin browsers use, so the CSRF `Origin` check has nothing to
compare against. The failure that produces is specific and confusing: login
works, logout does not, and the error names CSRF rather than configuration.

Everything else has a working default or is generated. `.env.example`
documents the lot.

### Why `bootstrap-admin` is a command and not a screen

There is deliberately no HTTP endpoint that creates the first administrator.
An endpoint that works while no user exists hands ownership of the deployment
to whoever reaches the port first, and on a homelab that race is real — see
[ADR-033](ARCHITECTURE.md#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint).

Inside a container, `docker compose exec` **is** the local command. It runs
once per installation and does not reopen: a second attempt is refused, so
ownership cannot be claimed twice. If you open the interface before running
it, you get a first-run screen that tells you the command — it explains it
rather than performing it, for the same reason.

---

## 2. What the stack is

```
            :443
              │
      ┌───────▼────────┐   TLS terminates here. compose.tls.yaml adds Caddy;
      │ reverse proxy  │   an existing Traefik or nginx is the other variant.
      │   (optional)   │
      └───────┬────────┘
              │ http, on an internal bridge network
      ┌───────▼────────┐        ┌──────────────┐
      │      hcc       │───────▶│   postgres   │
      │  (one worker)  │        │ named volume │
      └────────────────┘        └──────────────┘
```

Three services and one volume.

| Service | What it is |
| --- | --- |
| `hcc-init` | A one-shot that generates the database password on first start and then exits |
| `postgres` | PostgreSQL 17. Publishes **no port**. |
| `hcc` | The control plane, with the web client built in. Publishes `127.0.0.1:8000`. |

### One origin

The API is under `/api`, the operational probes are at `/healthz` and
`/readyz`, and everything else is the web client — served by the control plane
itself from the same port.

That is not a packaging convenience. Session cookies are `HttpOnly`,
`SameSite=Lax` and `Secure`; state-changing requests need a session-bound CSRF
token *and* a matching `Origin`; the production CORS list is empty because a
same-origin client needs no exception. A second origin for the client would
mean either a credentialed CORS allow-list or a second certificate for the
same deployment.

### The database password

Generated, not chosen. `hcc-init` writes 32 random bytes to
`./secrets/db_password` on first start and leaves it alone afterwards.

It is a **file** rather than an environment variable because a container's
environment appears in `docker inspect`, and `docker inspect` is what people
paste into issue reports. The entrypoint reads the file and assembles the
database URL in the server process's own environment.

Three details worth knowing:

* It is base64**url**, because the entrypoint puts it in a `postgresql://`
  URL and a `/` in the userinfo would silently produce a URL pointing
  somewhere else. The entrypoint refuses a password outside that alphabet
  rather than trusting the generator stayed correct.
* It is mode `0644`, not `0600`. Three containers with three different uids
  read it — the generator, PostgreSQL (uid 70) and the control plane (uid
  10001) — so a `0600` file owned by one of them is unreadable by the other
  two. The protection is the deployment directory on your host and the mount
  being read-only in both consumers. This is the same guarantee Docker's own
  `/run/secrets` gives, where files are `0444`.
* **Who owns it on the host depends on your runtime.** Under rootful Docker
  the generator container runs as root, so the file is root-owned — which is
  stricter than the alternative, because only root and the containers can read
  it, and it means `sudo` to remove it. Under rootless Podman it is yours.
  Either way the containers read it, because it is `0644`.
* The mount carries `:ro,z`. The `z` is an SELinux relabel, and without it the
  deployment fails on **Fedora, RHEL, CentOS Stream, Rocky and Alma** with
  "Permission denied" on a file that is plainly `0644`. Found by running it on
  an enforcing host. Docker and Podman both ignore the flag where SELinux is
  not enforcing, so it costs nothing elsewhere.
* **Back it up separately.** It is not in the database dump, and a restore
  into an existing volume needs it. See [§6](#6-backup-and-restore).

There is no application signing secret to manage. Sessions are server-side and
the cookie carries an opaque random token of which only the SHA-256 digest is
stored ([ADR-027](ARCHITECTURE.md#adr-027-session-tokens-are-hashed-with-sha-256-not-argon2),
[ADR-028](ARCHITECTURE.md#adr-028-server-side-sessions-not-jwt)) — nothing is
signed, so nothing needs a key, so there is no key to rotate or leak.

### Migrations

`alembic upgrade head` runs on every container start, before the port is
bound. A container that cannot migrate exits non-zero and never serves
traffic.

That is safe here specifically because **one controller process** is the rule
([ADR-046](ARCHITECTURE.md#adr-046-one-controller-process-stated-rather-than-enforced)):
there is no second replica racing the same migration. A deployment that ever
becomes multi-replica has to move this to a one-shot service.

Set `HCC_AUTO_MIGRATE=false` and run it yourself if you would rather watch it:

```bash
docker compose run --rm hcc migrate
```

### One worker, and no setting for more

Login throttling counters live in the controller process's memory
([ADR-029](ARCHITECTURE.md#adr-029-in-process-rate-limiting-plus-a-semaphore-on-argon2)),
so a second worker would get its own and multiply the effective limit. The
image deliberately exposes no worker-count knob: a performance setting must
not be able to weaken a security control. When per-process limiting is
replaced, the knob arrives with it.

---

## 3. An existing reverse proxy

`compose.yaml` alone publishes `127.0.0.1:8000`. Point your proxy at it.

Your proxy **must**:

* Set `X-Forwarded-Proto`. Without it the application sees plain HTTP
  internally while the browser sends an `https://` Origin, and every
  state-changing request fails the CSRF check. (`HCC_PUBLIC_ORIGIN` covers
  this too, and is authoritative — which is why it is the required setting.)
* Set `X-Forwarded-For`, or every client collapses onto one login-throttle
  budget and one attacker can deny login to everyone.
* **Pass WebSocket upgrades through.** The agent connection *is* a WebSocket.
  A proxy that does not will serve the interface perfectly and silently fail
  every monitored host.
* Not impose a read timeout on that connection. An agent socket is meant to
  stay open for hours; a proxy that closes it on a cycle makes the whole fleet
  look like it is flapping, with nothing in the controller's log to explain
  it.

And set `HCC_TRUSTED_PROXIES` to your proxy's address. `compose.yaml` presets
it to the private ranges a Compose bridge network uses, because on single-host
Compose the proxy is a sibling *container* and `127.0.0.1` would trust
nothing. Narrow it if you can: trusting a wide range means anything on that
network can forge its own source address and bypass per-address throttling.

### nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # The agent connection. Without these three lines the interface works and
    # every host stays offline.
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
}
```

**Unverified.** Nothing has run this configuration; it is the standard
WebSocket-capable proxy block with the timeout this application needs. The
Caddy variant in `compose.tls.yaml` is the one the repository actually ships.

---

## 4. HTTPS

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

Adds Caddy, which obtains and renews a Let's Encrypt certificate given a
publicly resolvable name, and proxies WebSockets without being told to.

Set in `.env`:

```ini
HCC_SITE_ADDRESS=hcc.example.com     # usually HCC_PUBLIC_ORIGIN without the scheme
```

**None of this has been verified.** Caddy's issuance needs a name CI does not
have, so what is tested is that the Caddyfile contains the directives the
agent connection requires — not that a certificate was ever obtained. Treat
`compose.tls.yaml` as reviewed rather than exercised.

### A LAN-only name

Let's Encrypt cannot validate a name that does not resolve publicly. Two
options, both set through `HCC_TLS_DIRECTIVE`:

```ini
HCC_TLS_DIRECTIVE=tls internal
```

Caddy issues from its own local CA. You must then **install that CA** on the
machines that use the interface, *and* pass it to every agent:

```bash
# The CA, from inside the caddy-data volume:
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./hcc-ca.crt

# On each monitored host:
sudo ./hcc-agent install --controller https://hcc.lan --ca-file /path/to/hcc-ca.crt
```

The agent's `--ca-file` is **additive**: the system roots stay trusted, so
moving the controller behind a public certificate later does not break
anything. There is no setting that disables TLS verification, and none will be
added — the agent's credential crosses that connection.

```ini
HCC_TLS_DIRECTIVE=tls /tls/cert.pem /tls/key.pem
```

Serves a certificate you already have. Put it in `./tls/`.

---

## 5. Upgrading

```bash
# See what it would involve, change nothing
./scripts/upgrade-controller.sh --to 0.2.0 --plan-only

# Do it: back up, verify the backup restores, apply, check readiness
./scripts/upgrade-controller.sh --to 0.2.0
```

`upgrade-controller.sh` is the seven steps below in one command, and it
stops at the first that fails with the recovery procedure and the backup's
real path in it.

| | |
| --- | --- |
| 1 | Preflight: Compose, a running stack, a reachable controller, a writable backup directory |
| 2 | Read the current version and applied schema revision |
| 3 | Pull the target image and read **its** head revision, so the migration plan comes from the image being deployed rather than from this checkout |
| 4 | Back up — and **verify the backup by restoring it** into a scratch database |
| 5 | Confirm, unless `--yes` |
| 6 | Apply, and wait for `/readyz` |
| 7 | Verify: the version, the schema revision, and that the agents reconnected |

**Step 4 is the one worth dwelling on.** A dump whose checksum matches is a
dump that is *intact*, which is not the same as a dump that *restores*. A
truncated `pg_dump`, a version mismatch between client and server, a dump
taken while an extension was missing — each produces a file with a perfectly
good checksum and no usable database inside it. So the backup is restored
into a scratch database in the same PostgreSQL instance, its tables counted
against the live one, and dropped. The live database is never touched, and
an upgrade does not start unless the backup actually works.

### Why this is a command and not a button

Replacing the controller means replacing the container the web interface is
served from. For the application to do that to itself it would need the
Docker socket — which is root on the host — and a web application with root
on its host is a worse problem than any upgrade it could perform. There is no
remote-command API either, for the same reason.

So the controller **guides** and the command **acts**:

```
GET /api/v1/controller/upgrade-readiness
```

reports the applied schema revision, the migrations that would run, how many
agents are connected and how many updates are in flight — with a sentence of
advice for each. It is read-only, available to every authenticated role, and
a test asserts that `POST`, `PUT`, `PATCH` and `DELETE` all answer 405.

That endpoint also names the one direction a backup cannot save you from:
**a database ahead of the image**. Running an older controller against a
schema a newer one migrated reports zero pending migrations — because the
older build has never heard of the revision to be behind it — and looks
entirely current. The readiness check reports it as `schema_is_ahead` and
says the only safe move is to deploy the newer image again.

### Doing it by hand

```bash
cd deploy/production
../../scripts/backup.sh --output ~/hcc-backups    # this is the rollback
$EDITOR compose.yaml                              # bump the pinned tag
docker compose pull && docker compose up -d
```

Migrations run on start, so the last step is the upgrade.

Rules that make it reliable:

* **`compose.yaml` pins an exact version, never `latest`.** A
  `docker compose up -d` that silently jumped a major version during an
  unrelated restart is how an upgrade happens at the worst possible moment.
  Upgrading is editing one line, which is also what makes it reviewable and
  revertible.
* **Migrations are forward-only in practice.** Every migration has a
  `downgrade` and CI proves it runs, but a downgrade that drops a column
  destroys data. To go back, restore the dump. Do not use
  `alembic downgrade` as a rollback strategy.
* **Agents keep working across a controller upgrade.** The protocol is
  versioned and the agent reconnects with backoff; a controller restart is
  exercised by `make reliability-e2e`. Release notes call out an agent version
  floor when one moves.

**Unverified:** there has been no version-to-version upgrade, because there is
only one version. The *mechanism* — migrations on start, a second start being
a no-op, data surviving a restart — is verified by
`scripts/deployment-e2e.sh`.

---

## 6. Backup and restore

```bash
./scripts/backup.sh --output ~/hcc-backups
```

Writes two files: a `pg_dump -Fc` of the database, and a manifest beside it
stating what is in the backup and what is not.

### What is in it

Everything the control plane owns: accounts and their Argon2id password
hashes, sessions, the host registry, agent credential **digests**, monitoring
configuration and its revision history, the audit trail, and every stored
measurement. The control plane keeps no state on disk, so one file is the
whole installation.

Treat it as a secret. It is every password hash and every credential digest
in the installation.

### What is not in it

| Not included | Why it matters |
| --- | --- |
| `secrets/db_password` | A restore into a **fresh** volume generates a new one. A restore into an **existing** volume needs the original, and not having it is a controller that cannot authenticate to its own database. |
| `.env` | Your configuration. One required field. |
| Agent credentials on your hosts | They live in `/etc/hcc-agent/credential` on each machine. The dump restores their *digests*, so every agent reconnects with the credential it already holds — **no re-enrolment**. |
| TLS certificates you mounted yourself | Caddy's own are in the `caddy-data` volume. |

### Restoring

```bash
./scripts/restore.sh --from ~/hcc-backups/hcc-20261014T000000Z.dump --dry-run
./scripts/restore.sh --from ~/hcc-backups/hcc-20261014T000000Z.dump --force
docker compose restart hcc
```

The restart is not optional: SQLAlchemy holds pooled connections to tables the
restore has just dropped and recreated.

What the script refuses:

* A dump that does not match the digest in its manifest — checked **before**
  anything is dropped, because a corrupt dump discovered halfway through
  leaves a database that is neither the old one nor the new one.
* A non-empty target without `--force`. Every measurement since the backup
  would be lost.
* Anything at all under `--dry-run`.

### Why it drops the schema rather than using `pg_restore --clean`

Because `--clean` does not work on this schema, and finding that out during a
restore would be the worst possible time.

The metric tables are partitioned, and `--clean` emits a `DROP CONSTRAINT`
per partition which PostgreSQL refuses:

```
ERROR: cannot drop inherited constraint "metric_samples_raw_default_pkey"
       of relation "metric_samples_raw_default"
```

`DROP SCHEMA public CASCADE` is the answer: PostgreSQL resolves the whole
dependency graph itself, partitions included. Found by running it against a
real dump of a real partitioned schema.

---

## 7. Operating it

```bash
docker compose ps
docker compose logs -f hcc
docker compose exec hcc hcc --help

curl -fsS http://127.0.0.1:8000/readyz    # includes the database
```

| Probe | Answers |
| --- | --- |
| `/healthz` | this process is alive |
| `/readyz` | …and the database is reachable |

The container's `HEALTHCHECK` uses `/readyz`, because a process that cannot
reach its database is alive and not ready. Both are public and deliberately
uninformative: they carry no version and no dependency detail
([ADR-032](ARCHITECTURE.md#adr-032-public-endpoints-disclose-as-little-as-possible)).

### Something is wrong

| Symptom | Where to look |
| --- | --- |
| `up -d` exits with "Unrecognised HCC_ environment variables" | A typo in `.env`. The application refuses to start on a variable it does not recognise, deliberately: a setting that was silently ignored would leave you believing you had configured something. |
| "Permission denied" reading `/run/secrets/db_password` | SELinux, on a host where it is enforcing. The shipped Compose file mounts with `:ro,z` for this reason; if you edited the mount, put the `z` back. |
| Login works, logout does not, errors mention CSRF | `HCC_PUBLIC_ORIGIN` does not match the origin your browser is using. |
| The interface works and every host is offline | Your reverse proxy is not passing WebSocket upgrades. See [§3](#3-an-existing-reverse-proxy). |
| Every host goes offline and comes back on a cycle | Your proxy has a read timeout on the agent connection. |
| A host will not connect at all | Run `hcc-agent doctor` on that host. It checks DNS, TCP, the TLS chain, clock skew, the credential's mode and owner, and what is actually being collected. |
| The container restarts in a loop | `docker compose logs hcc`. A migration failure exits non-zero before the port is bound, which is the intended behaviour. |

---

## 8. What this deployment is not

* **Not multi-arch.** The image is `linux/amd64`. The agent is cross-compiled
  for three architectures because it runs on whatever a homelab owns; the
  controller runs on one machine you choose. An arm64 controller image needs
  either a QEMU build or an arm64 runner and has not been built.
* **Not multi-replica.** One controller process, stated and relied on — see
  the migration and worker notes above.
* **Not a high-availability PostgreSQL.** One container, one named volume,
  `pg_dump` for backups.
* **Not hardened against a hostile host.** The container runs unprivileged
  with a read-only root filesystem and no capabilities, which bounds a
  compromise of the *application*. It does not defend against somebody who
  already controls the Docker socket.

---

Related: [`SECURITY.md`](SECURITY.md) for the threat model,
[`RELEASING.md`](RELEASING.md) for how the image and the agent artifacts are
built, [`agents/linux/deploy/README.md`](../agents/linux/deploy/README.md) for
installing an agent against this controller.
