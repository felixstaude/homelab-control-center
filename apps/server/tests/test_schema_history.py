"""Finding the migration history, in both layouts this ships in.

A unit test, and it exists because of a defect only the *image* had.

``hcc/updates/schema.py`` locates the Alembic history relative to its own
file. In a checkout that is ``apps/server/alembic`` and correct. In the
container the package is installed into the venv, so the same expression
resolves to ``site-packages/alembic`` — **Alembic's own library package**. It
exists, it is a directory, and the old code's only check was ``is_dir()``.

So every production image took the Alembic library for a migration history,
and nothing said so. ``ScriptDirectory`` does not object to a directory with
no ``versions/`` — it constructs, and reports a history with no revisions —
so the ancestry came back empty with no log line, ``head_revision()``
returned ``None`` because there was not exactly one head, and the
upgrade-readiness endpoint reported "the schema is current and nothing is in
flight" on a database whose revision came from the future. The exact answer
the endpoint exists to stop somebody acting on.

Nothing caught it until ``deployment-e2e.sh`` ran against the built image,
because every other test runs from a checkout where the first candidate is
right. These tests reproduce both layouts with temporary directories, so the
next person to change the resolution finds out here instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hcc.updates import schema

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_history(root: Path) -> Path:
    """A directory that really is a migration history."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "env.py").write_text("# an Alembic environment\n", encoding="utf-8")
    (root / "versions").mkdir(exist_ok=True)
    return root


def _write_library(root: Path) -> Path:
    """A directory named ``alembic`` that is the library, not a history.

    Shaped like the real thing: ``site-packages/alembic`` has modules at its
    root and a few subpackages, and none of them is ``versions``.
    """
    root.mkdir(parents=True, exist_ok=True)
    for module in ("__init__.py", "command.py", "config.py", "op.py"):
        (root / module).write_text("", encoding="utf-8")
    (root / "script").mkdir(exist_ok=True)
    (root / "runtime").mkdir(exist_ok=True)
    return root


def test_a_real_history_is_recognised(tmp_path: Path) -> None:
    assert schema.is_migration_directory(_write_history(tmp_path / "alembic"))


def test_the_alembic_library_is_not_mistaken_for_one(tmp_path: Path) -> None:
    """The whole defect, in one assertion.

    ``is_dir()`` is true of this directory. So would be a check for
    ``__init__.py``, or for *any* Python file. What distinguishes a history
    is ``env.py`` **and** ``versions/``, and the library has neither.
    """
    library = _write_library(tmp_path / "alembic")

    assert library.is_dir()
    assert not schema.is_migration_directory(library)


def test_the_library_directory_fails_silently_rather_than_loudly(tmp_path: Path) -> None:
    """Why ``is_dir()`` was not merely insufficient but actively misleading.

    The natural assumption is that pointing Alembic at the wrong directory
    raises, and that the broad handler in ``_ancestry`` would then log it. It
    does not. ``ScriptDirectory`` constructs happily against a directory with
    no ``versions/`` and reports a history containing nothing — so the old
    code produced an empty ancestry, no exception, and **no log line**, and
    the only visible symptom was an endpoint quietly answering "current".

    Pinned so that a future reader who decides the validation is paranoid
    can see what the unvalidated version actually does.
    """
    from alembic.script import ScriptDirectory

    library = _write_library(tmp_path / "alembic")

    directory = ScriptDirectory(str(library))

    assert directory.get_heads() == []
    assert list(directory.walk_revisions()) == []


def test_a_directory_with_only_one_marker_is_refused(tmp_path: Path) -> None:
    """Both markers, because either alone has a plausible impostor.

    ``versions/`` is a name anything could use, and a package root full of
    modules is what the library looks like. Requiring one would have let the
    original defect through in a different disguise.
    """
    only_env = tmp_path / "only-env"
    only_env.mkdir()
    (only_env / "env.py").write_text("", encoding="utf-8")
    assert not schema.is_migration_directory(only_env)

    only_versions = tmp_path / "only-versions"
    (only_versions / "versions").mkdir(parents=True)
    assert not schema.is_migration_directory(only_versions)


def test_the_image_layout_is_found_when_the_checkout_one_is_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container, reproduced.

    First candidate is the library — what ``parents[2] / "alembic"`` resolves
    to inside a venv — and the second is where the Dockerfile puts the real
    migrations. The search has to skip the first and find the second, which
    is precisely what the old ``is_dir()`` could not do.
    """
    library = _write_library(tmp_path / "site-packages" / "alembic")
    history = _write_history(tmp_path / "opt" / "hcc" / "alembic")

    monkeypatch.setattr(schema, "CANDIDATE_DIRECTORIES", (library, history))
    schema.reset_history_cache()

    assert schema.alembic_directory() == history


def test_no_history_anywhere_is_none_rather_than_a_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Cannot tell" is a real answer and the caller handles it.

    ``head_revision()`` returns ``None``, which the readiness endpoint must
    treat as "cannot tell" rather than as "nothing pending" — the confusion
    that produced the original wrong answer.
    """
    monkeypatch.setattr(
        schema, "CANDIDATE_DIRECTORIES", (tmp_path / "nowhere", tmp_path / "also-nowhere")
    )
    schema.reset_history_cache()

    assert schema.alembic_directory() is None


def test_this_checkout_resolves_to_its_own_migrations() -> None:
    """The positive control, against the real repository.

    Without it the four tests above would pass against a resolution that
    found nothing anywhere.
    """
    schema.reset_history_cache()
    found = schema.alembic_directory()

    assert found is not None
    assert (found / "versions").is_dir()
    assert any(found.glob("versions/*-0001_baseline.py"))
    assert schema.head_revision() is not None


def test_the_head_is_a_revision_this_build_knows() -> None:
    """The pair the readiness endpoint turns on.

    A build whose ``head_revision()`` is not in its own history would report
    pending migrations against itself for ever.
    """
    schema.reset_history_cache()
    head = schema.head_revision()

    assert head is not None
    assert schema.knows_revision(head)
