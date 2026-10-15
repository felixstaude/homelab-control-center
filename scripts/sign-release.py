#!/usr/bin/env python3
"""Sign a release manifest, and verify that the signature verifies.

The publisher's side of the trust model in :mod:`hcc.updates.signing`. The
controller *verifies* a signature; it must never be able to make one. So this
is a separate tool, run by whoever holds the key, and the controller's own CLI
has no signing subcommand — a control plane that could mint signatures would be
a control plane that could sign releases, which is the whole thing the detached
signature exists to prevent.

## Why it imports the controller's module instead of re-implementing

The signature is over the manifest's **exact bytes**, and the ``.sig`` document
has a specific shape. Two implementations of "write a JSON object with a base64
signature in it" drift — in key order, in whether the base64 is padded, in
whether a trailing newline is inside or outside the signed region — and every
one of those differences is indistinguishable from tampering at the verifier.
There is one implementation, and this calls it.

Usage
-----

Make a keypair. The private key is written with mode 0600 and is the only
secret in this system that matters:

    scripts/sign-release.py keygen --private-key release.key --public-key release.pub

Sign a manifest. Writes ``<manifest>.sig`` beside it:

    scripts/sign-release.py sign --manifest dist/agent-releases.json \\
        --private-key release.key

Verify, which is what a release workflow should do before publishing anything:

    scripts/sign-release.py verify --manifest dist/agent-releases.json \\
        --public-key release.pub

``verify`` exits non-zero on anything other than a valid signature, and prints
*which* of the six outcomes it got. "Signed by a key I do not trust" and
"tampered with" are different problems and this says which one happened.

Safety
------

* The private key is never printed, never logged, and is read from a file
  rather than from an argument — an argument would put it in the process
  table and in the shell history.
* ``sign`` refuses to overwrite an existing signature unless ``--force`` is
  given, because a release that is signed twice with different keys is a
  release nobody can reason about.
* Nothing here publishes, tags, or uploads. It writes two files.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "apps" / "server"))

try:
    from hcc.updates.signing import (
        SignatureStatus,
        SigningError,
        TrustStore,
        encode_public_key,
        generate_keypair,
        key_id,
        sign_manifest,
        signature_path_for,
    )
except ImportError as error:  # pragma: no cover - exercised by hand, not in CI
    print(
        f"cannot import the signing module ({error}).\n"
        "This needs the server's dependencies. Run it with that interpreter:\n"
        "  apps/server/.venv/bin/python scripts/sign-release.py …",
        file=sys.stderr,
    )
    raise SystemExit(2) from error


def _read_private_key(path: Path) -> bytes:
    """Read a raw 32-byte Ed25519 private key, base64 or raw.

    Base64 is accepted because that is what a secret store hands back, and
    raw because that is what ``keygen`` writes when asked. Distinguished by
    length rather than by a flag: 32 raw bytes and 44 base64 characters do not
    collide.
    """
    data = path.read_bytes()
    if len(data) == 32:
        return data
    text = data.decode("ascii", errors="strict").strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as error:  # noqa: BLE001 - any decode failure is the same answer
        raise SigningError(f"{path} is neither 32 raw bytes nor valid base64: {error}") from error
    if len(raw) != 32:
        raise SigningError(f"{path} decodes to {len(raw)} bytes; an Ed25519 private key is 32")
    return raw


def _keygen(arguments: argparse.Namespace) -> int:
    private_raw, public_raw = generate_keypair()

    private_path: Path = arguments.private_key
    if private_path.exists() and not arguments.force:
        print(f"{private_path} already exists; refusing to overwrite a signing key", file=sys.stderr)
        return 1

    # Written with the mode set at creation rather than chmod'ed afterwards:
    # between the two there is a window in which the key is world-readable.
    descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(base64.b64encode(private_raw) + b"\n")

    encoded_public = encode_public_key(public_raw)
    arguments.public_key.write_text(encoded_public + "\n", encoding="utf-8")

    print(f"private key  {private_path} (mode 0600, keep it off this machine if you can)")
    print(f"public key   {arguments.public_key}")
    print(f"key id       {key_id(public_raw)}")
    print()
    print("Configure the controller to trust it:")
    print(f'  HCC_RELEASE_SIGNING_KEYS=["{encoded_public}"]')
    return 0


def _sign(arguments: argparse.Namespace) -> int:
    manifest_path: Path = arguments.manifest
    payload = manifest_path.read_bytes()
    signature_path = signature_path_for(manifest_path)
    existing = _existing_signatures(signature_path) if arguments.add else []
    if signature_path.exists() and not (arguments.force or arguments.add):
        print(
            f"{signature_path} already exists; pass --force to replace it, "
            f"or --add to sign alongside it for a key rotation",
            file=sys.stderr,
        )
        return 1

    private_raw = _read_private_key(arguments.private_key)
    document = sign_manifest(payload, private_raw)

    if any(entry["key_id"] == document["key_id"] for entry in existing):
        print(
            f"{signature_path} already carries a signature by key "
            f"{document['key_id']}; nothing to add",
            file=sys.stderr,
        )
        return 1

    # One signature writes the single-document shape and several write the
    # rotation shape. Not always an array: a release signed by one key is the
    # normal case, and wrapping it would churn every signature that exists for
    # nothing.
    entries = [*existing, document]
    written: dict[str, object] = entries[0] if len(entries) == 1 else {"signatures": entries}

    # The signature covers `payload`, so the signature file's own formatting is
    # free — but it is written deterministically anyway, so that re-signing
    # identical bytes produces an identical file and a diff means something.
    signature_path.write_text(json.dumps(written, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"signed {manifest_path} ({len(payload)} bytes)")
    print(f"  key id      {document['key_id']}")
    if existing:
        print(f"  alongside   {', '.join(entry['key_id'] for entry in existing)}")
    print(f"  signature   {signature_path}")

    # Verified immediately, against the key that was just used, **and**
    # against each key already in the file. A tool that wrote a signature it
    # had not checked would be the one place in this system where "it is
    # signed" is an untested claim — and during a rotation the claim that
    # matters most is that the *old* signature still verifies, since that is
    # what the existing fleet will use.
    written_bytes = signature_path.read_bytes()
    for entry in entries:
        store = TrustStore((encode_public_key(_public_of(private_raw)),)) if entry is document else None
        if store is None:
            # An existing entry: verified by checking the whole document
            # against a store holding only that entry's key is not possible
            # without the public key, which is not in the file. What *can* be
            # checked is that the entry survived the rewrite byte for byte.
            if entry not in json.loads(written_bytes).get("signatures", []):
                print(f"  the existing signature by {entry['key_id']} was lost", file=sys.stderr)
                return 1
            print(f"  preserved   {entry['key_id']}")
            continue
        verification = store.verify(payload, written_bytes)
        if verification.status is not SignatureStatus.VALID:
            print(
                f"  the signature just written does not verify: {verification.detail}",
                file=sys.stderr,
            )
            return 1
        print("  verified    against the key that signed it")
    return 0


def _existing_signatures(signature_path: Path) -> list[dict[str, str]]:
    """Read whatever signatures a ``.sig`` already holds, in either shape."""
    if not signature_path.exists():
        return []
    try:
        parsed = json.loads(signature_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{signature_path} is not readable as JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SystemExit(f"{signature_path} is not a JSON object")
    if "signatures" in parsed:
        entries = parsed["signatures"]
        if not isinstance(entries, list):
            raise SystemExit(f"{signature_path} has a non-array 'signatures'")
        return list(entries)
    return [parsed]


def _public_of(private_raw: bytes) -> bytes:
    """The public key for a raw private key, without exposing a second API."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.from_private_bytes(private_raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _verify(arguments: argparse.Namespace) -> int:
    manifest_path: Path = arguments.manifest
    payload = manifest_path.read_bytes()
    signature_path = signature_path_for(manifest_path)
    if not signature_path.exists():
        print(f"no signature at {signature_path}", file=sys.stderr)
        return 1

    keys = [line.strip() for line in arguments.public_key.read_text(encoding="utf-8").splitlines()]
    store = TrustStore(tuple(key for key in keys if key))
    verification = store.verify(payload, signature_path.read_bytes())

    print(f"manifest   {manifest_path} ({len(payload)} bytes)")
    print(f"status     {verification.status.value}")
    if verification.key_id:
        print(f"key id     {verification.key_id}")
    if verification.detail:
        print(f"detail     {verification.detail}")

    if verification.status is SignatureStatus.VALID:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sign-release.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    keygen = subparsers.add_parser("keygen", help="make a new release signing keypair")
    keygen.add_argument("--private-key", required=True, type=Path)
    keygen.add_argument("--public-key", required=True, type=Path)
    keygen.add_argument("--force", action="store_true", help="overwrite an existing private key")
    keygen.set_defaults(handler=_keygen)

    sign = subparsers.add_parser("sign", help="sign a manifest's exact bytes")
    sign.add_argument("--manifest", required=True, type=Path)
    sign.add_argument("--private-key", required=True, type=Path)
    sign.add_argument("--force", action="store_true", help="replace an existing signature")
    sign.add_argument(
        "--add",
        action="store_true",
        help=(
            "sign alongside the signatures already present, for a key rotation. "
            "A verifier accepts if any one of them is by a key it trusts, which "
            "is what lets agents built before the rotation keep updating."
        ),
    )
    sign.set_defaults(handler=_sign)

    verify = subparsers.add_parser("verify", help="verify a manifest against a public key")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument(
        "--public-key",
        required=True,
        type=Path,
        help="a file of base64 public keys, one per line",
    )
    verify.set_defaults(handler=_verify)

    arguments = parser.parse_args(argv)
    try:
        result: int = arguments.handler(arguments)
    except (SigningError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
