"""Origin parsing and validation.

Milestone 1.5. The CSRF check compares origins as strings, so it is only as
good as the normalisation on both sides of that comparison. These tests pin
the canonical form and the rejections.
"""

from __future__ import annotations

import pytest

from hcc.security.origins import (
    InvalidOriginError,
    normalize_configured_origin,
    normalize_origin,
    origin_from_parts,
)


class TestNormalizeOrigin:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://hcc.example.com", "https://hcc.example.com"),
            # A trailing slash is the one tolerated extra: operators paste it
            # habitually and rejecting it would be pedantry.
            ("https://hcc.example.com/", "https://hcc.example.com"),
            # Scheme and host are case-insensitive; the path is not, but there
            # is no path here.
            ("HTTPS://HCC.Example.COM", "https://hcc.example.com"),
            # A default port is dropped so the two spellings compare equal.
            ("https://hcc.example.com:443", "https://hcc.example.com"),
            ("http://hcc.example.com:80", "http://hcc.example.com"),
            # A non-default port is significant and kept.
            ("http://localhost:5173", "http://localhost:5173"),
            ("https://hcc.example.com:8443", "https://hcc.example.com:8443"),
            # IPv6 literals keep their brackets, as the Origin header sends
            # them.
            ("http://[::1]:8000", "http://[::1]:8000"),
            ("  https://hcc.example.com  ", "https://hcc.example.com"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        assert normalize_origin(raw) == expected

    def test_normalisation_is_idempotent(self) -> None:
        once = normalize_origin("HTTPS://Example.COM:443/")
        assert normalize_origin(once) == once

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            # No scheme: not an origin, and "example.com" would otherwise be
            # parsed as a path.
            "example.com",
            "//example.com",
            # Wrong scheme.
            "ftp://example.com",
            "javascript://example.com",
            "file://example.com",
            # A URL, not an origin. Silently discarding the path would hide a
            # configuration mistake.
            "https://example.com/app",
            "https://example.com/?q=1",
            "https://example.com/#frag",
            # Credentials have no business in an origin.
            "https://user:pass@example.com",
            # No host.
            "https://",
            # Invalid port.
            "https://example.com:notaport",
        ],
    )
    def test_rejections(self, raw: str) -> None:
        with pytest.raises(InvalidOriginError):
            normalize_origin(raw)

    def test_the_error_is_a_value_error(self) -> None:
        # So Pydantic surfaces it as a field validation error at startup.
        assert issubclass(InvalidOriginError, ValueError)

    def test_the_error_names_the_problem(self) -> None:
        with pytest.raises(InvalidOriginError, match="no path"):
            normalize_origin("https://example.com/app")


class TestNormalizeConfiguredOrigin:
    def test_accepts_a_real_origin(self) -> None:
        assert normalize_configured_origin("https://hcc.example.com/") == "https://hcc.example.com"

    def test_rejects_the_wildcard(self) -> None:
        # The verified defect: this API sends session cookies, and Starlette
        # answers a wildcard-plus-credentials configuration by echoing
        # whichever origin asked — so any website could read authenticated
        # responses.
        with pytest.raises(InvalidOriginError, match="not permitted"):
            normalize_configured_origin("*")

    def test_the_wildcard_rejection_explains_the_alternative(self) -> None:
        with pytest.raises(InvalidOriginError, match="same-origin"):
            normalize_configured_origin("*")

    @pytest.mark.parametrize("value", ["null", "NULL", " null "])
    def test_rejects_the_opaque_origin(self, value: str) -> None:
        # Allow-listing "null" would permit sandboxed iframes and file://
        # documents — exactly the contexts an origin check excludes.
        with pytest.raises(InvalidOriginError, match="opaque"):
            normalize_configured_origin(value)


class TestOriginFromParts:
    def test_builds_a_canonical_origin(self) -> None:
        assert origin_from_parts("https", "hcc.example.com:443") == "https://hcc.example.com"

    def test_returns_none_for_an_unusable_pair(self) -> None:
        # So a caller can treat "cannot determine my own origin" as a failure
        # rather than comparing against a bogus string.
        assert origin_from_parts("https", "") is None
        assert origin_from_parts("ftp", "example.com") is None
