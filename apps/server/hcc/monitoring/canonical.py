"""One deterministic serialisation, and the checksum over it.

Phase 2. Small, and load-bearing: this is what makes "the agent is running
revision 42" a checkable claim rather than a hopeful one.

## The checksum covers transmitted bytes, not a re-serialisation

The obvious design is to send a JSON object, have both sides serialise it
canonically, and compare digests. It is also the design that breaks, because it
makes the checksum depend on **two JSON encoders agreeing** about things they
are not required to agree about: key order, integer versus float rendering
(``1`` or ``1.0``), non-ASCII escaping, and float formatting. Python's
``json`` and Go's ``encoding/json`` differ on several of those, and the failure
mode is a checksum mismatch that looks like tampering.

So the canonical form is produced **once**, by the controller, and travels as a
string. The agent verifies ``sha256(bytes) == checksum`` on exactly the bytes
it received, then parses them. No second encoder is involved, so there is
nothing for two encoders to disagree about. The same bytes are what the
database stores, which is why ``host_config_revisions.document`` is ``text``
and not ``jsonb`` — ``jsonb`` reorders keys and normalises numbers, so a
round trip through it would not reproduce the digest.

Recorded as ADR-050.

## The rules

:func:`canonicalize` produces UTF-8 bytes with:

* **keys sorted** by Unicode code point, so two documents with the same content
  have the same bytes;
* **no insignificant whitespace** — ``,`` and ``:`` separators;
* **non-ASCII characters literal**, not ``\\uXXXX`` escaped, because escaping is
  optional in JSON and therefore not canonical;
* **integers as integers.** A float is refused outright rather than rendered,
  because ``0.1`` has no exact decimal form and two languages' shortest
  round-trip algorithms are not guaranteed to pick the same digits. Every
  duration in this system is integer seconds (ADR-019) and every percentage is
  carried as a scaled integer, so nothing legitimate needs a float here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

#: Prefix on every checksum, so a bare hex string cannot be mistaken for one
#: and a future algorithm change is visible in stored data rather than implied.
CHECKSUM_PREFIX: Final = "sha256:"

#: Length of a rendered checksum: the prefix plus 64 hex characters.
CHECKSUM_LENGTH: Final = len(CHECKSUM_PREFIX) + 64


class NonCanonicalValueError(TypeError):
    """A value that cannot be serialised deterministically.

    A ``TypeError`` because it is a programming error rather than operator
    input — validation happens before anything reaches here, so a float
    arriving means a model declared one.
    """


def canonicalize(document: Any) -> bytes:
    """Serialise ``document`` to its one canonical byte form.

    Raises:
        NonCanonicalValueError: if the document contains a float, a
            non-string mapping key, or any type JSON cannot represent.
    """
    _reject_non_canonical(document)
    text = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def checksum(document: Any) -> str:
    """The canonical checksum of ``document``, as ``sha256:<hex>``."""
    return checksum_of_bytes(canonicalize(document))


def checksum_of_bytes(payload: bytes) -> str:
    """The checksum of bytes that are *already* canonical.

    This is what the agent's verification corresponds to, and what the
    controller uses when it has the stored document text rather than the
    object it came from. Keeping it separate from :func:`checksum` is the point
    of ADR-050: re-deriving a digest from a parsed object would reintroduce the
    second encoder the design exists to avoid.
    """
    return CHECKSUM_PREFIX + hashlib.sha256(payload).hexdigest()


def is_checksum(value: object) -> bool:
    """Whether ``value`` has the shape of a checksum this module produces.

    Shape only — it says nothing about whether the digest is correct. Used to
    refuse a malformed acknowledgement before it is compared, so that a
    mismatch means "different document" rather than "unparseable field".
    """
    if not isinstance(value, str) or len(value) != CHECKSUM_LENGTH:
        return False
    if not value.startswith(CHECKSUM_PREFIX):
        return False
    digest = value[len(CHECKSUM_PREFIX) :]
    return all(character in "0123456789abcdef" for character in digest)


def _reject_non_canonical(value: Any, path: str = "$") -> None:
    """Walk ``value``, raising on anything that cannot be canonical.

    Depth is bounded by the recursion limit rather than explicitly: every
    document reaching here is built by this codebase from validated models, so
    a pathological nesting depth is not reachable from input. The documents
    the *agent* sends are bounded before parsing, in
    :mod:`hcc.agents.protocol`.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise NonCanonicalValueError(
            f"{path}: a float cannot be serialised canonically — two languages' "
            "shortest round-trip formatting need not agree. Carry it as a "
            "scaled integer (ADR-019)."
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError(
                    f"{path}: mapping keys must be strings, got {type(key).__name__}"
                )
            _reject_non_canonical(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_canonical(item, f"{path}[{index}]")
        return
    raise NonCanonicalValueError(f"{path}: {type(value).__name__} is not JSON-representable")
