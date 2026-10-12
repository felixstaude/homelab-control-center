"""First-run setup status.

Phase 1, milestone 1.6. One public endpoint, returning one boolean, so the web
client can tell "this installation needs initializing" from "log in".

## Why this is public, and why that is safe

The web client has to make this distinction *before* any credential exists, so
the endpoint cannot require a session. Being public, everything it returns is
returned to anyone who can reach the port — so it returns as little as
possible: a single boolean.

The disclosure is safe because of how bootstrap works. There is **no public
path to claim an uninitialized installation** (ADR-033): the initial
administrator is created by a local command that proves access to the
database. So learning that an installation is uninitialized tells an attacker
nothing they can act on. Had bootstrap been an HTTP endpoint, this status would
have been a starting gun; because it is not, it is a label on a locked door.

What it deliberately does **not** return: when initialization happened, how
many accounts exist, any username, the schema version, or anything about the
database. Those are either useful to an attacker or useless to the client.

## Why not infer it from the login endpoint instead

The tempting alternative — let the client try to log in and read a distinctive
error — would mean making login's failure responses non-uniform. Login returns
one identical response for every cause precisely so it cannot be used as a
username oracle (``hcc/security/authentication.py``). Adding an
"installation not initialized" variant would reopen that, and would do it on
the one endpoint where it matters most.

## Cost

One query against a table that holds at most one row, on an unauthenticated
endpoint. That is the same order of work as ``/readyz``, which already runs
``SELECT 1`` for anonymous callers, and far less than the Argon2 verification
``/api/v1/auth/login`` performs. No additional throttling is applied; if that
ever changes, the login throttle is the mechanism to reuse.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from hcc.api.deps import SessionDep
from hcc.security.bootstrap import is_initialized

#: Public by necessity — the client needs this before it has a credential.
#: Mounted on the public router in :mod:`hcc.api.v1.router`.
public_router = APIRouter(prefix="/setup", tags=["setup"])


class SetupStatus(BaseModel):
    """Whether this installation has been initialized.

    Deliberately one field. See the module docstring for what is excluded and
    why.
    """

    initialized: bool = Field(
        description=(
            "True once an initial administrator has been created. False means "
            "the operator must run `hcc bootstrap-admin` on the controller "
            "host; there is no way to do it over HTTP."
        )
    )


@public_router.get(
    "/status",
    summary="Whether first-run setup is complete",
    response_model=SetupStatus,
)
async def setup_status(db: SessionDep) -> SetupStatus:
    """Report whether an initial administrator exists.

    Reads the installation row rather than counting users, matching the
    invariant bootstrap itself enforces (ADR-033): an installation stays
    initialized even if every account is later removed, so this must not start
    reporting "setup required" when the last user is deleted.
    """
    return SetupStatus(initialized=await is_initialized(db) is not None)
