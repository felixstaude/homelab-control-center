# Secret-scanning investigation — 2026-10-13

Six high-severity findings were reported by **GitGuardian** against this
repository on the day it was first published. All six were investigated
individually. **None is a live credential, and nothing required revocation or
rotation.**

This document records the evidence, so the same six do not have to be
re-investigated from scratch the next time a scanner reports them.

**No secret values are reproduced here.** Findings are identified by file and
line. Two of the six are passwords that are deliberately committed and
therefore already public; they are still referred to by location rather than
restated, because a security document that quotes credentials is one you
cannot paste into an issue.

---

## 1. What reported them, and what did not

Worth recording precisely, because the two were confused during triage.

**GitHub's own secret scanning reported nothing.** At the time of this
investigation the API returned zero alerts in every state:

```
secret-scanning alerts:  open 0,  resolved 0
code scanning:           no analysis
dependabot:              disabled
```

That is the expected result rather than a gap. GitHub's free secret scanning
on public repositories matches **provider patterns** — AWS keys, GitHub
tokens, Stripe keys and other partner formats with a known shape. Generic
passwords in a test file or a Compose file are **non-provider patterns**, and
that setting is disabled:

```
secret_scanning:                        enabled
secret_scanning_non_provider_patterns:  disabled
secret_scanning_push_protection:        enabled
```

So GitHub structurally could not have produced these six. They came from
GitGuardian, which scans for generic patterns as well.

**Both tools are left exactly as they were.** No path exclusions were added,
nothing was disabled, and no alert was dismissed as part of this
investigation. Suppressing a scanner is a decision that outlives the person
who made it.

---

## 2. Findings

| # | Location | Classification | Dismissal reason |
| --- | --- | --- | --- |
| F1 | `agents/linux/internal/controller/client_test.go:18` | Test fixture | `used_in_tests` |
| F2 | `apps/server/tests/test_security_password.py:62` | Test fixture | `used_in_tests` |
| F3 | `apps/server/tests/test_security_password.py:746` | Test fixture | `used_in_tests` |
| F4 | `apps/server/tests/test_security_password.py:826` | Not a credential | `false_positive` |
| F5 | `.github/workflows/ci.yml:47` | Ephemeral CI service | `used_in_tests` |
| F6 | `deploy/compose.yaml:25` | Published by design | `wont_fix` |

### F1 — agent enrolment credential fixture

A high-entropy constant used as the enrolment credential in the agent's
controller-client tests.

* **Authenticates against nothing.** Every use is against
  `httptest.NewServer`, an in-process handler bound to `127.0.0.1` that exists
  for the duration of the test.
* **Predates the first real deployment.** Introduced in milestone 1.8, before
  any agent had enrolled against a real controller, so it is not a captured
  live value.
* **It is the canary in a leak test.** The same constant is used to assert
  that the credential appears in the `Authorization` header and **not** in the
  query string, the path, or the request body. Flagging it flags the thing
  that proves credentials are not leaked.
* Appears in one file, and in one path across all of history.

### F2 — the standard test password

The fixture password used across the backend suite. It is a well-known public
passphrase from a webcomic about password entropy, chosen so that no reader
mistakes it for a real one. Used in 22 test files; never in application code.

### F3 — the password-leakage canary

A class attribute of `TestNoSecretLeakage`. Its entire purpose is to be
hashed, verified and passed to logging, and then asserted **absent** from
`repr()` output and from every captured log event. One file, one path in
history.

### F4 — not a credential at all

The flagged line asserts the *type name* of a value recorded in a log event:

```python
assert events[0]["password_type"] == "str"
```

There is no secret on the line. The scanner matched the substring `password`
in a dictionary key. This is the one finding of the six that is a plain false
positive rather than a correctly-identified test fixture.

### F5 — CI database service

The password for a `postgres:17-alpine` **service container** in GitHub
Actions. The container is created for one job, is reachable only from inside
that job's network namespace, and is destroyed when the job ends. It
authenticates against nothing that exists after the run.

### F6 — local development database

The password for the development PostgreSQL in `deploy/compose.yaml`.

This is the one finding that should **not** be called a false positive: it is
a working credential. It is also published on purpose, and the file has said
so since it was written — the comment above it states that the credentials are
local-development-only, grant access to a throwaway container, and must never
be reused in a deployment.

Verified not externally reachable:

```
ss -ltnp     LISTEN  127.0.0.1:5432
podman port  5432/tcp -> 127.0.0.1:5432
```

The Compose file binds `127.0.0.1:5432:5432` rather than the `5432:5432`
default precisely because the default listens on every interface. No
non-loopback address can reach it. No HCC service listens off-loopback.

Dismissing this as "not a secret" would be inaccurate. `wont_fix`, with the
reasoning recorded, is the honest classification.

---

## 3. Checks performed

Applied to all six, not just the ones that looked doubtful:

1. **Exact code context** read for every finding, including how the value is
   used at each call site.
2. **Reach**: what the value authenticates against, and whether that thing is
   reachable from outside the machine.
3. **Reuse**: whether the value appears anywhere else in the working tree.
4. **History**: whether it ever appeared in a file that has since been deleted
   or changed, searched across every commit rather than only the current tree.
5. **Provenance**: when it entered the repository, relative to the first real
   agent enrolment.

No value crosses from test or development configuration into application
source. No value appears in a file that was later removed.

---

## 4. Conclusions

* No credential requires rotation. No deployed or externally accessible
  service is protected by any of these values.
* F1–F5 are false positives in the operational sense: five test or CI
  fixtures and one keyword match.
* F6 is a real credential that is public by design and bounded by a loopback
  binding.
* The two committed development passwords stay as they are. Removing them
  would mean a fresh checkout no longer works without setup, which is the
  trade the comment in `deploy/compose.yaml` already describes.
* The test fixtures stay as they are. Renaming them to look more obviously
  fake would churn 22 files to make a scanner quieter, and the fixtures are
  already self-evident in context.

## 5. If these are reported again

Classify against the table in §2 rather than re-deriving. When a scanner
reports something **not** in that table, investigate it properly — the value
of keeping this list short is that a seventh finding is visibly new.

Real credentials belong in neither the repository nor this document. The
reporting route for an actual exposure is [`SECURITY.md`](../../SECURITY.md);
the handling rules for secrets in general are in
[`SECURITY.md`'s secrets-handling section](../SECURITY.md#secrets-handling).
