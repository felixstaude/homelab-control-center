"""Semantic versions, parsed and ordered properly.

## Why this file exists at all

Because ``"0.10.0" < "0.9.0"`` is true in Python, and that is the bug this
module is here to make impossible. A string comparison on versions is correct
for the first nine releases of a project and wrong afterwards, which is the
worst possible failure schedule: it passes every test written early and breaks
when there is a fleet to break.

The project already has a `VERSION` file and a version stamped into the agent
binary. What it did not have is a way to answer "is this one newer" that holds
for `0.10.0`, for `0.2.0-beta.1`, and for the pre-release ordering an alpha
channel needs.

## What this implements

Semantic Versioning 2.0.0, the parts that matter here:

* ``MAJOR.MINOR.PATCH``, all required, all non-negative, no leading zeros.
* An optional pre-release: ``-alpha.1``, dot-separated identifiers that are
  either numeric or alphanumeric-with-hyphens.
* Optional build metadata: ``+abc123``. **Parsed and ignored for ordering**,
  per the specification — two versions differing only in build metadata have
  the same precedence. It is kept on the object because a release that names
  its commit is useful to display.

Ordering rules, which are the whole point:

* Numeric parts compare numerically.
* A version *with* a pre-release is **older** than the same version without
  one: ``0.2.0-beta.1 < 0.2.0``. This is the rule people get backwards, and
  getting it backwards means shipping a beta to the stable channel.
* Pre-release identifiers compare left to right. Numeric identifiers compare
  numerically; alphanumeric ones compare as ASCII; a numeric identifier is
  always lower than an alphanumeric one. A shorter run of identifiers is lower
  when all preceding ones are equal, so ``1.0.0-alpha < 1.0.0-alpha.1``.

## What it deliberately does not do

**It does not coerce.** ``"v1.2.3"``, ``"1.2"``, ``"1.2.3.4"`` and ``"latest"``
are all errors, not guesses. A version parser that accepts ``1.2`` has to
decide whether that means ``1.2.0``, and a parser that strips a leading ``v``
has to decide what ``version`` means. Both decisions are better made by the
thing that produced the string — and in this system that thing is the build,
which produces exact versions.

The one accommodation: surrounding whitespace is stripped, because a version
read from a file has a trailing newline.
"""

from __future__ import annotations

import functools
import re
from typing import Final, NamedTuple

#: The grammar, from the specification's own suggested regular expression.
#:
#: Anchored at both ends. The numeric components forbid leading zeros, which is
#: what makes ``01.0.0`` an error rather than a version that sorts strangely.
_PATTERN: Final = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

#: Longer than any legitimate version, and short enough that a hostile value
#: cannot make the regular expression expensive.
MAX_VERSION_LENGTH: Final = 64


class InvalidVersionError(ValueError):
    """A string that is not a semantic version.

    A distinct type rather than a bare ``ValueError`` so that callers reading
    untrusted metadata can tell "this field is malformed" apart from every
    other validation failure, and report it as the former.
    """


class _PrereleasePart(NamedTuple):
    """One pre-release identifier, keyed for comparison.

    ``is_alphanumeric`` comes first so that a numeric identifier sorts below an
    alphanumeric one, which is the specification's rule and is not what a naive
    tuple comparison of mixed types would do — in Python that is a TypeError.
    """

    is_alphanumeric: bool
    numeric: int
    text: str


@functools.total_ordering
class Version:
    """A parsed semantic version.

    Immutable, hashable, and ordered. ``functools.total_ordering`` derives the
    rest from ``__eq__`` and ``__lt__``, so there is one comparison rule here
    rather than six that could disagree.
    """

    __slots__ = ("_key", "build", "major", "minor", "patch", "prerelease", "raw")

    def __init__(self, raw: str) -> None:
        if len(raw) > MAX_VERSION_LENGTH:
            raise InvalidVersionError(
                f"version is {len(raw)} characters, over the {MAX_VERSION_LENGTH} allowed"
            )
        text = raw.strip()
        match = _PATTERN.match(text)
        if match is None:
            raise InvalidVersionError(
                f"{text!r} is not a semantic version. Expected MAJOR.MINOR.PATCH with "
                "optional -prerelease and +build, for example 0.2.0 or 0.2.0-beta.1. "
                "Leading 'v', two-part versions and 'latest' are not accepted."
            )
        self.raw: str = text
        self.major: int = int(match["major"])
        self.minor: int = int(match["minor"])
        self.patch: int = int(match["patch"])
        self.prerelease: tuple[str, ...] = (
            tuple(match["prerelease"].split(".")) if match["prerelease"] else ()
        )
        self.build: str | None = match["build"]
        self._key: tuple[object, ...] = self._comparison_key()

    def _comparison_key(self) -> tuple[object, ...]:
        """Everything precedence depends on, and nothing it does not.

        Build metadata is absent from this on purpose: the specification says
        two versions differing only in build metadata have equal precedence.
        """
        parts = tuple(
            _PrereleasePart(
                is_alphanumeric=not identifier.isdigit(),
                numeric=int(identifier) if identifier.isdigit() else 0,
                text=identifier,
            )
            for identifier in self.prerelease
        )
        # `not self.prerelease` is the rule people invert: a release with no
        # pre-release outranks the same numbers with one, so it must sort
        # *higher*. True > False, so "has no prerelease" is the right key.
        return (self.major, self.minor, self.patch, not self.prerelease, parts)

    @property
    def is_prerelease(self) -> bool:
        """Whether this version carries a pre-release identifier."""
        return bool(self.prerelease)

    @property
    def core(self) -> tuple[int, int, int]:
        """``(major, minor, patch)``, for deciding what kind of step this is."""
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key < other._key

    def __hash__(self) -> int:
        return hash(self._key)


def parse(raw: str) -> Version:
    """Parse *raw*, raising :class:`InvalidVersionError` if it is not a version."""
    return Version(raw)


def try_parse(raw: str | None) -> Version | None:
    """Parse *raw*, or return ``None`` if it is absent or malformed.

    For reading what a *host* reported. An agent's self-reported version is
    advisory — the controller stores whatever string arrived — so a host running
    something unparseable must be representable rather than a crash in the
    update planner. The caller then reports ``unknown_version``, which is a
    state the interface renders, instead of a 500.
    """
    if raw is None:
        return None
    try:
        return Version(raw)
    except InvalidVersionError:
        return None


class StepKind:
    """How large a step between two versions is.

    Plain string constants rather than an enum: these are compared against
    values read from the database and sent over the API, and an enum would mean
    converting at three boundaries to gain nothing.
    """

    MAJOR: Final = "major"
    MINOR: Final = "minor"
    PATCH: Final = "patch"
    PRERELEASE: Final = "prerelease"
    NONE: Final = "none"

    ALL: Final = (MAJOR, MINOR, PATCH, PRERELEASE, NONE)


def step_kind(installed: Version, target: Version) -> str:
    """What kind of step going from *installed* to *target* is.

    This is what an automatic-update policy decides on: "patch releases only"
    has to mean something exact. Measured on the **core** triple, so
    ``0.1.0 → 0.1.1-beta.1`` is a patch step that also happens to be a
    pre-release; the policy checks both facts separately rather than this
    function trying to express them in one word.

    A step *backwards* reports the kind of the difference, not its direction:
    the caller already knows the direction from the ordering and needs to know
    whether a downgrade crosses a major boundary.
    """
    if installed.core == target.core:
        return StepKind.NONE if installed == target else StepKind.PRERELEASE
    if installed.major != target.major:
        return StepKind.MAJOR
    if installed.minor != target.minor:
        return StepKind.MINOR
    return StepKind.PATCH


def newest(versions: list[Version]) -> Version | None:
    """The highest-precedence version, or ``None`` for an empty list."""
    return max(versions) if versions else None
