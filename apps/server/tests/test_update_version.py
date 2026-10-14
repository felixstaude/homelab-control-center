"""Semantic version parsing and ordering.

The first test in this file is the reason the module exists: a string
comparison gets ``0.9.0`` and ``0.10.0`` backwards, and an update system built
on one would offer a downgrade as an upgrade from the tenth release onwards.

The pre-release ordering tests matter nearly as much. Getting
``0.2.0-beta.1 < 0.2.0`` inverted means a beta outranks the release it precedes
— so a host on the stable channel would be offered a pre-release, which is
exactly the thing channels exist to prevent.
"""

from __future__ import annotations

import itertools

import pytest

from hcc.updates.version import (
    InvalidVersionError,
    StepKind,
    Version,
    newest,
    parse,
    step_kind,
    try_parse,
)


class TestItDoesNotCompareLexicographically:
    def test_ten_is_after_nine(self) -> None:
        # The bug this module exists to prevent. As strings, "0.10.0" sorts
        # before "0.9.0"; as versions it does not.
        assert parse("0.10.0") > parse("0.9.0")
        assert "0.10.0" < "0.9.0"

    def test_a_hundred_is_after_ninety_nine(self) -> None:
        assert parse("1.100.0") > parse("1.99.0")
        assert parse("100.0.0") > parse("99.0.0")
        assert parse("1.0.100") > parse("1.0.99")

    def test_sorting_a_realistic_release_history(self) -> None:
        # Shuffled deliberately: a sort that happened to be stable on
        # already-ordered input would pass without testing anything.
        unordered = [
            "1.0.0",
            "0.9.0",
            "0.10.0",
            "0.2.0",
            "0.2.0-beta.1",
            "0.1.0",
            "0.2.0-alpha.2",
            "0.2.0-alpha.10",
            "0.11.0",
            "0.2.1",
        ]
        assert [str(v) for v in sorted(parse(raw) for raw in unordered)] == [
            "0.1.0",
            "0.2.0-alpha.2",
            "0.2.0-alpha.10",
            "0.2.0-beta.1",
            "0.2.0",
            "0.2.1",
            "0.9.0",
            "0.10.0",
            "0.11.0",
            "1.0.0",
        ]


class TestPrereleaseOrdering:
    def test_a_prerelease_is_older_than_its_release(self) -> None:
        # The rule people invert. Inverting it ships a beta to stable.
        assert parse("0.2.0-beta.1") < parse("0.2.0")
        assert parse("1.0.0-alpha") < parse("1.0.0")
        assert not parse("0.2.0") < parse("0.2.0-beta.1")

    def test_the_specifications_own_ordering_example(self) -> None:
        # Straight from semver 2.0.0 §11, which is the best available check
        # that this implements the standard rather than something like it.
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        parsed = [parse(raw) for raw in ordered]
        for lower, higher in itertools.pairwise(parsed):
            assert lower < higher, f"{lower} should be below {higher}"

    def test_numeric_identifiers_compare_numerically(self) -> None:
        # "11" vs "2": as text, "11" < "2". As a numeric identifier it is not.
        assert parse("1.0.0-beta.2") < parse("1.0.0-beta.11")

    def test_a_numeric_identifier_is_below_an_alphanumeric_one(self) -> None:
        assert parse("1.0.0-1") < parse("1.0.0-alpha")

    def test_fewer_identifiers_sorts_lower_when_the_prefix_matches(self) -> None:
        assert parse("1.0.0-alpha") < parse("1.0.0-alpha.1")

    def test_mixed_numeric_and_alphanumeric_does_not_raise(self) -> None:
        # Comparing an int with a str is a TypeError in Python, so a naive
        # tuple key would crash here rather than ordering.
        assert parse("1.0.0-1.alpha") < parse("1.0.0-alpha.1")


class TestBuildMetadata:
    def test_build_metadata_is_kept(self) -> None:
        assert parse("0.1.0+abc123").build == "abc123"
        assert parse("0.1.0").build is None

    def test_build_metadata_does_not_affect_precedence(self) -> None:
        # The specification is explicit: equal precedence. Two artifacts of the
        # same release built from different commits must not look like an
        # upgrade from one another.
        assert parse("0.1.0+abc123") == parse("0.1.0+def456")
        assert not parse("0.1.0+abc123") < parse("0.1.0+def456")
        assert parse("0.1.0") == parse("0.1.0+abc123")

    def test_equal_precedence_means_equal_hashes(self) -> None:
        # Otherwise a dict or set keyed on versions disagrees with ==.
        assert hash(parse("0.1.0+abc")) == hash(parse("0.1.0"))
        assert len({parse("0.1.0+abc"), parse("0.1.0+def"), parse("0.1.0")}) == 1


class TestItRefusesRatherThanGuessing:
    @pytest.mark.parametrize(
        "raw",
        [
            "v1.2.3",  # a leading v is a tag convention, not a version
            "1.2",  # two parts: 1.2.0? 1.2.x?
            "1.2.3.4",  # four parts
            "latest",
            "",
            "   ",
            "1.2.3-",  # empty prerelease
            "1.2.3+",  # empty build
            "01.0.0",  # leading zero
            "1.02.0",
            "1.0.00",
            "-1.0.0",
            "1.0.0-alpha..1",  # empty identifier
            "1.0.0_alpha",
            "0x10.0.0",
            "1.0.0-alpha+",
            "1.0.0 extra",
        ],
    )
    def test_a_non_version_is_an_error(self, raw: str) -> None:
        # Coercion is the wrong behaviour: a parser that accepts "1.2" has to
        # invent the third component, and the thing that produced the string
        # is better placed to be exact.
        with pytest.raises(InvalidVersionError):
            parse(raw)

    def test_the_error_says_what_was_expected(self) -> None:
        with pytest.raises(InvalidVersionError, match=r"MAJOR\.MINOR\.PATCH"):
            parse("v1.2.3")

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        # The one accommodation: a version read from a file has a newline.
        assert parse("  0.1.0\n") == parse("0.1.0")
        assert parse("0.1.0\n").raw == "0.1.0"

    def test_an_absurdly_long_string_is_refused_before_parsing(self) -> None:
        # Bounded so that untrusted metadata cannot make the regular
        # expression expensive.
        with pytest.raises(InvalidVersionError, match="over the"):
            parse("1.0.0-" + "a" * 200)

    def test_try_parse_returns_none_instead_of_raising(self) -> None:
        # For a version a *host* reported. That string is advisory and arrives
        # from outside, so an unparseable one has to be a renderable state
        # rather than a 500 in the update planner.
        assert try_parse("not a version") is None
        assert try_parse(None) is None
        assert try_parse("0.1.0") == parse("0.1.0")


class TestTheRepositoriesOwnVersion:
    def test_the_version_file_parses(self) -> None:
        # If this fails, the build is stamping something this module cannot
        # order, and every comparison against it is already broken.
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        raw = (root / "VERSION").read_text(encoding="utf-8")

        assert parse(raw).core == (0, 1, 0)


class TestStepKind:
    @pytest.mark.parametrize(
        ("installed", "target", "expected"),
        [
            ("0.1.0", "0.1.1", StepKind.PATCH),
            ("0.1.0", "0.2.0", StepKind.MINOR),
            ("0.1.0", "1.0.0", StepKind.MAJOR),
            ("0.1.0", "0.1.0", StepKind.NONE),
            ("0.1.0", "0.1.0+build", StepKind.NONE),
            ("0.2.0-beta.1", "0.2.0", StepKind.PRERELEASE),
            ("0.2.0", "0.2.0-beta.1", StepKind.PRERELEASE),
            # Backwards steps report the kind, not the direction.
            ("0.2.0", "0.1.0", StepKind.MINOR),
            ("1.0.0", "0.9.0", StepKind.MAJOR),
            # A minor change outranks a patch change when both differ.
            ("0.1.5", "0.2.1", StepKind.MINOR),
            ("0.1.5", "1.2.3", StepKind.MAJOR),
        ],
    )
    def test_the_step_between_two_versions(
        self, installed: str, target: str, expected: str
    ) -> None:
        assert step_kind(parse(installed), parse(target)) == expected

    def test_a_patch_step_to_a_prerelease_is_still_a_patch_step(self) -> None:
        # Measured on the core triple. "Is this a patch step" and "is the
        # target a pre-release" are two separate facts, and a policy checks
        # both — collapsing them into one word would make "patch releases
        # only" silently admit pre-releases or silently refuse them.
        assert step_kind(parse("0.1.0"), parse("0.1.1-beta.1")) == StepKind.PATCH
        assert parse("0.1.1-beta.1").is_prerelease


class TestNewest:
    def test_it_picks_the_highest_precedence(self) -> None:
        assert newest([parse(v) for v in ["0.9.0", "0.10.0", "0.2.0"]]) == parse("0.10.0")

    def test_an_empty_list_has_no_newest(self) -> None:
        assert newest([]) is None

    def test_a_prerelease_does_not_win_over_its_release(self) -> None:
        assert newest([parse("1.0.0-rc.1"), parse("1.0.0")]) == parse("1.0.0")


class TestVersionObjectBehaviour:
    def test_it_is_hashable_and_usable_as_a_key(self) -> None:
        mapping = {parse("0.1.0"): "first", parse("0.2.0"): "second"}

        assert mapping[parse("0.1.0")] == "first"

    def test_str_round_trips_the_input(self) -> None:
        assert str(parse("0.2.0-beta.1+abc")) == "0.2.0-beta.1+abc"

    def test_repr_is_unambiguous(self) -> None:
        assert repr(parse("0.1.0")) == "Version('0.1.0')"

    def test_comparing_with_a_non_version_is_not_an_accident(self) -> None:
        # Returning NotImplemented lets Python raise TypeError rather than
        # producing a silently wrong answer against a bare string.
        assert parse("0.1.0") != "0.1.0"
        with pytest.raises(TypeError):
            _ = parse("0.1.0") < "0.2.0"

    def test_it_has_no_mutable_state(self) -> None:
        # __slots__, so a typo'd attribute is an error rather than a field
        # nobody reads.
        with pytest.raises(AttributeError):
            parse("0.1.0").patchh = 2  # type: ignore[attr-defined]

    def test_is_prerelease(self) -> None:
        assert parse("1.0.0-alpha").is_prerelease
        assert not parse("1.0.0").is_prerelease
        assert not parse("1.0.0+build").is_prerelease

    def test_the_version_type_is_exported_for_annotations(self) -> None:
        assert isinstance(parse("0.1.0"), Version)
