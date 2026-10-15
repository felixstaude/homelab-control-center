"""Which database migrations are applied, for the compatibility check.

A release may declare ``schema_requirement``: the Alembic revision it needs
present before it will work. Evaluating that needs the **set** of applied
revisions, and ``alembic_version`` holds only the head — so the ancestry has
to come from somewhere.

## Why membership and not ordering

Alembic revisions are a linked list of opaque identifiers. "Is revision X
applied" is exactly the question and is trivially correct; "is X at or before
the head" needs the migration graph and invites an ordering comparison on
strings, which is the same mistake as comparing versions lexicographically.

So this walks the graph once — from the head back through ``down_revision`` —
and produces a set. The walk uses Alembic's own ``ScriptDirectory``, which is
the only thing that knows the shape of the history.

## When the history cannot be read

A deployment whose migration files are absent — a slim image, a checkout
without the package — cannot have its ancestry computed. That returns the head
alone, and the consequence is stated rather than hidden: a release requiring
anything other than the current head is reported as blocked. Failing closed,
because the alternative is installing a release whose migration may not have
run, and the operator sees a blocker naming the revision rather than a host
that breaks after the upgrade.

## Why it is cached

The answer changes only when a migration runs, which cannot happen while the
process is serving: the entrypoint migrates and then starts the application.
Re-walking the history per request would read a dozen files to compute a
constant.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hcc.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

#: Where the migrations live, relative to this package's root.
#:
#: ``apps/server/alembic`` in a checkout and ``/opt/hcc/alembic`` in the
#: container, which is why it is resolved from this file rather than from the
#: working directory — the container's working directory is not the repository
#: root and a relative path would find nothing there.
ALEMBIC_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "alembic"


async def current_revision(db: AsyncSession) -> str | None:
    """The revision the database reports, or ``None`` on an unmigrated one.

    Reads ``alembic_version`` directly rather than through Alembic's runtime,
    which wants a synchronous connection and a configuration object for what
    is one row of one column.
    """
    try:
        row = (await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).first()
    except SQLAlchemyError:
        # Narrow to database errors, which is what can actually go wrong here:
        # on a database nothing has migrated the table does not exist, and
        # that must read as "no revision" rather than as a 500 on every page
        # showing update status.
        logger.warning("alembic_version_unreadable")
        return None
    return str(row[0]) if row else None


@lru_cache(maxsize=1)
def _ancestry() -> dict[str, tuple[str, ...]]:
    """Every revision mapped to itself plus its ancestors.

    Built once. Returns an empty mapping when the migration files cannot be
    read, which the caller turns into "the head alone" — see the module
    docstring for why that fails closed.
    """
    try:
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a hard dependency
        logger.warning("alembic_not_importable")
        return {}

    if not ALEMBIC_DIRECTORY.is_dir():
        logger.warning("alembic_directory_absent", path=str(ALEMBIC_DIRECTORY))
        return {}

    try:
        directory = ScriptDirectory(str(ALEMBIC_DIRECTORY))
        ancestry: dict[str, tuple[str, ...]] = {}
        for script in directory.walk_revisions():
            chain: list[str] = [script.revision]
            # `iterate_revisions` from this revision to the base gives the
            # whole ancestry, which is what "applied" means for a linear
            # history: reaching revision N means every revision behind it ran.
            for ancestor in directory.iterate_revisions(script.revision, "base"):
                if ancestor.revision not in chain:
                    chain.append(ancestor.revision)
            ancestry[script.revision] = tuple(chain)
    except Exception:
        # Broad on purpose: a malformed or partially-copied migration history
        # is a deployment fault, and it must not turn every page that shows
        # update status into a 500. The caller degrades to the head alone,
        # which fails closed.
        logger.exception("alembic_history_unreadable", path=str(ALEMBIC_DIRECTORY))
        return {}
    else:
        return ancestry


async def applied_revisions(db: AsyncSession) -> frozenset[str]:
    """The set of Alembic revisions this database has had applied.

    The head plus its ancestry. An empty set when the database reports no
    revision at all, which is an unmigrated database — and a release
    requiring any revision is then correctly reported as blocked.
    """
    head = await current_revision(db)
    if head is None:
        return frozenset()
    ancestry = _ancestry()
    if head in ancestry:
        return frozenset(ancestry[head])
    # The head is a revision this build's migration files do not contain —
    # a database migrated by a *newer* controller than this one. Reported as
    # the head alone rather than guessed at, and the resulting blocker names
    # the revision, which is more useful than a silent pass.
    logger.warning("alembic_head_not_in_history", head=head)
    return frozenset({head})


def knows_revision(revision: str) -> bool:
    """Whether this build's migration files contain *revision* at all.

    The question "is the database ahead of this image" reduces to this one,
    and it cannot be answered from :func:`applied_revisions` — that reports
    an unknown head as the head alone, deliberately, so that a blocker can
    name it. A caller asking "do I know this revision" needs the direct
    answer.

    Returns ``True`` when the history cannot be read, which fails *open* for
    this question alone and on purpose: claiming "your database is from the
    future" because a file was unreadable would be the more alarming wrong
    answer, and every other use of the history already fails closed.
    """
    ancestry = _ancestry()
    return not ancestry or revision in ancestry


def head_revision() -> str | None:
    """The newest revision this build's migration files contain.

    Distinct from :func:`current_revision`, which is what the *database* has
    applied. The pair is the whole upgrade-readiness question: equal means
    the schema is current, and different means migrations are pending —
    either because the image is newer than the database, or because somebody
    is running an image older than the database it is pointed at, which is
    the dangerous direction and the one worth naming.

    Returns ``None`` when the migration files cannot be read, which the
    caller must treat as "cannot tell" rather than as "nothing pending".
    """
    try:
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a hard dependency
        return None
    if not ALEMBIC_DIRECTORY.is_dir():
        return None
    try:
        heads = ScriptDirectory(str(ALEMBIC_DIRECTORY)).get_heads()
    except Exception:
        # Same reasoning as `_ancestry`: a malformed history is a deployment
        # fault and must not turn a status page into a 500.
        logger.exception("alembic_heads_unreadable", path=str(ALEMBIC_DIRECTORY))
        return None
    return heads[0] if len(heads) == 1 else None


def pending_revisions(applied: frozenset[str]) -> tuple[str, ...]:
    """Revisions this build has and the database has not, oldest first.

    The list an administrator reads before deciding whether to upgrade now or
    tonight. Empty when the schema is current, and empty when the history
    cannot be read — the caller distinguishes those by also asking
    :func:`head_revision`, which is ``None`` in the second case.
    """
    head = head_revision()
    if head is None:
        return ()
    ancestry = _ancestry()
    # Expanded, so a caller that passes a bare head rather than the set
    # `applied_revisions` returns gets the right answer instead of "every
    # migration is pending". Reaching revision N means every revision behind
    # it ran, and that is true however the caller spells it.
    reached: set[str] = set()
    for revision in applied:
        reached.update(ancestry.get(revision, (revision,)))
    everything = ancestry.get(head, ())
    # `_ancestry` orders a revision before its ancestors, so reversing gives
    # the order the migrations would actually run in.
    return tuple(revision for revision in reversed(everything) if revision not in reached)
