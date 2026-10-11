"""Token generation and storage-hashing unit tests.

No database, no HTTP. Covers entropy, encoding, the hashing contract and
constant-time comparison.
"""

from __future__ import annotations

import base64
import hashlib
import string

import pytest

from hcc.security.tokens import (
    TOKEN_BYTES,
    TOKEN_HASH_LENGTH,
    TOKEN_LENGTH,
    generate_token,
    hash_token,
    looks_like_token,
    tokens_equal,
)


class TestGeneration:
    def test_token_has_the_documented_length(self) -> None:
        assert len(generate_token()) == TOKEN_LENGTH == 43

    def test_token_decodes_to_the_documented_entropy(self) -> None:
        token = generate_token()
        # Unpadded base64 must be re-padded before decoding.
        padded = token + "=" * (-len(token) % 4)
        assert len(base64.urlsafe_b64decode(padded)) == TOKEN_BYTES == 32

    def test_tokens_are_unique(self) -> None:
        # A repeat would mean the generator is not random — and a repeated
        # session token is a session handed to the wrong person.
        tokens = {generate_token() for _ in range(1000)}
        assert len(tokens) == 1000

    def test_token_uses_only_url_safe_characters(self) -> None:
        # Keeps the value free of anything needing escaping in a cookie, a
        # header or a URL.
        permitted = set(string.ascii_letters + string.digits + "-_")
        for _ in range(100):
            assert set(generate_token()) <= permitted

    def test_token_carries_no_padding(self) -> None:
        for _ in range(100):
            assert "=" not in generate_token()

    def test_generated_tokens_pass_the_shape_check(self) -> None:
        for _ in range(100):
            assert looks_like_token(generate_token())


class TestHashing:
    def test_hash_is_hex_sha256(self) -> None:
        token = generate_token()
        digest = hash_token(token)

        assert len(digest) == TOKEN_HASH_LENGTH == 64
        assert set(digest) <= set(string.hexdigits.lower())
        assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_hash_is_deterministic(self) -> None:
        # Required: the lookup finds a session by hashing the presented token,
        # so a salted or otherwise non-deterministic hash would force a full
        # table scan.
        token = generate_token()
        assert hash_token(token) == hash_token(token)

    def test_different_tokens_hash_differently(self) -> None:
        hashes = {hash_token(generate_token()) for _ in range(500)}
        assert len(hashes) == 500

    def test_hash_does_not_contain_the_token(self) -> None:
        token = generate_token()
        assert token not in hash_token(token)

    def test_hash_rejects_non_strings(self) -> None:
        with pytest.raises(TypeError, match="must be a string"):
            hash_token(b"bytes")  # type: ignore[arg-type]

    def test_unicode_input_hashes_without_error(self) -> None:
        # Not a case we generate, but a presented cookie is arbitrary input
        # and must not raise on the lookup path.
        assert len(hash_token("tökén-💥")) == TOKEN_HASH_LENGTH


class TestComparison:
    def test_identical_tokens_compare_equal(self) -> None:
        token = generate_token()
        assert tokens_equal(token, token) is True

    def test_different_tokens_compare_unequal(self) -> None:
        assert tokens_equal(generate_token(), generate_token()) is False

    def test_shared_prefix_compares_unequal(self) -> None:
        # The case a naive `==` would leak through timing.
        token = generate_token()
        assert tokens_equal(token, token[:-1] + ("A" if token[-1] != "A" else "B")) is False

    def test_empty_and_mismatched_lengths(self) -> None:
        assert tokens_equal("", "") is True
        assert tokens_equal("", generate_token()) is False
        assert tokens_equal("short", "longer-value") is False

    @pytest.mark.parametrize(
        ("left", "right"),
        [(None, "x"), ("x", None), (None, None), (b"x", "x"), (123, "x")],
    )
    def test_non_strings_compare_unequal_without_raising(self, left: object, right: object) -> None:
        # This sits on the request path: a malformed header is a rejected
        # request, not a 500.
        assert tokens_equal(left, right) is False  # type: ignore[arg-type]

    def test_unicode_compares_without_error(self) -> None:
        assert tokens_equal("tökén", "tökén") is True
        assert tokens_equal("tökén", "token") is False


class TestShapeCheck:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "short",
            "x" * (TOKEN_LENGTH - 1),
            "x" * (TOKEN_LENGTH + 1),
            "x" * 10000,
            "a" * 42 + "!",  # right length, illegal character
            "a" * 42 + "=",  # padding is stripped from real tokens
            "a" * 42 + "/",  # standard base64, not URL-safe
            "a" * 42 + "+",
        ],
    )
    def test_rejects_values_that_cannot_be_tokens(self, value: str) -> None:
        assert looks_like_token(value) is False

    @pytest.mark.parametrize("value", [None, b"x" * 43, 123, []])
    def test_rejects_non_strings(self, value: object) -> None:
        assert looks_like_token(value) is False  # type: ignore[arg-type]

    def test_accepts_a_well_formed_token(self) -> None:
        assert looks_like_token("a" * 43) is True
        assert looks_like_token("-" * 43) is True
        assert looks_like_token("_" * 43) is True

    def test_is_not_a_security_check(self) -> None:
        # Documents the contract: passing this means "worth a database
        # lookup", not "authenticated". A well-formed token that was never
        # issued must still resolve to nothing, which is asserted in the
        # session integration tests.
        forged = "a" * TOKEN_LENGTH
        assert looks_like_token(forged) is True
        assert hash_token(forged) != hash_token(generate_token())
