"""Release metadata authenticity: Ed25519 over the manifest's exact bytes.

## The problem this solves

Before this, the controller vouched for the digest. That is defensible for a
human-initiated upgrade — an administrator reads a digest inside an
authenticated TLS session and types it — and it is **not** sufficient for an
automatic one, because it means a compromised control plane can install
arbitrary code as root on every monitored machine.

"The control plane is compromised" is precisely the scenario a fleet update
mechanism must not amplify. So the controller is demoted: it decides *whether*
and *when* an update happens; it does not get to decide *what the bytes are*.

The publisher signs the release manifest. The agent carries the trusted public
key, verifies the signature, and takes the digest from the *signed* document.
A controller that has been taken over can still refuse to offer updates, or
offer an old one — availability and version choice are its job — but it cannot
produce a manifest the agent will accept for a binary the publisher did not
build.

## Why a detached signature over the exact bytes

Not over a re-serialisation, and not over a canonical form computed from the
parsed model. Two JSON libraries do not agree on key order, integer rendering
or non-ASCII escaping, so a signature over "the document as I would write it"
fails whenever the writer and the reader differ in any of those — and the
failure is a signature mismatch, which is indistinguishable from tampering.

This project has already learned that lesson once: the configuration snapshot
carries its document as an opaque string for the same reason, and the comment
there says so. The signature covers `releases.json` byte for byte, and the
verifier is handed those bytes.

## Why Ed25519 and not Sigstore

Both, actually — they answer different questions.

`release-prepare.yml` already produces a build-provenance attestation, which
binds an artifact to the workflow that built it. That is the right thing for a
human auditing a download, and the wrong thing for a 7 MB Go binary on a NAS:
verifying it needs an OIDC-aware verifier, a transparency-log lookup and a
trust policy written in certificate identities.

Ed25519 over a detached signature needs 32 bytes of public key and one
standard-library call on the agent side. Attestation for people, a signature
for machines.

## Key rotation

The signature names a key by a short identifier derived from the key itself, so
a verifier holding several trusted keys can find the right one, and a key can
be retired by removing it from the trust store. The identifier is *not* a
security boundary — it selects a key, it does not authorise one. An unknown
identifier is reported as untrusted rather than as a bad signature, because
those call for different actions.

Several trusted keys on the *verifier* side is not enough on its own, and the
gap is a chicken-and-egg one worth naming. An agent built before a rotation
trusts only the old key. If the next manifest is signed with the new key, that
agent refuses it — including the update that would have given it a binary
trusting the new key. The fleet that most needs the rotation is the one it
locks out.

So a ``.sig`` may carry **several signatures over the same bytes**, and a
verifier accepts if any one of them is made by a key it trusts. During an
overlap the publisher signs with both keys: old agents verify with the old
one, new agents with the new one, and nobody is stranded. When the fleet has
rolled over, the old signature — and then the old key — is dropped. See
``docs/UPDATES.md``.

The precedence when nothing verifies is deliberate: a *trusted* key whose
signature fails outranks an untrusted one in the report, because the first is
an attack signal and the second is a routine "not for me". Masking the former
behind the latter would be the quiet way to lose the only alarm this has.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

#: The only algorithm accepted.
#:
#: A single value rather than a negotiated set. Algorithm agility in a
#: signature format is how downgrade attacks get a foothold: a verifier that
#: accepts "whatever the document says" accepts the weakest thing it knows.
#: Adding a second algorithm later means a new field value and a verifier that
#: accepts exactly two, decided by the verifier rather than by the document.
ALGORITHM: Final = "ed25519"

#: Raw Ed25519 key and signature sizes, in bytes. Checked rather than assumed:
#: handing a 31-byte key to the library is an exception a long way from the
#: cause, and a clear refusal is more useful than a traceback.
PUBLIC_KEY_BYTES: Final = 32
SIGNATURE_BYTES: Final = 64

#: How many hex characters of the key digest identify a key.
#:
#: Eight bytes. Long enough that two keys in one trust store will not collide
#: by accident, short enough to read out loud. It is a selector, not a
#: credential.
KEY_ID_LENGTH: Final = 16

#: Largest signature file that will be read. The document is a few hundred
#: bytes; the bound stops a misconfigured path from being the process's memory.
MAX_SIGNATURE_BYTES: Final = 8 * 1024

#: How many signatures one document may carry.
#:
#: A rotation overlap needs two. Four would be an unusual mess and eight is
#: past the point of asking what is going on, so the bound is there to make an
#: absurd document a refusal rather than work — each entry costs a signature
#: verification, and a verifier that will do unbounded work on an unverified
#: input has a denial of service in it.
MAX_SIGNATURES: Final = 8


class SignatureStatus(enum.Enum):
    """The outcome of verifying a release manifest.

    Six outcomes rather than a boolean, because they call for different
    actions and an interface has to be able to say which happened. "Not valid"
    collapses "nobody signed this" with "somebody signed this badly", and those
    are a missing release step and an attack respectively.
    """

    #: Signed by a trusted key, and the signature verifies.
    VALID = "valid"
    #: No signature accompanies the manifest.
    ABSENT = "absent"
    #: Well-formed and signed by a key this verifier does not trust.
    UNTRUSTED_KEY = "untrusted_key"
    #: Signed by a trusted key, and the signature does not verify. Either the
    #: document changed after signing, or somebody tried to make it look signed.
    INVALID = "invalid"
    #: The signature file is not readable as one.
    MALFORMED = "malformed"
    #: The signature names an algorithm this verifier does not implement.
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"

    @property
    def is_authentic(self) -> bool:
        """Whether this outcome establishes publisher authenticity.

        Exactly one does. Written as a property on the enum rather than as a
        comparison at each call site so that there is one place where "is this
        trusted" is decided, and so that adding a status cannot accidentally
        land on the trusted side of an ``!= INVALID`` test.
        """
        return self is SignatureStatus.VALID


@dataclass(frozen=True, slots=True)
class SignatureVerification:
    """What verification found, and enough detail to act on it."""

    status: SignatureStatus
    #: The key the signature named, when it was well-formed enough to say.
    key_id: str | None = None
    #: A sentence for an operator. Never contains key material.
    detail: str | None = None

    @property
    def is_authentic(self) -> bool:
        return self.status.is_authentic


class SigningError(Exception):
    """A key or signature that cannot be used for a structural reason."""


def key_id(public_key: bytes) -> str:
    """The short identifier for a raw Ed25519 public key.

    Derived from the key rather than assigned, so two parties naming the same
    key agree without coordinating, and a key cannot be given a misleading
    name.
    """
    if len(public_key) != PUBLIC_KEY_BYTES:
        raise SigningError(
            f"an Ed25519 public key is {PUBLIC_KEY_BYTES} bytes, got {len(public_key)}"
        )
    return hashlib.sha256(public_key).hexdigest()[:KEY_ID_LENGTH]


def decode_public_key(encoded: str) -> bytes:
    """Decode a base64 raw Ed25519 public key.

    Raw rather than PEM or SSH format. The agent has to carry this value
    stamped into its binary by ``-ldflags -X``, which means it has to survive
    being a single-line string with no newlines and no shell-significant
    characters — and base64 of 32 bytes is 44 characters of exactly that.
    """
    text = encoded.strip()
    if not text:
        raise SigningError("the public key is empty")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SigningError(f"the public key is not valid base64: {error}") from error
    if len(raw) != PUBLIC_KEY_BYTES:
        raise SigningError(
            f"the public key decodes to {len(raw)} bytes; an Ed25519 key is {PUBLIC_KEY_BYTES}"
        )
    # Constructed here so a structurally invalid key is refused at
    # configuration time rather than at the first verification.
    Ed25519PublicKey.from_public_bytes(raw)
    return raw


def encode_public_key(raw: bytes) -> str:
    """Encode a raw Ed25519 public key as base64."""
    if len(raw) != PUBLIC_KEY_BYTES:
        raise SigningError(f"an Ed25519 public key is {PUBLIC_KEY_BYTES} bytes, got {len(raw)}")
    return base64.b64encode(raw).decode("ascii")


class TrustStore:
    """The public keys this verifier will accept, keyed by identifier.

    Explicit and finite. There is no mode in which an unknown key is accepted,
    and no "trust on first use" — an update mechanism that learned keys from
    the thing it is verifying would be verifying nothing.
    """

    __slots__ = ("_keys",)

    def __init__(self, public_keys: list[str] | tuple[str, ...] = ()) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        for encoded in public_keys:
            raw = decode_public_key(encoded)
            self._keys[key_id(raw)] = Ed25519PublicKey.from_public_bytes(raw)

    def __len__(self) -> int:
        return len(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    @property
    def key_ids(self) -> tuple[str, ...]:
        """The identifiers of the trusted keys, sorted for stable display."""
        return tuple(sorted(self._keys))

    def get(self, identifier: str) -> Ed25519PublicKey | None:
        return self._keys.get(identifier)

    def verify(self, payload: bytes, signature_document: bytes) -> SignatureVerification:
        """Verify *signature_document* over *payload*.

        *payload* must be the bytes that were signed, not a re-serialisation of
        them. See the module docstring.

        A document carrying several signatures verifies if **any one** of them
        was made by a trusted key — that is the rotation overlap. The outcome
        reported when none verifies is the most alarming one present, so a
        tampered signature by a trusted key is never hidden behind an
        untrusted one that happened to be listed after it.
        """
        try:
            candidates = _parse_signature_document(signature_document)
        except SigningError as error:
            return SignatureVerification(status=SignatureStatus.MALFORMED, detail=str(error))

        outcomes = [self._verify_one(payload, candidate) for candidate in candidates]
        for outcome in outcomes:
            if outcome.is_authentic:
                return outcome
        # Ordered worst-first: INVALID means a key we trust signed something
        # that is not this, which is the one outcome worth waking up for.
        for status in (
            SignatureStatus.INVALID,
            SignatureStatus.UNSUPPORTED_ALGORITHM,
            SignatureStatus.UNTRUSTED_KEY,
            SignatureStatus.MALFORMED,
        ):
            for outcome in outcomes:
                if outcome.status is status:
                    return outcome
        # Unreachable: _parse_signature_document refuses an empty set.
        return SignatureVerification(
            status=SignatureStatus.MALFORMED, detail="the signature document carries no signature"
        )

    def _verify_one(self, payload: bytes, parsed: _ParsedSignature) -> SignatureVerification:
        if parsed.algorithm != ALGORITHM:
            return SignatureVerification(
                status=SignatureStatus.UNSUPPORTED_ALGORITHM,
                key_id=parsed.key_id,
                detail=(
                    f"the signature names algorithm {parsed.algorithm!r}; this "
                    f"verifier implements {ALGORITHM} only"
                ),
            )

        key = self.get(parsed.key_id)
        if key is None:
            known = ", ".join(self.key_ids) if self._keys else "none configured"
            return SignatureVerification(
                status=SignatureStatus.UNTRUSTED_KEY,
                key_id=parsed.key_id,
                detail=(
                    f"the signature was made by key {parsed.key_id}, which is not "
                    f"trusted here. Trusted keys: {known}."
                ),
            )

        try:
            key.verify(parsed.signature, payload)
        except InvalidSignature:
            return SignatureVerification(
                status=SignatureStatus.INVALID,
                key_id=parsed.key_id,
                detail=(
                    "the signature does not verify. Either the manifest changed "
                    "after it was signed, or it was not signed by the key it names."
                ),
            )
        return SignatureVerification(status=SignatureStatus.VALID, key_id=parsed.key_id)


@dataclass(frozen=True, slots=True)
class _ParsedSignature:
    algorithm: str
    key_id: str
    signature: bytes


def _parse_signature_document(document: bytes) -> tuple[_ParsedSignature, ...]:
    """Read a ``.sig`` document, which holds one signature or several.

    One:

    ```json
    {"algorithm": "ed25519", "key_id": "0123456789abcdef", "signature": "<base64>"}
    ```

    Several, for a rotation overlap:

    ```json
    {"signatures": [{...}, {...}]}
    ```

    Both shapes are read, and the single one is **not** deprecated: a release
    signed by one key is the normal case and wrapping it in an array would
    churn every existing signature for nothing. A document with both a
    top-level ``signature`` and a ``signatures`` array is refused rather than
    reconciled — two statements about what signed this is not a thing to guess
    at on a privilege boundary.
    """
    if len(document) > MAX_SIGNATURE_BYTES:
        raise SigningError(
            f"the signature file is {len(document)} bytes, over the "
            f"{MAX_SIGNATURE_BYTES} byte limit"
        )
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError as error:
        raise SigningError(f"not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SigningError("not a JSON object")

    entries = parsed.get("signatures")
    if entries is None:
        return (_parse_signature(parsed),)
    if "signature" in parsed or "key_id" in parsed or "algorithm" in parsed:
        raise SigningError(
            "the document has both a 'signatures' array and top-level signature "
            "fields; it must have one or the other"
        )
    if not isinstance(entries, list):
        raise SigningError("'signatures' is not an array")
    if not entries:
        raise SigningError("'signatures' is empty")
    if len(entries) > MAX_SIGNATURES:
        raise SigningError(
            f"'signatures' has {len(entries)} entries, over the {MAX_SIGNATURES} limit"
        )
    return tuple(_parse_signature(entry) for entry in entries)


def _parse_signature(parsed: object) -> _ParsedSignature:
    """Read one signature object out of an already-decoded document."""
    if not isinstance(parsed, dict):
        raise SigningError("a signature entry is not a JSON object")

    algorithm = parsed.get("algorithm")
    identifier = parsed.get("key_id")
    encoded = parsed.get("signature")
    if not isinstance(algorithm, str) or not algorithm:
        raise SigningError("missing or non-string 'algorithm'")
    if not isinstance(identifier, str) or not identifier:
        raise SigningError("missing or non-string 'key_id'")
    if not isinstance(encoded, str) or not encoded:
        raise SigningError("missing or non-string 'signature'")

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SigningError(f"'signature' is not valid base64: {error}") from error
    if len(raw) != SIGNATURE_BYTES:
        raise SigningError(
            f"'signature' decodes to {len(raw)} bytes; an Ed25519 signature is {SIGNATURE_BYTES}"
        )
    return _ParsedSignature(algorithm=algorithm, key_id=identifier, signature=raw)


def verify_manifest(
    payload: bytes,
    signature_document: bytes | None,
    trust_store: TrustStore,
) -> SignatureVerification:
    """Verify a manifest, reporting absence distinctly from failure.

    ``signature_document`` is ``None`` when no ``.sig`` file accompanies the
    manifest, which is the state of this repository today: nothing has been
    published and nothing has been signed. That reports ``ABSENT``, and the
    caller decides whether that is acceptable — it is, for an install command a
    human runs; it is not, for an automatic update.
    """
    if signature_document is None:
        return SignatureVerification(
            status=SignatureStatus.ABSENT,
            detail=(
                "no signature accompanies the release manifest, so its "
                "authenticity cannot be established. Automatic updates require "
                "one; an operator-driven install does not."
            ),
        )
    return trust_store.verify(payload, signature_document)


def signature_path_for(manifest_path: Path) -> Path:
    """Where the detached signature for *manifest_path* lives.

    ``releases.json`` → ``releases.json.sig``. A suffix rather than a sibling
    directory, so copying the manifest somewhere and forgetting the signature
    is visible in one ``ls``.
    """
    return manifest_path.with_name(manifest_path.name + ".sig")


def read_signature(manifest_path: Path) -> bytes | None:
    """Read the detached signature beside *manifest_path*, or ``None``.

    A missing file is absence, not an error: see :func:`verify_manifest`. An
    unreadable one *is* an error, because a signature that exists and cannot be
    read is a deployment fault rather than an unsigned release.
    """
    path = signature_path_for(manifest_path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SigningError(f"cannot read {path}: {error}") from error


# ---------------------------------------------------------------------------
# Signing. Used by the release tooling and by tests, never by the controller.
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[bytes, bytes]:
    """A new Ed25519 keypair as ``(private_raw, public_raw)``.

    Here rather than in a script so the tests can make keys without shelling
    out. The controller never calls it: a control plane that could mint release
    signing keys would be a control plane that could sign releases, which is
    the entire thing this module exists to prevent.
    """
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ),
    )


def sign_manifest(payload: bytes, private_key_raw: bytes) -> dict[str, str]:
    """Sign *payload*, returning the document to write as ``.sig``."""
    if len(private_key_raw) != PUBLIC_KEY_BYTES:
        raise SigningError(
            f"an Ed25519 private key is {PUBLIC_KEY_BYTES} bytes, got {len(private_key_raw)}"
        )
    private = Ed25519PrivateKey.from_private_bytes(private_key_raw)
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {
        "algorithm": ALGORITHM,
        "key_id": key_id(public_raw),
        "signature": base64.b64encode(private.sign(payload)).decode("ascii"),
    }


def trust_store_from_settings(encoded_keys: tuple[str, ...] | Mapping[str, str]) -> TrustStore:
    """Build a trust store from configuration.

    Accepts a sequence of base64 keys. A mapping is accepted too and its
    *values* are used, so that a future configuration shape naming keys does
    not need a second constructor.
    """
    if isinstance(encoded_keys, dict):
        return TrustStore(tuple(encoded_keys.values()))
    return TrustStore(tuple(encoded_keys))
