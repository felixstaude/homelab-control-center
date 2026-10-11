"""Password hashing unit tests.

No database and no HTTP. Covers the policy, the hashing and verification
contract, failure handling, parameter upgrades, and that neither passwords nor
hashes leak into logs, exceptions or representations.

Deliberately contains **no timing assertions**. Constant-time comparison is
``argon2-cffi``'s responsibility, and a wall-clock test of it would be flaky
on shared CI runners — it would fail for reasons unrelated to the property it
claims to check.

Most tests use :data:`FAST_PARAMETERS`, the lowest parameters the module
permits, so the suite stays quick. The shipped defaults are exercised
separately in :class:`TestDefaultParameters`.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from hcc.config import Settings
from hcc.security.password import (
    DEFAULT_PARAMETERS,
    HASH_LENGTH,
    MAX_MEMORY_COST_KIB,
    MAX_PARALLELISM,
    MAX_PASSWORD_BYTES,
    MAX_TIME_COST,
    MIN_MEMORY_COST_KIB,
    MIN_PARALLELISM,
    MIN_PASSWORD_LENGTH,
    MIN_TIME_COST,
    SALT_LENGTH,
    Argon2Parameters,
    PasswordError,
    PasswordHasher,
    PasswordPolicyError,
    build_password_hasher,
    normalize_password,
    validate_password,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The cheapest parameters the module allows. Using these keeps the suite fast
# (~18 ms per hash instead of ~50 ms) without testing a configuration the
# module would reject.
FAST_PARAMETERS = Argon2Parameters(
    memory_cost=MIN_MEMORY_COST_KIB,
    time_cost=MIN_TIME_COST,
    parallelism=MIN_PARALLELISM,
)

PASSWORD = "correct horse battery staple"


def assert_no_secrets_in(events: Sequence[Mapping[str, Any]], *secrets: str) -> None:
    """Assert no secret appears anywhere in captured log events.

    Checks every key and every value, not just the rendered message, so a
    secret smuggled in as a structured field is caught too.
    """
    rendered = repr(events)
    for secret in secrets:
        assert secret not in rendered, f"secret leaked into a log event: {secret[:12]}..."


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    """A hasher at the cheapest permitted parameters.

    Module-scoped: a hasher holds no secret or mutable state, so sharing one
    across tests is safe and avoids re-initialising it repeatedly.
    """
    return PasswordHasher(FAST_PARAMETERS)


@pytest.fixture(scope="module")
def encoded(hasher: PasswordHasher) -> str:
    """A hash of :data:`PASSWORD`, reused by the read-only verification tests."""
    return hasher.hash(PASSWORD)


# ---------------------------------------------------------------------------
# Policy: normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_nfc_form_is_returned(self) -> None:
        # U+00E9 is already NFC; it must survive unchanged.
        assert normalize_password("café") == "café"

    def test_decomposed_input_is_composed(self) -> None:
        # "e" + U+0301 COMBINING ACUTE ACCENT normalizes to U+00E9. Without
        # this, a user whose platform produces NFD could not log in to an
        # account whose password was set as NFC.
        assert normalize_password("café") == "café"

    def test_equivalent_encodings_hash_identically(self, hasher: PasswordHasher) -> None:
        nfc, nfd = "paßcaféword", "paßcaféword"
        assert nfc != nfd  # genuinely different byte sequences

        assert hasher.verify(hasher.hash(nfc), nfd) is True
        assert hasher.verify(hasher.hash(nfd), nfc) is True

    def test_compatibility_variants_stay_distinct(self, hasher: PasswordHasher) -> None:
        # NFKC would fold fullwidth onto ASCII and collapse these into one
        # password, discarding entropy. NFC must not. This is the deliberate
        # opposite of the username rule, which uses NFKC.
        ascii_form = "adminadmin"
        # U+FF41.. FULLWIDTH LATIN "adminadmin", as escapes: fullwidth
        # characters are indistinguishable from ASCII in a diff.
        fullwidth = "\uff41\uff44\uff4d\uff49\uff4e" * 2

        assert normalize_password(fullwidth) != normalize_password(ascii_form)
        assert hasher.verify(hasher.hash(fullwidth), ascii_form) is False

    def test_superscript_is_not_folded_to_digit(self) -> None:
        # NFKC maps U+00B2 to "2"; NFC does not.
        assert normalize_password("passwor²") != normalize_password("passwor2")

    def test_whitespace_is_preserved(self) -> None:
        # Unlike usernames, passwords are not stripped: trimming would
        # silently alter the secret.
        assert normalize_password("  spaced out  ") == "  spaced out  "
        assert normalize_password("tab\there") == "tab\there"

    def test_case_is_preserved(self) -> None:
        assert normalize_password("MixedCase") == "MixedCase"

    def test_normalization_is_idempotent(self) -> None:
        for raw in ["café", "\uff41\uff44\uff4d", "paßword", "  x  "]:
            once = normalize_password(raw)
            assert normalize_password(once) == once

    def test_output_is_in_nfc(self) -> None:
        for raw in ["café", "ñ", "áb̀c"]:
            assert unicodedata.is_normalized("NFC", normalize_password(raw))

    def test_non_string_input_is_a_type_error(self) -> None:
        with pytest.raises(TypeError, match="must be a string"):
            normalize_password(b"bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Policy: validation
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_a_reasonable_password_is_accepted(self) -> None:
        assert validate_password(PASSWORD) == PASSWORD

    def test_minimum_length_boundary(self) -> None:
        assert validate_password("x" * MIN_PASSWORD_LENGTH)
        with pytest.raises(PasswordPolicyError, match="at least"):
            validate_password("x" * (MIN_PASSWORD_LENGTH - 1))

    def test_empty_password_is_rejected(self) -> None:
        # Argon2 itself will happily hash an empty string, so the policy is
        # the only thing standing between a blank field and a stored
        # credential.
        with pytest.raises(PasswordPolicyError, match="at least"):
            validate_password("")

    def test_whitespace_only_password_is_not_specially_rejected(self) -> None:
        # Eight spaces is a poor password but a legitimate one; since
        # passwords are not stripped, it satisfies the length rule. Recorded
        # to make the decision explicit rather than accidental.
        assert validate_password(" " * MIN_PASSWORD_LENGTH) == " " * MIN_PASSWORD_LENGTH

    def test_no_composition_rules(self) -> None:
        # NIST SP 800-63B: composition rules push users toward predictable
        # patterns. All of these must be accepted.
        for candidate in [
            "alllowercase",
            "ALLUPPERCASE",
            "1234567890",
            "!!!!!!!!!!",
            "\U0001f510\U0001f510\U0001f510\U0001f510\U0001f510\U0001f510\U0001f510\U0001f510",
            "a b c d e f",
        ]:
            assert validate_password(candidate)

    def test_maximum_size_boundary_in_bytes(self) -> None:
        assert validate_password("x" * MAX_PASSWORD_BYTES)
        with pytest.raises(PasswordPolicyError, match="at most"):
            validate_password("x" * (MAX_PASSWORD_BYTES + 1))

    def test_limit_is_measured_in_bytes_not_characters(self) -> None:
        # A 4-byte emoji repeated 300 times is 300 characters but 1200 bytes.
        # A character-based cap would let four times the payload through.
        emoji = "\U0001f510" * 300
        assert len(emoji) == 300
        assert len(emoji.encode("utf-8")) == 1200

        with pytest.raises(PasswordPolicyError, match="bytes"):
            validate_password(emoji)

    def test_size_is_measured_after_normalization(self) -> None:
        # Normalization can change the encoded size, so the limit must be
        # applied to what will actually be hashed.
        decomposed = "é" * 400  # 3 bytes each in NFD, 2 in NFC
        assert len(decomposed.encode("utf-8")) > MAX_PASSWORD_BYTES
        assert len(normalize_password(decomposed).encode("utf-8")) <= MAX_PASSWORD_BYTES

        assert validate_password(decomposed)  # accepted on its normalized size

    def test_over_long_password_is_rejected_not_truncated(self) -> None:
        # The critical property: an over-long password must never be silently
        # shortened, which would both break the user's expectation and make
        # two passwords sharing a long prefix the same credential.
        long_a = "x" * MAX_PASSWORD_BYTES + "AAAA"
        long_b = "x" * MAX_PASSWORD_BYTES + "BBBB"

        with pytest.raises(PasswordPolicyError):
            validate_password(long_a)
        with pytest.raises(PasswordPolicyError):
            validate_password(long_b)

    def test_truncation_would_have_collided(self, hasher: PasswordHasher) -> None:
        # Demonstrates what rejection prevents: if the implementation
        # truncated at the cap, these two distinct passwords would produce
        # interchangeable credentials.
        prefix = "x" * MAX_PASSWORD_BYTES
        truncated_a, truncated_b = (
            (prefix + "AAAA")[:MAX_PASSWORD_BYTES],
            (prefix + "BBBB")[:MAX_PASSWORD_BYTES],
        )
        assert truncated_a == truncated_b  # they would be the same secret

        # The real implementation refuses both instead.
        with pytest.raises(PasswordPolicyError):
            hasher.hash(prefix + "AAAA")

    def test_all_problems_are_reported_at_once(self) -> None:
        # Nothing can be both too short and too long, so this checks the
        # mechanism directly.
        error = PasswordPolicyError(["first problem", "second problem"])
        assert error.problems == ["first problem", "second problem"]
        assert "first problem" in str(error)
        assert "second problem" in str(error)

    def test_policy_error_is_a_value_error(self) -> None:
        # Consistent with the User model's validators, which raise ValueError.
        assert issubclass(PasswordPolicyError, ValueError)
        assert issubclass(PasswordPolicyError, PasswordError)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_correct_password_verifies(self, hasher: PasswordHasher, encoded: str) -> None:
        assert hasher.verify(encoded, PASSWORD) is True

    @pytest.mark.parametrize(
        "wrong",
        [
            "Correct horse battery staple",  # one letter's case
            "correct horse battery stapl",  # one character short
            "correct horse battery staples",  # one character long
            "correct  horse battery staple",  # doubled space
            " correct horse battery staple",  # leading space
            "correct horse battery staple ",  # trailing space
            "entirely different password",
        ],
    )
    def test_incorrect_password_is_rejected(
        self, hasher: PasswordHasher, encoded: str, wrong: str
    ) -> None:
        assert hasher.verify(encoded, wrong) is False

    def test_each_hash_uses_a_unique_salt(self, hasher: PasswordHasher) -> None:
        hashes = {hasher.hash(PASSWORD) for _ in range(5)}

        # Same password, five different encoded strings: the salt is random
        # per call. Without this, identical passwords would be identifiable
        # across accounts and precomputation would become feasible.
        assert len(hashes) == 5

        salts = {h.split("$")[4] for h in hashes}
        assert len(salts) == 5

        # Every one of them still verifies.
        for value in hashes:
            assert hasher.verify(value, PASSWORD) is True

    def test_encoded_hash_format(self, encoded: str) -> None:
        parts = encoded.split("$")

        # $argon2id$v=19$m=...,t=...,p=...$salt$hash
        assert parts[0] == ""
        assert parts[1] == "argon2id"
        assert parts[2] == "v=19"
        assert len(parts) == 6

        settings_part = dict(item.split("=") for item in parts[3].split(","))
        assert int(settings_part["m"]) == FAST_PARAMETERS.memory_cost
        assert int(settings_part["t"]) == FAST_PARAMETERS.time_cost
        assert int(settings_part["p"]) == FAST_PARAMETERS.parallelism

    def test_encoded_hash_names_the_argon2id_variant(self, encoded: str) -> None:
        # Not argon2i or argon2d: RFC 9106 recommends the hybrid for password
        # hashing.
        assert encoded.startswith("$argon2id$")

    def test_parameters_are_embedded_so_upgrades_are_possible(self, encoded: str) -> None:
        # Self-describing hashes are what make needs_rehash work without a
        # separate column recording how each one was produced.
        assert f"m={FAST_PARAMETERS.memory_cost}" in encoded
        assert f"t={FAST_PARAMETERS.time_cost}" in encoded
        assert f"p={FAST_PARAMETERS.parallelism}" in encoded

    def test_hash_fits_the_user_model_column(self) -> None:
        from hcc.models.user import PASSWORD_HASH_MAX_LENGTH

        # Checked at the most expensive parameters the module permits, since
        # larger numbers mean a longer encoded string.
        widest = PasswordHasher(
            Argon2Parameters(
                memory_cost=MAX_MEMORY_COST_KIB,
                time_cost=MAX_TIME_COST,
                parallelism=MAX_PARALLELISM,
            )
        )
        # Build the encoded form without paying for a 4 GiB hash: the length
        # is determined by the parameter digits, salt and digest, all known.
        encoded_length = (
            len(f"$argon2id$v=19$m={MAX_MEMORY_COST_KIB},t={MAX_TIME_COST},p={MAX_PARALLELISM}$")
            + len("A" * 22)
            + 1
            + len("A" * 43)
        )

        assert encoded_length < PASSWORD_HASH_MAX_LENGTH
        assert widest.parameters.memory_cost == MAX_MEMORY_COST_KIB

    def test_hashing_rejects_a_policy_violation(self, hasher: PasswordHasher) -> None:
        # The policy is enforced at hash time, so every stored hash
        # corresponds to a password that satisfied it.
        with pytest.raises(PasswordPolicyError):
            hasher.hash("short")

    def test_hashing_rejects_non_strings(self, hasher: PasswordHasher) -> None:
        with pytest.raises(TypeError):
            hasher.hash(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Unicode
# ---------------------------------------------------------------------------


class TestUnicodePasswords:
    @pytest.mark.parametrize(
        "password",
        [
            "pässwörter",
            "密码密码密码密码",
            "\u043f\u0430\u0440\u043e\u043b\u044c123",  # Cyrillic "parol123"
            "\U0001f510\U0001f511\U0001f512\U0001f513\U0001f510\U0001f511\U0001f512\U0001f513",
            "סיסמהסיס",
            "paßwort1",
            "münchenü",
            "emoji\U0001f680mix",
        ],
    )
    def test_unicode_passwords_round_trip(self, hasher: PasswordHasher, password: str) -> None:
        encoded = hasher.hash(password)

        assert hasher.verify(encoded, password) is True
        assert hasher.verify(encoded, password + "x") is False

    def test_nul_byte_is_accepted_and_not_a_truncation_point(self, hasher: PasswordHasher) -> None:
        # argon2-cffi passes an explicit length to the C library, so a NUL is
        # data rather than a terminator. Verified rather than assumed, because
        # a truncating binding would make "pass\0word" and "pass" the same
        # credential.
        with_nul = "pass\x00word"
        encoded = hasher.hash(with_nul)

        assert hasher.verify(encoded, with_nul) is True
        assert hasher.verify(encoded, "pass") is False
        assert hasher.verify(encoded, "password") is False

    def test_surrogate_pairs_and_astral_plane(self, hasher: PasswordHasher) -> None:
        astral = "\U0001f4a9\U0001f4a9\U0001f4a9\U0001f4a9\U0001f4a9\U0001f4a9\U0001f4a9\U0001f4a9"
        encoded = hasher.hash(astral)
        assert hasher.verify(encoded, astral) is True


# ---------------------------------------------------------------------------
# Verification failure modes
# ---------------------------------------------------------------------------


class TestInvalidHashes:
    @pytest.mark.parametrize(
        ("stored", "label"),
        [
            ("", "empty string"),
            ("not-a-hash", "arbitrary text"),
            ("$argon2id$garbage", "right prefix, nonsense body"),
            ("$argon2id$v=19$m=65536,t=3,p=4$", "truncated after parameters"),
            ("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$", "empty digest"),
            ("$2b$12$abcdefghijklmnopqrstuvwxyz", "a bcrypt hash"),
            ("$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", "wrong argon2 variant"),
            ("$pbkdf2-sha256$29000$salt$hash", "a pbkdf2 hash"),
            ("x" * 300, "a long run of junk"),
        ],
    )
    def test_malformed_stored_hash_returns_false(
        self, hasher: PasswordHasher, stored: str, label: str
    ) -> None:
        # Never raises, never returns True. A malformed hash in the database
        # must not become a 500 on the login path, and must not grant access.
        assert hasher.verify(stored, PASSWORD) is False, label

    def test_tampered_digest_returns_false(self, hasher: PasswordHasher, encoded: str) -> None:
        head, _, digest = encoded.rpartition("$")
        flipped = "B" if digest[0] != "B" else "C"
        assert hasher.verify(f"{head}${flipped}{digest[1:]}", PASSWORD) is False

    def test_tampered_salt_returns_false(self, hasher: PasswordHasher, encoded: str) -> None:
        parts = encoded.split("$")
        parts[4] = "B" + parts[4][1:] if parts[4][0] != "B" else "C" + parts[4][1:]
        assert hasher.verify("$".join(parts), PASSWORD) is False

    def test_non_string_arguments_return_false(self, hasher: PasswordHasher) -> None:
        # A programming error upstream, but the safe answer is still
        # "not verified" rather than an exception on the login path.
        assert hasher.verify(None, PASSWORD) is False  # type: ignore[arg-type]
        assert hasher.verify("$argon2id$...", None) is False  # type: ignore[arg-type]
        assert hasher.verify(b"bytes", PASSWORD) is False  # type: ignore[arg-type]

    def test_oversized_candidate_is_refused_without_hashing(
        self, hasher: PasswordHasher, encoded: str
    ) -> None:
        # The byte cap applies on verification too, so an unauthenticated
        # caller cannot make the server hash a megabyte.
        assert hasher.verify(encoded, "x" * (MAX_PASSWORD_BYTES + 1)) is False

    def test_short_candidate_is_still_verifiable(self, hasher: PasswordHasher) -> None:
        # The *minimum length* must not be enforced on verification: raising
        # the minimum later must not lock out existing users. Simulated by
        # hashing a short password through the policy-free path.
        # Reaching past the policy deliberately: this is the only way to
        # produce the short hash a pre-policy-change user would have.
        legacy = hasher._hasher.hash("old")  # noqa: SLF001

        assert hasher.verify(legacy, "old") is True
        assert hasher.verify(legacy, "wrong") is False

        # ...while new hashes still cannot be created that short.
        with pytest.raises(PasswordPolicyError):
            hasher.hash("old")


# ---------------------------------------------------------------------------
# Parameter upgrades
# ---------------------------------------------------------------------------


class TestRehashDetection:
    def test_current_parameters_need_no_rehash(self, hasher: PasswordHasher, encoded: str) -> None:
        assert hasher.needs_rehash(encoded) is False

    def test_increased_memory_cost_triggers_rehash(self) -> None:
        old = PasswordHasher(FAST_PARAMETERS)
        new = PasswordHasher(
            Argon2Parameters(
                memory_cost=FAST_PARAMETERS.memory_cost * 2,
                time_cost=FAST_PARAMETERS.time_cost,
                parallelism=FAST_PARAMETERS.parallelism,
            )
        )
        stored = old.hash(PASSWORD)

        assert new.needs_rehash(stored) is True
        assert old.needs_rehash(stored) is False

    def test_increased_time_cost_triggers_rehash(self) -> None:
        old = PasswordHasher(FAST_PARAMETERS)
        new = PasswordHasher(
            Argon2Parameters(
                memory_cost=FAST_PARAMETERS.memory_cost,
                time_cost=FAST_PARAMETERS.time_cost + 1,
                parallelism=FAST_PARAMETERS.parallelism,
            )
        )
        assert new.needs_rehash(old.hash(PASSWORD)) is True

    def test_changed_parallelism_triggers_rehash(self) -> None:
        old = PasswordHasher(FAST_PARAMETERS)
        new = PasswordHasher(
            Argon2Parameters(
                memory_cost=FAST_PARAMETERS.memory_cost,
                time_cost=FAST_PARAMETERS.time_cost,
                parallelism=FAST_PARAMETERS.parallelism + 1,
            )
        )
        assert new.needs_rehash(old.hash(PASSWORD)) is True

    def test_old_hash_still_verifies_under_new_parameters(self) -> None:
        # The property that makes transparent upgrades possible: the stored
        # hash is self-describing, so a hasher configured differently can
        # still check it.
        old = PasswordHasher(FAST_PARAMETERS)
        new = PasswordHasher(
            Argon2Parameters(
                memory_cost=FAST_PARAMETERS.memory_cost * 2,
                time_cost=FAST_PARAMETERS.time_cost + 1,
                parallelism=FAST_PARAMETERS.parallelism,
            )
        )
        stored = old.hash(PASSWORD)

        assert new.verify(stored, PASSWORD) is True
        assert new.verify(stored, "wrong password") is False

    def test_upgrade_cycle_produces_a_hash_at_new_parameters(self) -> None:
        # The intended flow once login exists: verify, notice the hash is
        # stale, re-hash with the plaintext that is available only at that
        # moment.
        old = PasswordHasher(FAST_PARAMETERS)
        new = PasswordHasher(
            Argon2Parameters(
                memory_cost=FAST_PARAMETERS.memory_cost * 2,
                time_cost=FAST_PARAMETERS.time_cost,
                parallelism=FAST_PARAMETERS.parallelism,
            )
        )
        stored = old.hash(PASSWORD)

        assert new.verify(stored, PASSWORD) is True
        assert new.needs_rehash(stored) is True

        upgraded = new.hash(PASSWORD)

        assert new.needs_rehash(upgraded) is False
        assert new.verify(upgraded, PASSWORD) is True
        assert upgraded != stored
        assert f"m={FAST_PARAMETERS.memory_cost * 2}" in upgraded

    @pytest.mark.parametrize("stored", ["", "not-a-hash", "$2b$12$abcdefgh"])
    def test_malformed_hash_is_reported_as_needing_rehash(
        self, hasher: PasswordHasher, stored: str
    ) -> None:
        # Such a hash can never verify, so "replace it" is the useful answer.
        # Callers only act on this after a successful verification, so it
        # cannot force a rehash of an unverifiable credential.
        assert hasher.needs_rehash(stored) is True

    def test_non_string_is_reported_as_needing_rehash(self, hasher: PasswordHasher) -> None:
        assert hasher.needs_rehash(None) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parameters and configuration
# ---------------------------------------------------------------------------


class TestParameters:
    def test_defaults_are_the_rfc_9106_low_memory_profile(self) -> None:
        assert DEFAULT_PARAMETERS.memory_cost == 65536
        assert DEFAULT_PARAMETERS.time_cost == 3
        assert DEFAULT_PARAMETERS.parallelism == 4
        assert DEFAULT_PARAMETERS.hash_len == HASH_LENGTH == 32
        assert DEFAULT_PARAMETERS.salt_len == SALT_LENGTH == 16

    def test_memory_cost_in_mib_is_reported_for_display(self) -> None:
        assert DEFAULT_PARAMETERS.memory_cost_mib == 64.0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"memory_cost": MIN_MEMORY_COST_KIB - 1},
            {"memory_cost": MAX_MEMORY_COST_KIB + 1},
            {"time_cost": MIN_TIME_COST - 1},
            {"time_cost": MAX_TIME_COST + 1},
            {"parallelism": MIN_PARALLELISM - 1},
            {"parallelism": MAX_PARALLELISM + 1},
            {"hash_len": 8},
            {"salt_len": 4},
        ],
    )
    def test_out_of_range_parameters_are_rejected(self, overrides: dict[str, int]) -> None:
        # Floors exist so that configuration can tune cost for weak hardware
        # without being able to configure the hashing into uselessness.
        with pytest.raises(ValueError, match="invalid Argon2 parameters"):
            Argon2Parameters(**overrides)

    def test_boundary_parameters_are_accepted(self) -> None:
        assert Argon2Parameters(
            memory_cost=MIN_MEMORY_COST_KIB,
            time_cost=MIN_TIME_COST,
            parallelism=MIN_PARALLELISM,
        )
        assert Argon2Parameters(
            memory_cost=MAX_MEMORY_COST_KIB,
            time_cost=MAX_TIME_COST,
            parallelism=MAX_PARALLELISM,
        )

    def test_every_problem_is_reported_at_once(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            Argon2Parameters(memory_cost=1, time_cost=99, parallelism=99)

        message = str(exc_info.value)
        assert "memory_cost" in message
        assert "time_cost" in message
        assert "parallelism" in message

    def test_the_floors_guarantee_argon2s_per_lane_requirement(self) -> None:
        # Argon2 requires at least 8 KiB per lane. The module checks this, but
        # the enforced floors make it unreachable: the smallest permitted
        # memory cost already covers the largest permitted lane count by a
        # wide margin. Asserting the relationship keeps that true if the
        # bounds are ever retuned.
        assert MIN_MEMORY_COST_KIB >= MAX_PARALLELISM * 8

        # The widest permitted combination is therefore accepted.
        assert Argon2Parameters(
            memory_cost=MIN_MEMORY_COST_KIB,
            time_cost=MIN_TIME_COST,
            parallelism=MAX_PARALLELISM,
        )

    def test_per_lane_requirement_is_enforced_when_it_can_bite(self) -> None:
        # Exercises the check directly by constructing the one shape it
        # guards, bypassing the range checks that would otherwise mask it.
        with pytest.raises(ValueError, match="too small for"):
            Argon2Parameters(
                memory_cost=MIN_MEMORY_COST_KIB,
                time_cost=MIN_TIME_COST,
                parallelism=MIN_MEMORY_COST_KIB // 8 + 1,
            )

    def test_parameters_are_immutable(self) -> None:
        with pytest.raises(AttributeError):
            DEFAULT_PARAMETERS.memory_cost = 1  # type: ignore[misc]

    def test_built_from_settings(self) -> None:
        settings = Settings(
            environment="test",
            database_url="postgresql://u:p@localhost:5432/hcc",
            password_hash_memory_cost=32768,
            password_hash_time_cost=4,
            password_hash_parallelism=2,
        )
        built = build_password_hasher(settings)

        assert built.parameters.memory_cost == 32768
        assert built.parameters.time_cost == 4
        assert built.parameters.parallelism == 2

    def test_settings_enforce_the_same_floors(self) -> None:
        # The Pydantic bounds and the dataclass bounds are separate checks in
        # separate layers; this asserts they have not drifted apart.
        from pydantic import ValidationError

        for field, value in [
            ("password_hash_memory_cost", MIN_MEMORY_COST_KIB - 1),
            ("password_hash_time_cost", MIN_TIME_COST - 1),
            ("password_hash_parallelism", MIN_PARALLELISM - 1),
        ]:
            with pytest.raises(ValidationError):
                Settings(
                    environment="test",
                    database_url="postgresql://u:p@localhost:5432/hcc",
                    **{field: value},  # type: ignore[arg-type]
                )


class TestDefaultParameters:
    """Exercises the shipped defaults, which the rest of the suite trades away
    for speed."""

    def test_round_trip_at_default_parameters(self) -> None:
        hasher = PasswordHasher()
        encoded = hasher.hash(PASSWORD)

        assert hasher.verify(encoded, PASSWORD) is True
        assert hasher.verify(encoded, "wrong") is False
        assert hasher.needs_rehash(encoded) is False
        assert "m=65536,t=3,p=4" in encoded

    def test_default_hasher_uses_default_parameters(self) -> None:
        assert PasswordHasher().parameters == DEFAULT_PARAMETERS
        assert PasswordHasher(None).parameters == DEFAULT_PARAMETERS


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------


class TestAsyncInterface:
    async def test_hash_and_verify_async(self, hasher: PasswordHasher) -> None:
        encoded = await hasher.hash_async(PASSWORD)

        assert await hasher.verify_async(encoded, PASSWORD) is True
        assert await hasher.verify_async(encoded, "wrong") is False

    async def test_async_hash_is_compatible_with_sync_verify(self, hasher: PasswordHasher) -> None:
        encoded = await hasher.hash_async(PASSWORD)
        assert hasher.verify(encoded, PASSWORD) is True

    async def test_async_policy_errors_propagate(self, hasher: PasswordHasher) -> None:
        with pytest.raises(PasswordPolicyError):
            await hasher.hash_async("short")

    async def test_concurrent_hashing_works(self, hasher: PasswordHasher) -> None:
        import asyncio

        # Offloading to threads means several hashes can be in flight. This
        # checks correctness under concurrency, not timing.
        passwords = [f"password-number-{index}" for index in range(4)]
        hashes = await asyncio.gather(*(hasher.hash_async(p) for p in passwords))

        assert len({*hashes}) == 4
        results = await asyncio.gather(
            *(hasher.verify_async(h, p) for h, p in zip(hashes, passwords, strict=True))
        )
        assert all(results)


# ---------------------------------------------------------------------------
# Leakage prevention
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    SECRET = "SuperSecret-Passphrase-9981"

    def test_repr_contains_no_secret(self, hasher: PasswordHasher) -> None:
        rendered = repr(hasher)

        assert self.SECRET not in rendered
        assert "password" not in rendered.lower() or "PasswordHasher" in rendered
        # Parameters are safe and useful to show.
        assert str(FAST_PARAMETERS.memory_cost) in rendered

    def test_policy_error_message_contains_no_password(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password("shrt")

        message = str(exc_info.value)
        assert "shrt" not in message
        assert all("shrt" not in problem for problem in exc_info.value.problems)

    def test_oversize_error_message_contains_no_password(self) -> None:
        secret = "Z" * (MAX_PASSWORD_BYTES + 10)
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password(secret)

        message = str(exc_info.value)
        # The size is reported; the content is not.
        assert str(len(secret)) in message
        assert "Z" * 20 not in message

    def test_wrong_password_is_not_logged_at_all(
        self, hasher: PasswordHasher, encoded: str
    ) -> None:
        # A failed login is attacker-controlled log volume, and the
        # authentication layer records attempts itself.
        with capture_logs() as events:
            assert hasher.verify(encoded, self.SECRET) is False

        assert events == []

    def test_malformed_hash_warning_omits_the_hash(self, hasher: PasswordHasher) -> None:
        malformed = "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$NOT_REAL_DIGEST"

        with capture_logs() as events:
            assert hasher.verify(malformed, self.SECRET) is False

        # The problem is reported, never swallowed into a success...
        assert len(events) == 1
        assert events[0]["event"] == "password_verify_invalid_stored_hash"
        assert events[0]["log_level"] == "warning"
        # ...and the only detail recorded is the exception type.
        assert events[0]["error_type"] in {"InvalidHashError", "VerificationError"}
        assert_no_secrets_in(events, malformed, self.SECRET, "NOT_REAL_DIGEST")

    def test_needs_rehash_warning_omits_the_hash(self, hasher: PasswordHasher) -> None:
        malformed = "$argon2id$totally-bogus-value"

        with capture_logs() as events:
            assert hasher.needs_rehash(malformed) is True

        assert len(events) == 1
        assert events[0]["event"] == "password_needs_rehash_invalid_stored_hash"
        assert_no_secrets_in(events, malformed, "totally-bogus-value")

    def test_oversized_input_log_omits_the_password(self, hasher: PasswordHasher) -> None:
        oversized = "Q" * (MAX_PASSWORD_BYTES + 1)

        with capture_logs() as events:
            assert hasher.verify("$argon2id$x", oversized) is False

        assert len(events) == 1
        assert events[0]["event"] == "password_verify_rejected_oversized_input"
        assert_no_secrets_in(events, oversized)

    def test_bad_argument_type_log_records_types_not_values(self, hasher: PasswordHasher) -> None:
        with capture_logs() as events:
            assert hasher.verify(None, self.SECRET) is False  # type: ignore[arg-type]

        assert len(events) == 1
        assert events[0]["event"] == "password_verify_bad_argument_type"
        # Types are useful for debugging; values are not recorded.
        assert events[0]["encoded_hash_type"] == "NoneType"
        assert events[0]["password_type"] == "str"
        assert_no_secrets_in(events, self.SECRET)

    def test_hash_output_does_not_contain_the_password(self, hasher: PasswordHasher) -> None:
        # Obvious, but it is the whole premise: the stored value must not
        # embed the plaintext.
        encoded = hasher.hash(self.SECRET)
        assert self.SECRET not in encoded


# ---------------------------------------------------------------------------
# Architectural guarantees
# ---------------------------------------------------------------------------


def test_security_package_does_not_import_fastapi() -> None:
    """The security package must stay independent of HTTP.

    Checked in a subprocess with a clean interpreter, because the test session
    has already imported the FastAPI app, so inspecting ``sys.modules`` in
    this process would prove nothing.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hcc.security; "
            "forbidden = [m for m in ('fastapi', 'starlette') if m in sys.modules]; "
            "print(','.join(forbidden))",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"hcc.security pulled in HTTP modules: {result.stdout.strip()}"
    )
