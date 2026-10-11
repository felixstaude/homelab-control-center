"""Opaque bearer-token generation and storage hashing.

Used for session tokens and CSRF tokens, and intended for the agent enrolment
credentials of a later milestone. Framework-free: no FastAPI, no database.

## Generation

Tokens are 32 bytes from :func:`secrets.token_bytes`, rendered with
URL-safe base64 without padding — 43 ASCII characters, 256 bits of entropy.

256 bits is not arbitrary. A session token is a bearer credential with no
second factor and no rate limit on *possession*, so guessing must be
infeasible rather than merely slow. At 2^256 there is no feasible search, and
the token is short enough to sit in a cookie without trouble.

``secrets`` is used rather than ``random``: the latter is a Mersenne Twister
whose internal state is recoverable from a modest number of outputs, which
would let an attacker predict every future session token.

## Storage hashing: SHA-256, not Argon2

Stored tokens are hashed with **plain SHA-256**, unsalted. That is the correct
choice here, and it is worth stating why, because the project uses Argon2id
for passwords two modules away (see :mod:`hcc.security.password`).

Argon2's cost exists to defeat *guessing* of low-entropy secrets. A human
password has perhaps 30-60 bits of entropy, so an attacker with the hash can
enumerate candidates; making each attempt cost 64 MiB and 50 ms is what makes
that uneconomic.

A session token has 256 bits of entropy from a CSPRNG. There is no candidate
list to enumerate, no dictionary, and no structure to exploit — so there is
nothing for a slow KDF to slow down. Using Argon2 here would instead be
actively harmful: every authenticated request would cost 50 ms of CPU and
64 MiB of RAM, making the session lookup more expensive than the login that
created it, and turning any authenticated endpoint into a denial-of-service
amplifier.

**Unsalted, deliberately.** The hash must be deterministic so a presented
token can be found with a single indexed lookup. A per-row salt would force a
full table scan with one hash per row. Salts defend against precomputation
across a *shared* keyspace; with 256-bit random tokens there is no shared
keyspace to precompute.

What the hashing does buy: a database dump, backup or log leak yields hashes
that cannot be replayed as sessions. That is the whole threat it addresses,
and SHA-256 addresses it completely.

See ADR-027.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

#: Raw entropy per token, in bytes. 32 bytes = 256 bits.
TOKEN_BYTES: Final = 32

#: Length of the encoded form: unpadded URL-safe base64 of 32 bytes.
TOKEN_LENGTH: Final = 43

#: Length of the hex-encoded SHA-256 digest stored in the database.
TOKEN_HASH_LENGTH: Final = 64


def _is_text(value: object) -> bool:
    """Whether a value is a string.

    Takes ``object`` so mypy cannot narrow the call site away. The annotated
    signatures state the contract, but these functions sit on the request path
    and are reached from untyped sources — a cookie, a header — where a
    non-string is a real possibility rather than one the type checker has
    already excluded. Same reasoning as ``hcc.security.password._is_text``.
    """
    return isinstance(value, str)


def generate_token() -> str:
    """Return a new 256-bit token in unpadded URL-safe base64.

    URL-safe base64 keeps the value free of characters that need escaping in a
    cookie, a header or a URL, so it can be moved around without encoding
    bugs. Padding is stripped because it carries no information and would only
    invite inconsistent handling.
    """
    raw = secrets.token_bytes(TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def hash_token(token: str) -> str:
    """Return the hex SHA-256 digest of a token, for storage and lookup.

    Deterministic, so a presented token resolves to its row with one indexed
    lookup. See the module docstring for why SHA-256 and not Argon2.

    Raises:
        TypeError: if ``token`` is not a string.
    """
    if not isinstance(token, str):
        raise TypeError(f"token must be a string, got {type(token).__name__}")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Compare two tokens in constant time.

    Used for the CSRF token, which *is* compared value-to-value rather than
    looked up by hash. :func:`hmac.compare_digest` avoids leaking the length of
    the matching prefix through timing, which a naive ``==`` would.

    Returns ``False`` for any non-string input rather than raising: this sits
    on the request path, where a non-string means a malformed request, not a
    reason to return a 500.
    """
    if not _is_text(left) or not _is_text(right):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def looks_like_token(value: str) -> bool:
    """Cheap shape check for a presented token.

    Lets the request path reject obvious junk — an empty cookie, a truncated
    value, a megabyte of padding — before it reaches the database. It is a
    performance and noise guard, **not** a security check: a value passing this
    is still only authenticated by a successful hash lookup.
    """
    if not _is_text(value) or len(value) != TOKEN_LENGTH:
        return False
    # The encoded alphabet is fixed, so anything outside it cannot be one of
    # our tokens.
    return all(character.isalnum() or character in "-_" for character in value)
