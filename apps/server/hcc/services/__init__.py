"""Business logic, between the API and the database.

Phase 1, milestone 1.7. This package exists because user management is the
first feature with rules that are neither HTTP concerns nor storage concerns.
"Deactivating the last active administrator is refused" is not a request
validation, not an authorization decision and not a constraint PostgreSQL can
express — it is a business rule, and putting it in a route handler would mean
the rule lived wherever the first caller happened to be.

The boundaries, and why each is where it is:

| Layer | Owns | Lives in |
| --- | --- | --- |
| Request validation | Shapes, bounds, field names | ``hcc/api/v1/*.py`` (Pydantic) |
| Authorization | Who may call this at all | ``hcc/api/deps.py``, ``security/authorization.py`` |
| Business rules | Invariants, refusals, orchestration | **here** |
| Storage | Columns, constraints, triggers | ``hcc/models/*.py`` |
| Password hashing | Argon2id and the policy | ``hcc/security/password.py`` |
| Session lifecycle | Issue, resolve, revoke | ``hcc/security/sessions.py`` |
| Audit | What was done, by whom | ``hcc/security/audit.py`` |

Like :mod:`hcc.security`, nothing here imports FastAPI. A service takes an
``AsyncSession`` and plain values, raises typed exceptions, and leaves the
mapping onto status codes to the API layer — so the same rule is enforced
identically whether it is reached from a route, a future CLI command, or a
test that calls it directly.

**Services do not commit.** The caller does, so that a mutation and the audit
event describing it land together or not at all. The one exception is
recording a *refusal*, which by definition has nothing else to commit with and
is documented where it happens.
"""
