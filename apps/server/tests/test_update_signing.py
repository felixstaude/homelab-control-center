"""Release metadata authenticity.

The tests that matter here are the negative ones. A signature verifier that
accepts a valid signature is easy; one that cannot be talked into accepting an
invalid one is the point, because the thing it is protecting is "install this
binary as root on every machine in the fleet".

Six distinct outcomes are asserted separately rather than through a boolean,
because collapsing them loses the difference between "nobody signed this" — a
missing release step — and "somebody signed this badly", which is an attack.
"""

from __future__ import annotations

import base64
import json

import pytest

from hcc.updates.signing import (
    ALGORITHM,
    PUBLIC_KEY_BYTES,
    SIGNATURE_BYTES,
    SignatureStatus,
    SigningError,
    TrustStore,
    decode_public_key,
    encode_public_key,
    generate_keypair,
    key_id,
    read_signature,
    sign_manifest,
    signature_path_for,
    trust_store_from_settings,
    verify_manifest,
)

MANIFEST = b'{"schema_version": 2, "recommended": "0.1.0"}'


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    """One keypair for the module. Generation is cheap but not free."""
    return generate_keypair()


@pytest.fixture(scope="module")
def other_keypair() -> tuple[bytes, bytes]:
    return generate_keypair()


def sig_bytes(payload: bytes, private_raw: bytes) -> bytes:
    return json.dumps(sign_manifest(payload, private_raw)).encode("utf-8")


class TestAValidSignature:
    def test_it_verifies(self, keypair: tuple[bytes, bytes]) -> None:
        private, public = keypair
        store = TrustStore([encode_public_key(public)])

        result = verify_manifest(MANIFEST, sig_bytes(MANIFEST, private), store)

        assert result.status is SignatureStatus.VALID
        assert result.is_authentic
        assert result.key_id == key_id(public)

    def test_only_valid_is_authentic(self) -> None:
        # Asserted against every member, so adding a status cannot land on the
        # trusted side of the test by default.
        authentic = [s for s in SignatureStatus if s.is_authentic]

        assert authentic == [SignatureStatus.VALID]

    def test_a_store_with_several_keys_finds_the_right_one(
        self, keypair: tuple[bytes, bytes], other_keypair: tuple[bytes, bytes]
    ) -> None:
        private, public = keypair
        _, other_public = other_keypair
        store = TrustStore([encode_public_key(other_public), encode_public_key(public)])

        result = verify_manifest(MANIFEST, sig_bytes(MANIFEST, private), store)

        assert result.status is SignatureStatus.VALID
        assert len(store) == 2


class TestItRefusesEverythingElse:
    def test_a_tampered_manifest_does_not_verify(self, keypair: tuple[bytes, bytes]) -> None:
        # The attack this exists to stop: the bytes changed after signing.
        private, public = keypair
        store = TrustStore([encode_public_key(public)])
        signature = sig_bytes(MANIFEST, private)

        result = verify_manifest(MANIFEST + b" ", signature, store)

        assert result.status is SignatureStatus.INVALID
        assert not result.is_authentic

    def test_a_single_flipped_byte_does_not_verify(self, keypair: tuple[bytes, bytes]) -> None:
        private, public = keypair
        store = TrustStore([encode_public_key(public)])
        altered = MANIFEST.replace(b"0.1.0", b"0.1.1")
        assert altered != MANIFEST

        result = verify_manifest(altered, sig_bytes(MANIFEST, private), store)

        assert result.status is SignatureStatus.INVALID

    def test_a_signature_from_an_untrusted_key_is_untrusted_not_invalid(
        self, other_keypair: tuple[bytes, bytes], keypair: tuple[bytes, bytes]
    ) -> None:
        # The distinction an operator most needs. "Untrusted key" means add the
        # key or check who signed this; "invalid" means something is wrong with
        # the bytes. Reporting both as "not valid" loses that.
        other_private, _ = other_keypair
        _, public = keypair
        store = TrustStore([encode_public_key(public)])

        result = verify_manifest(MANIFEST, sig_bytes(MANIFEST, other_private), store)

        assert result.status is SignatureStatus.UNTRUSTED_KEY
        assert not result.is_authentic
        assert result.detail is not None
        assert "not trusted here" in result.detail

    def test_an_empty_trust_store_trusts_nothing(self, keypair: tuple[bytes, bytes]) -> None:
        # There is no trust-on-first-use. An update mechanism that learned keys
        # from the thing it is verifying would be verifying nothing.
        private, _ = keypair

        result = verify_manifest(MANIFEST, sig_bytes(MANIFEST, private), TrustStore([]))

        assert result.status is SignatureStatus.UNTRUSTED_KEY
        assert result.detail is not None
        assert "none configured" in result.detail

    def test_a_signature_claiming_a_trusted_key_it_did_not_use_is_invalid(
        self, keypair: tuple[bytes, bytes], other_keypair: tuple[bytes, bytes]
    ) -> None:
        # The key_id is a selector, not a credential. Relabelling a signature
        # with a trusted key's identifier must not make it verify — and the
        # outcome is INVALID, because the bytes really are wrong for that key.
        _, public = keypair
        other_private, _ = other_keypair
        store = TrustStore([encode_public_key(public)])
        document = sign_manifest(MANIFEST, other_private)
        document["key_id"] = key_id(public)

        result = verify_manifest(MANIFEST, json.dumps(document).encode(), store)

        assert result.status is SignatureStatus.INVALID

    def test_an_absent_signature_is_absent_not_invalid(self) -> None:
        # The state of this repository today: nothing published, nothing
        # signed. The caller decides whether that is acceptable; it is for an
        # install command a human runs, and it is not for an automatic update.
        result = verify_manifest(MANIFEST, None, TrustStore([]))

        assert result.status is SignatureStatus.ABSENT
        assert not result.is_authentic
        assert result.detail is not None
        assert "Automatic updates require one" in result.detail

    @pytest.mark.parametrize(
        ("document", "fragment"),
        [
            (b"{not json", "not valid JSON"),
            (b"[]", "not a JSON object"),
            (b'{"key_id": "x", "signature": "AA=="}', "'algorithm'"),
            (b'{"algorithm": "ed25519", "signature": "AA=="}', "'key_id'"),
            (b'{"algorithm": "ed25519", "key_id": "x"}', "'signature'"),
            (b'{"algorithm": "", "key_id": "x", "signature": "AA=="}', "'algorithm'"),
            (b'{"algorithm": 1, "key_id": "x", "signature": "AA=="}', "'algorithm'"),
            (
                b'{"algorithm": "ed25519", "key_id": "x", "signature": "not base64!"}',
                "not valid base64",
            ),
            (
                b'{"algorithm": "ed25519", "key_id": "x", "signature": "AA=="}',
                "decodes to 1 bytes",
            ),
        ],
    )
    def test_a_malformed_signature_file_is_malformed(self, document: bytes, fragment: str) -> None:
        result = verify_manifest(MANIFEST, document, TrustStore([]))

        assert result.status is SignatureStatus.MALFORMED
        assert result.detail is not None
        assert fragment in result.detail

    def test_an_oversized_signature_file_is_refused_before_parsing(self) -> None:
        result = verify_manifest(MANIFEST, b"{" + b"a" * 20_000, TrustStore([]))

        assert result.status is SignatureStatus.MALFORMED
        assert result.detail is not None
        assert "over the" in result.detail

    def test_another_algorithm_is_refused_rather_than_negotiated(
        self, keypair: tuple[bytes, bytes]
    ) -> None:
        # Algorithm agility is how downgrade attacks get a foothold: a verifier
        # that accepts whatever the document names accepts the weakest thing it
        # knows. The verifier decides, not the document.
        private, public = keypair
        store = TrustStore([encode_public_key(public)])
        document = sign_manifest(MANIFEST, private)
        document["algorithm"] = "md5-rsa-whatever"

        result = verify_manifest(MANIFEST, json.dumps(document).encode(), store)

        assert result.status is SignatureStatus.UNSUPPORTED_ALGORITHM
        assert result.detail is not None
        assert ALGORITHM in result.detail

    def test_the_empty_signature_of_an_empty_manifest_is_not_a_bypass(
        self, keypair: tuple[bytes, bytes]
    ) -> None:
        # A signature over b"" must not verify an arbitrary manifest.
        private, public = keypair
        store = TrustStore([encode_public_key(public)])

        result = verify_manifest(MANIFEST, sig_bytes(b"", private), store)

        assert result.status is SignatureStatus.INVALID


class TestKeyHandling:
    def test_a_key_id_is_derived_from_the_key(self, keypair: tuple[bytes, bytes]) -> None:
        # Derived rather than assigned, so two parties naming the same key
        # agree without coordinating and a key cannot be given a misleading
        # name.
        _, public = keypair

        assert key_id(public) == key_id(public)
        assert len(key_id(public)) == 16

    def test_different_keys_have_different_ids(
        self, keypair: tuple[bytes, bytes], other_keypair: tuple[bytes, bytes]
    ) -> None:
        assert key_id(keypair[1]) != key_id(other_keypair[1])

    def test_base64_round_trips(self, keypair: tuple[bytes, bytes]) -> None:
        _, public = keypair

        assert decode_public_key(encode_public_key(public)) == public

    def test_an_encoded_key_is_a_single_safe_line(self, keypair: tuple[bytes, bytes]) -> None:
        # It has to survive being stamped into a Go binary by `-ldflags -X`,
        # which means one line with nothing shell-significant in it.
        encoded = encode_public_key(keypair[1])

        assert "\n" not in encoded
        assert len(encoded) == 44
        assert all(c.isalnum() or c in "+/=" for c in encoded)

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ("", "empty"),
            ("   ", "empty"),
            ("not base64!!", "not valid base64"),
            (base64.b64encode(b"short").decode(), "decodes to 5 bytes"),
            (base64.b64encode(b"x" * 64).decode(), "decodes to 64 bytes"),
        ],
    )
    def test_a_bad_public_key_is_refused_at_configuration_time(
        self, value: str, fragment: str
    ) -> None:
        # At configuration time rather than at the first verification, so a
        # typo in a deployment is a startup error and not a silent trust store
        # with one fewer key in it than the operator thinks.
        with pytest.raises(SigningError, match=fragment):
            decode_public_key(value)

    def test_building_a_trust_store_with_a_bad_key_raises(self) -> None:
        with pytest.raises(SigningError):
            TrustStore(["not a key"])

    def test_key_ids_are_sorted_for_stable_display(
        self, keypair: tuple[bytes, bytes], other_keypair: tuple[bytes, bytes]
    ) -> None:
        store = TrustStore([encode_public_key(keypair[1]), encode_public_key(other_keypair[1])])

        assert list(store.key_ids) == sorted(store.key_ids)

    def test_an_empty_store_is_falsey(self) -> None:
        assert not TrustStore([])
        assert TrustStore([encode_public_key(generate_keypair()[1])])

    def test_the_sizes_are_what_ed25519_uses(self) -> None:
        private, public = generate_keypair()

        assert len(private) == PUBLIC_KEY_BYTES == 32
        assert len(public) == 32
        assert (
            len(base64.b64decode(sign_manifest(MANIFEST, private)["signature"]))
            == SIGNATURE_BYTES
            == 64
        )

    def test_signing_with_a_malformed_private_key_raises(self) -> None:
        with pytest.raises(SigningError, match="private key is 32 bytes"):
            sign_manifest(MANIFEST, b"too short")

    def test_trust_store_from_settings_accepts_a_sequence(
        self, keypair: tuple[bytes, bytes]
    ) -> None:
        store = trust_store_from_settings((encode_public_key(keypair[1]),))

        assert store.key_ids == (key_id(keypair[1]),)

    def test_trust_store_from_settings_accepts_a_mapping(
        self, keypair: tuple[bytes, bytes]
    ) -> None:
        store = trust_store_from_settings({"release-2026": encode_public_key(keypair[1])})

        assert store.key_ids == (key_id(keypair[1]),)


class TestTheSignatureFileLocation:
    def test_it_is_the_manifest_with_a_suffix(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        path = tmp_path / "releases.json"

        # A suffix rather than a sibling directory, so copying the manifest
        # and forgetting the signature is visible in one `ls`.
        assert signature_path_for(path).name == "releases.json.sig"

    def test_a_missing_signature_reads_as_none(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        manifest = tmp_path / "releases.json"
        manifest.write_bytes(MANIFEST)

        assert read_signature(manifest) is None

    def test_a_present_signature_is_read(
        self, tmp_path: object, keypair: tuple[bytes, bytes]
    ) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        manifest = tmp_path / "releases.json"
        manifest.write_bytes(MANIFEST)
        signature_path_for(manifest).write_bytes(sig_bytes(MANIFEST, keypair[0]))

        raw = read_signature(manifest)

        assert raw is not None
        store = TrustStore([encode_public_key(keypair[1])])
        assert store.verify(MANIFEST, raw).status is SignatureStatus.VALID

    def test_an_unreadable_signature_is_an_error_not_absence(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        manifest = tmp_path / "releases.json"
        manifest.write_bytes(MANIFEST)
        # A directory where the signature should be: present, and not readable
        # as a file. That is a deployment fault, not an unsigned release.
        signature_path_for(manifest).mkdir()

        with pytest.raises(SigningError, match="cannot read"):
            read_signature(manifest)


class TestTheControllerCannotSign:
    def test_no_request_handling_code_can_sign_a_release(self) -> None:
        """The boundary that matters, asserted structurally.

        A control plane that could mint release signing keys would be a control
        plane that could sign releases, which is the entire thing this module
        exists to prevent. So nothing that handles a request, serves an agent,
        or implements a service may reach for ``generate_keypair`` or
        ``sign_manifest``.

        Scoped to those three trees rather than to all of ``hcc/``, and the
        exclusion is named rather than implied: ``hcc/cli/contracts.py`` is a
        build-time generator invoked by ``scripts/contracts.sh`` to produce the
        cross-language test vectors. It never runs in the serving process. If
        the signing helpers ever appear under ``api/``, ``services/`` or
        ``agents/``, that is a design change and this test should be the thing
        that notices.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "hcc"
        reachable_from_a_request = ("api", "services", "agents", "security")
        offenders = [
            path.relative_to(root).as_posix()
            for tree in reachable_from_a_request
            for path in (root / tree).rglob("*.py")
            if any(
                name in path.read_text(encoding="utf-8")
                for name in ("generate_keypair", "sign_manifest")
            )
        ]

        assert offenders == []
