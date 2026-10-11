"""Argon2id password hashing and verification.

Phase 1, milestone 1.2. Hashing only — there is no login endpoint, no session
and no registration flow. This module is deliberately independent of HTTP: it
imports no FastAPI and knows nothing about requests, so it is equally usable
from a handler, a CLI command or a test.

No cryptography is implemented here. All key derivation is delegated to
``argon2-cffi``, the reference Python binding to the official Argon2 C
implementation.

## Algorithm

**Argon2id**, the hybrid variant, which is what RFC 9106 §4 recommends for
password hashing: Argon2i alone is weaker against time-memory trade-off
attacks, and Argon2d alone leaks timing information through data-dependent
memory access. Argon2id is also the variant OWASP recommends.

## Parameters

Defaults are RFC 9106's **second recommended** ("low memory") profile, which
is also ``argon2-cffi``'s own default:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `memory_cost` | 65536 KiB (64 MiB) | Memory per hash. The dominant cost to an attacker. |
| `time_cost` | 3 | Passes over memory. |
| `parallelism` | 4 | Lanes. |
| `hash_len` | 32 bytes | Output length. |
| `salt_len` | 16 bytes | Random salt per hash. |

Measured on a 20-core desktop: ~50 ms per hash at the defaults, ~115 ms with
``parallelism=1`` (closer to a single-core host). Both are acceptable login
latencies and expensive enough to make offline cracking costly.

**Resource implications.** Memory is charged *per concurrent hash*, not
globally: ten simultaneous logins at 64 MiB each transiently need ~640 MiB.
That is the figure to size against, and it is why the memory cost is
configurable — a Raspberry Pi with 1 GiB of RAM serving a household of three
should tune it down, and an operator with memory to spare can tune it up.

RFC 9106's *first* recommended profile (2 GiB, ``t=1``) is deliberately not
the default. It is the stronger choice on a dedicated machine, but a single
login would exceed the total memory of much of the hardware this product
targets.

**Floors are enforced** (see :class:`Argon2Parameters`): 19456 KiB memory,
2 passes, 1 lane. The lower bounds are OWASP's minimum recommendation, so an
operator can tune parameters down for weak hardware but cannot configure the
hashing into uselessness. This follows the invariants-versus-capabilities rule
in ``docs/PRODUCT_VISION.md``: configuration decides what the system does,
never whether its security properties hold.

## Stored format

The full PHC-style encoded string goes into ``users.password_hash``::

    $argon2id$v=19$m=65536,t=3,p=4$<base64 salt>$<base64 hash>

97 characters at the defaults, 101 at the maximum permitted parameters — both
well inside the column's 255-character limit. Storing the parameters alongside
the digest is what makes :meth:`PasswordHasher.needs_rehash` possible: a hash
produced under old parameters is self-describing, so it can be identified and
upgraded on the user's next successful login without a password reset.

## Password policy

Deliberately permissive. NIST SP 800-63B rejects composition rules because
they push users toward predictable patterns, so there are none: no required
character classes, no forbidden characters, no rotation.

| Rule | Value | Reasoning |
| --- | --- | --- |
| Minimum length | 8 characters | NIST SP 800-63B minimum. |
| Maximum size | 1024 bytes UTF-8 | Denial-of-service bound, not a usability limit. |
| Composition | none | Composition rules reduce real-world entropy. |
| Unicode | fully accepted | Any code point, including emoji and NUL. |
| Normalization | NFC | See below. |
| Truncation | **never** | An over-long password is rejected, never silently shortened. |

### Why a maximum at all

Argon2 has no inherent input limit — unlike bcrypt, which silently ignores
everything past 72 bytes. The cap exists purely because hashing cost grows
with input length, so an unbounded field is a cheap way to make the server do
expensive work. 1024 bytes is far beyond any human password and any password
manager's default generated length.

The limit is on **bytes, not characters**, because bytes are what the
algorithm processes: 1024 emoji are 4096 bytes. A character-based limit would
let a four-times-larger payload through.

**Nothing is ever truncated.** Over-length input raises
:class:`PasswordPolicyError`. Silently shortening a password would mean the
stored hash no longer corresponds to what the user typed, and — worse — that
two different passwords sharing a long prefix become the same credential.

### Unicode normalization: NFC, and why not NFKC

Passwords are normalized to **NFC** before hashing, following the PRECIS
``OpaqueString`` profile (RFC 8265 §4.2), which is the standard profile for
passwords.

This matters because ``"café"`` can be encoded two ways — ``é`` as one code
point (NFC) or ``e`` plus a combining acute accent (NFD) — and they are
different byte sequences, so they produce different hashes. Without
normalization, a user who sets their password on one platform and types it on
another can be locked out by an invisible encoding difference. Verified
experimentally: the NFD form does not verify against an NFC hash.

**NFC, not NFKC** — and this is the opposite choice from usernames, which use
NFKC (see ``hcc/models/user.py``). The distinction is deliberate:

* Usernames want *aggressive* folding. A full-width rendering of ``admin``
  (U+FF41 U+FF44 ...) must collide with the ASCII spelling, because
  allowing both invites impersonation.
* Passwords want *minimal* folding. NFKC maps compatibility variants onto
  canonical ones — full-width to ASCII, ligatures to letter pairs,
  superscript two to a plain ``2`` — which would collapse genuinely
  distinct passwords into one and throw away entropy the user intended to
  have.

NFC fixes only encoding differences that represent *identical* text. It does
not merge characters a user would consider different.

### Whitespace is preserved

Unlike usernames, passwords are **not** stripped. Leading and trailing spaces
are legitimate password characters, and a password manager may well generate
them. Trimming would silently alter the secret — the same class of mistake as
truncation.

## Failure handling

:meth:`PasswordHasher.verify` returns ``bool`` and never raises for a failed
verification, so a caller cannot accidentally treat an exception path as
success.

A subtlety worth recording, because it is an easy way to introduce a 500 on
the login path: ``argon2-cffi``'s ``InvalidHashError`` subclasses
``ValueError``, **not** ``VerificationError``. Catching only
``VerificationError`` leaves malformed stored hashes raising out of the
verifier. Both are handled here.

A malformed stored hash is a data-integrity problem, not a failed login, so it
is logged at warning level — with the exception type only, never the hash
itself — and still returns ``False``. It is reported rather than swallowed,
and it never grants access.

## Secrets never appear in output

No password, and no hash, is written to a log, an exception message, or a
``repr``:

* Exceptions carry field names and limits, never values.
* :class:`PasswordHasher` has no secret state; its ``repr`` shows parameters.
* The only log statement in this module is the malformed-hash warning, which
  records the exception type and nothing else.

## Async use

Hashing is CPU-bound and takes tens of milliseconds, which is long enough to
stall an event loop. The control plane is fully async (ADR-004), so
:meth:`PasswordHasher.hash_async` and :meth:`PasswordHasher.verify_async`
offload to a worker thread. Request handlers must use those; the synchronous
methods are for CLI and test use.
"""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from hcc.logging import get_logger

if TYPE_CHECKING:
    from hcc.config import Settings

logger = get_logger(__name__)

# --- Policy -----------------------------------------------------------------

#: Minimum password length in characters, per NIST SP 800-63B. Measured in
#: characters rather than bytes because that is what a user perceives.
MIN_PASSWORD_LENGTH: Final = 8

#: Maximum password size in UTF-8 bytes. A denial-of-service bound, not a
#: usability limit — see the module docstring.
MAX_PASSWORD_BYTES: Final = 1024

# --- Parameter bounds -------------------------------------------------------
# Lower bounds are OWASP's minimum recommendation. Configuration may tune
# parameters within these bounds; it may not go below them.

MIN_MEMORY_COST_KIB: Final = 19456
MAX_MEMORY_COST_KIB: Final = 4194304
MIN_TIME_COST: Final = 2
MAX_TIME_COST: Final = 20
MIN_PARALLELISM: Final = 1
MAX_PARALLELISM: Final = 16

#: Digest length in bytes. Not configurable: 32 bytes is the RFC 9106
#: recommendation and there is no operational reason to vary it.
HASH_LENGTH: Final = 32

#: Salt length in bytes. Not configurable: 16 bytes is the RFC 9106
#: recommendation, and a shorter salt weakens the per-hash uniqueness that
#: makes precomputation infeasible.
SALT_LENGTH: Final = 16


def _is_text(value: object) -> bool:
    """Whether a value is a string.

    Takes ``object`` so that mypy cannot narrow the call site away. The
    annotated signatures below state the contract, but these functions sit on
    the authentication path and may be reached from untyped sources — a
    nullable database column, decoded JSON — where a non-string is a real
    possibility rather than a type error mypy has already excluded.
    """
    return isinstance(value, str)


class PasswordError(Exception):
    """Base class for password handling failures raised by this module."""


class PasswordPolicyError(PasswordError, ValueError):
    """A candidate password violates the policy.

    Carries every problem rather than the first, so a caller can show a user
    all of them at once — matching the agent's configuration validation and
    the API's field-level validation errors.

    Subclasses ``ValueError`` for consistency with the User model, whose
    validators also raise ``ValueError`` for bad input.

    The message and ``problems`` describe *rules and limits only*. Neither
    ever contains the password.
    """

    def __init__(self, problems: list[str]) -> None:
        if not problems:  # pragma: no cover - defensive
            raise ValueError("PasswordPolicyError requires at least one problem")
        self.problems = list(problems)
        if len(problems) == 1:
            super().__init__(problems[0])
        else:
            super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """Validated Argon2id cost parameters.

    Frozen so that a hasher's parameters cannot change under it after
    construction, and validated in ``__post_init__`` so an out-of-range value
    fails where it was set rather than at first use.
    """

    memory_cost: int = 65536
    time_cost: int = 3
    parallelism: int = 4
    hash_len: int = HASH_LENGTH
    salt_len: int = SALT_LENGTH

    def __post_init__(self) -> None:
        problems: list[str] = []

        if not MIN_MEMORY_COST_KIB <= self.memory_cost <= MAX_MEMORY_COST_KIB:
            problems.append(
                f"memory_cost must be between {MIN_MEMORY_COST_KIB} and "
                f"{MAX_MEMORY_COST_KIB} KiB, got {self.memory_cost}"
            )
        if not MIN_TIME_COST <= self.time_cost <= MAX_TIME_COST:
            problems.append(
                f"time_cost must be between {MIN_TIME_COST} and "
                f"{MAX_TIME_COST}, got {self.time_cost}"
            )
        if not MIN_PARALLELISM <= self.parallelism <= MAX_PARALLELISM:
            problems.append(
                f"parallelism must be between {MIN_PARALLELISM} and "
                f"{MAX_PARALLELISM}, got {self.parallelism}"
            )
        # Argon2 requires at least 8 KiB per lane; below that the C library
        # rejects the parameters at hash time, which would be a confusing
        # place to discover it.
        if self.memory_cost < self.parallelism * 8:
            problems.append(
                f"memory_cost {self.memory_cost} KiB is too small for "
                f"{self.parallelism} lanes (Argon2 requires at least 8 KiB per lane)"
            )
        if self.hash_len < 16:
            problems.append(f"hash_len must be at least 16 bytes, got {self.hash_len}")
        if self.salt_len < 8:
            problems.append(f"salt_len must be at least 8 bytes, got {self.salt_len}")

        if problems:
            raise ValueError("invalid Argon2 parameters: " + "; ".join(problems))

    @property
    def memory_cost_mib(self) -> float:
        """Memory cost in MiB, for display. Charged per concurrent hash."""
        return self.memory_cost / 1024


#: The default profile: RFC 9106 second recommended ("low memory").
DEFAULT_PARAMETERS: Final = Argon2Parameters()


def normalize_password(raw: str) -> str:
    """Return the password in NFC form.

    Only normalization — no stripping, no case folding, no length checks. See
    the module docstring for why NFC and not NFKC, and why whitespace is
    preserved.

    Raises:
        TypeError: if ``raw`` is not a string.
    """
    if not isinstance(raw, str):
        raise TypeError(f"password must be a string, got {type(raw).__name__}")
    return unicodedata.normalize("NFC", raw)


def validate_password(raw: str) -> str:
    """Normalize a password and check it against the policy.

    Returns:
        The NFC-normalized password, ready to hash.

    Raises:
        TypeError: if ``raw`` is not a string.
        PasswordPolicyError: listing every policy violation. The error never
            contains the password.
    """
    candidate = normalize_password(raw)
    problems: list[str] = []

    # Length in characters is what a user counts; size in bytes is what the
    # algorithm processes. Both are checked, against the thing each bounds.
    if len(candidate) < MIN_PASSWORD_LENGTH:
        problems.append(f"password must be at least {MIN_PASSWORD_LENGTH} characters")

    encoded_size = len(candidate.encode("utf-8"))
    if encoded_size > MAX_PASSWORD_BYTES:
        problems.append(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded, "
            f"got {encoded_size}; it is not truncated, so please choose a shorter one"
        )

    if problems:
        raise PasswordPolicyError(problems)
    return candidate


class PasswordHasher:
    """Hashes and verifies passwords with Argon2id.

    Holds no secret state: an instance is just a set of cost parameters, so it
    is safe to share across requests and to ``repr`` in a log line.

    Construct via :func:`build_password_hasher` in application code, so that
    parameters come from validated settings. Construct directly in tests that
    need specific parameters.
    """

    __slots__ = ("_hasher", "_parameters")

    def __init__(self, parameters: Argon2Parameters | None = None) -> None:
        self._parameters = parameters or DEFAULT_PARAMETERS
        self._hasher = _Argon2Hasher(
            time_cost=self._parameters.time_cost,
            memory_cost=self._parameters.memory_cost,
            parallelism=self._parameters.parallelism,
            hash_len=self._parameters.hash_len,
            salt_len=self._parameters.salt_len,
        )

    @property
    def parameters(self) -> Argon2Parameters:
        """The cost parameters new hashes are produced with."""
        return self._parameters

    # --- Hashing ----------------------------------------------------------

    def hash(self, password: str) -> str:
        """Hash a password, returning the PHC-encoded string to store.

        The policy is enforced here, so every stored hash corresponds to a
        password that satisfied it. A fresh random salt is generated per call,
        so hashing the same password twice yields different output.

        Raises:
            TypeError: if ``password`` is not a string.
            PasswordPolicyError: if the password violates the policy.
            PasswordError: if the underlying library fails to hash.
        """
        candidate = validate_password(password)
        try:
            return self._hasher.hash(candidate)
        except HashingError as exc:
            # Wrapped so callers need not import argon2's exceptions, and so
            # the message cannot carry library internals to a client. The
            # cause is preserved for the server-side traceback.
            raise PasswordError("failed to hash password") from exc

    async def hash_async(self, password: str) -> str:
        """Hash a password in a worker thread.

        Use this from request handlers: hashing takes tens of milliseconds,
        which would otherwise block the event loop and stall every concurrent
        request.
        """
        return await asyncio.to_thread(self.hash, password)

    # --- Verification -----------------------------------------------------

    def verify(self, encoded_hash: str, password: str) -> bool:
        """Check a password against a stored hash.

        Returns ``False`` for every kind of failure — wrong password, malformed
        stored hash, wrong algorithm — and never raises for them. A caller
        therefore cannot mistake an error path for success.

        The policy's *minimum length* is deliberately **not** enforced here.
        If the minimum is raised later, existing users must still be able to
        log in; blocking them would turn a policy change into a lockout. The
        byte cap *is* enforced, because that bound exists to limit work the
        server will do for an unauthenticated caller.

        Comparison of the derived key is constant-time inside ``argon2-cffi``.
        """
        if not _is_text(encoded_hash) or not _is_text(password):
            # A non-string here means a programming error upstream, but the
            # safe answer is still "not verified".
            logger.warning(
                "password_verify_bad_argument_type",
                encoded_hash_type=type(encoded_hash).__name__,
                password_type=type(password).__name__,
            )
            return False

        candidate = normalize_password(password)

        if len(candidate.encode("utf-8")) > MAX_PASSWORD_BYTES:
            # Refuse before doing the expensive work, so an over-long password
            # cannot be used to make the server burn CPU.
            logger.info("password_verify_rejected_oversized_input")
            return False

        try:
            self._hasher.verify(encoded_hash, candidate)
        except VerifyMismatchError:
            # The expected negative case: a wrong password. Not logged — on a
            # login endpoint this would be attacker-controlled log volume, and
            # the authentication layer records attempts itself.
            return False
        except (InvalidHashError, VerificationError) as exc:
            # A stored hash that does not parse, or a verification that failed
            # for a reason other than mismatch. This is a data-integrity
            # problem, so it is reported — but never swallowed into a success,
            # and never logged with the hash itself.
            #
            # Both branches are needed: InvalidHashError subclasses ValueError,
            # not VerificationError, so catching one does not catch the other.
            logger.warning(
                "password_verify_invalid_stored_hash",
                error_type=type(exc).__name__,
            )
            return False
        return True

    async def verify_async(self, encoded_hash: str, password: str) -> bool:
        """Verify a password in a worker thread. Use this from handlers."""
        return await asyncio.to_thread(self.verify, encoded_hash, password)

    # --- Parameter upgrades -----------------------------------------------

    def needs_rehash(self, encoded_hash: str) -> bool:
        """Whether a stored hash was produced with outdated parameters.

        Cheap: it reads the parameters from the encoded string and compares
        them, performing no key derivation.

        The intended use, once login exists, is to upgrade transparently::

            if hasher.verify(user.password_hash, password):
                if hasher.needs_rehash(user.password_hash):
                    user.password_hash = hasher.hash(password)
                # ... the plaintext is available only here, so this is the
                # one moment at which an upgrade is possible

        Returns ``True`` for a malformed hash: such a hash can never verify,
        so flagging it for replacement is the useful answer. The caller only
        acts on it after a successful verification, so this cannot be used to
        force a rehash of an unverifiable credential.
        """
        if not _is_text(encoded_hash):
            return True
        try:
            return self._hasher.check_needs_rehash(encoded_hash)
        except (InvalidHashError, VerificationError) as exc:
            logger.warning(
                "password_needs_rehash_invalid_stored_hash",
                error_type=type(exc).__name__,
            )
            return True

    def __repr__(self) -> str:
        """Parameters only. There is no secret state to leak."""
        return (
            f"PasswordHasher(memory_cost={self._parameters.memory_cost}, "
            f"time_cost={self._parameters.time_cost}, "
            f"parallelism={self._parameters.parallelism})"
        )


def build_password_hasher(settings: Settings) -> PasswordHasher:
    """Build a hasher from validated application settings.

    Settings already constrain each value with Pydantic bounds matching the
    floors in :class:`Argon2Parameters`, so this cannot produce a weak hasher.
    The parameters are validated again here — the two checks exist in
    different layers and must not be allowed to drift apart silently.
    """
    return PasswordHasher(
        Argon2Parameters(
            memory_cost=settings.password_hash_memory_cost,
            time_cost=settings.password_hash_time_cost,
            parallelism=settings.password_hash_parallelism,
        )
    )
