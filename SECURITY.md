# Security policy

This file is the **reporting policy**. The threat model, the trust boundaries
and the complete list of known gaps live in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | ✅ |
| Anything else | — |

There are no tagged releases yet. This is pre-release software: the only
supported version is the current `main`, and fixes land there.

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a
security problem.**

Use GitHub's private vulnerability reporting: the **Security** tab of this
repository → **Report a vulnerability**. That channel is private to the
maintainer and lets us discuss a fix before it is public.

If private reporting is unavailable to you, open a public issue that contains
**no details** — just ask for a private channel — and you will be contacted.

### What to include

* The version or commit you tested.
* Which component: control plane, web client, or agent.
* What an attacker can achieve, and what access they need to start.
* Reproduction steps, a proof of concept, or the request that triggers it.
* Anything you think makes it worse or better than it looks.

### What to expect

This is a volunteer-maintained, pre-release project maintained by one person.

* **Acknowledgement within a week.** If you have not heard back in that time,
  the report did not arrive — please follow up.
* An assessment and a planned fix date once the report is understood.
* A fix on `main` as soon as practical, and a note in the commit.
* **Credit by name, unless you prefer otherwise.** Tell us which you want.

There is no bug bounty.

## Scope

In scope: the control plane (`apps/server`), the web client (`apps/web`) and
the Linux agent (`agents/linux`) as published in this repository.

Out of scope, because they are already documented as absent or deliberate:

* Anything in
  [the known gaps](docs/SECURITY.md#known-gaps). These are stated publicly
  because they are accepted, pending, or inherent. A report that one of them
  exists is not a vulnerability report — though a report that one is **worse
  than documented** very much is.
* Features that do not exist yet. Docker, logs, alerting, network checks and
  remote management are specified in the documentation and **not
  implemented**; a design critique is welcome as an issue, not a
  vulnerability.
* Missing hardening in the development setup. `deploy/compose.yaml` starts a
  loopback-bound PostgreSQL with a committed throwaway password, for
  development only, and says so.
* Denial of service by an authenticated administrator. An administrator can
  already stop the service.
* Reports produced by a scanner with no demonstrated impact.

## Deploying this safely

The short version, for anyone running it before the packaging work lands:

* Put a **TLS-terminating reverse proxy** in front of the control plane. It
  speaks plain HTTP itself, and agents refuse plain HTTP to anything but
  loopback.
* Set `HCC_PUBLIC_ORIGIN`, or `HCC_TRUSTED_PROXIES` if you cannot. Startup
  fails with neither, deliberately.
* Run **one** worker. Login throttling counters are per process
  ([ADR-029](docs/ARCHITECTURE.md#adr-029-in-process-rate-limiting-plus-a-semaphore-on-argon2)).
* Never set `HCC_SESSION_COOKIE_SECURE=false` in production — it is refused at
  startup for a reason.
* Keep agent credential files at mode `0600`, owned by the agent's own user.
  The agent refuses to read one that is not.

Full detail: [`docs/SECURITY.md`](docs/SECURITY.md) and
[`apps/server/.env.example`](apps/server/.env.example), which documents the
reasoning for every setting.
