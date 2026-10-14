"""Emit the cross-language contract documents.

Run by ``scripts/contracts.sh``. Writes one JSON object to stdout containing
every fact the Go agent and the TypeScript client have to agree with the
controller about, so that three hand-maintained copies of one table cannot
drift silently.

## Why a module rather than a function in the script

The script is bash and the registry is Python, so something has to cross that
boundary. Doing it here rather than with an inline heredoc means the
generation is importable, type-checked by mypy, and lintable — and a change to
the registry's shape fails in the same gate as the rest of the codebase
instead of only when somebody runs the script.

## The calendar cases are generated, not hand-written

Each case is ``(schedule, after) → occurrence``, computed by the controller's
own evaluator. Both test suites then assert their implementation reproduces
it. The alternative — a hand-written table — would be a third implementation
of the rule, written by whoever was least sure of it.

Generating them from one side does mean the Go suite is asserting agreement
with Python rather than agreement with the specification. That is the weaker
of the two claims, and it is the one worth having: the specification is prose
(``docs/SCHEDULING.md`` §5), it is tested directly on the Python side against
real transition dates, and what the agent actually needs is to not disagree
with the controller.
"""

from __future__ import annotations

import base64
import itertools
import json
import sys
import zoneinfo
from datetime import datetime
from typing import Any, Final

from hcc.monitoring import modules as registry
from hcc.monitoring.document import DOCUMENT_VERSION
from hcc.monitoring.schedule import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CalendarSchedule,
    next_run,
)
from hcc.updates import signing
from hcc.updates import version as semver

UTC = zoneinfo.ZoneInfo("UTC")

#: The calendar cases both implementations must reproduce.
#:
#: Chosen to cover every branch of the resolution rule rather than to be
#: numerous: an ordinary day, a weekday restriction, a day-of-month and
#: day-of-week conjunction, 29 February, and all four daylight-saving
#: policies against real transition dates in two zones with different
#: transition *sizes* — Berlin's hour and Lord Howe Island's half hour, which
#: is what catches an implementation that shifts by a hardcoded hour.
_CALENDAR_CASES: list[dict[str, Any]] = [
    {
        "name": "every day at 03:00",
        "schedule": {"timezone": "Europe/Berlin", "times": ["03:00"]},
        "after": "2024-06-01T12:00:00+00:00",
    },
    {
        "name": "two times a day, earliest first",
        "schedule": {"timezone": "Europe/Berlin", "times": ["20:00", "08:00"]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "mondays only, 0 is sunday",
        "schedule": {"timezone": "Europe/Berlin", "times": ["03:00"], "days_of_week": [1]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "sunday is zero",
        "schedule": {"timezone": "UTC", "times": ["00:00"], "days_of_week": [0]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "day of month and day of week are ANDed, not ORed",
        "schedule": {
            "timezone": "UTC",
            "times": ["00:00"],
            "days_of_month": [13],
            "days_of_week": [5],
        },
        "after": "2024-01-01T00:00:00+00:00",
    },
    {
        "name": "29 february, four years away",
        "schedule": {
            "timezone": "UTC",
            "times": ["00:00"],
            "months": [2],
            "days_of_month": [29],
        },
        "after": "2024-03-01T00:00:00+00:00",
    },
    {
        "name": "spring forward, shift to the first instant that exists",
        "schedule": {"timezone": "Europe/Berlin", "times": ["02:30"]},
        "after": "2024-03-30T12:00:00+00:00",
    },
    {
        "name": "spring forward, skip",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_nonexistent": "skip",
        },
        "after": "2024-03-30T12:00:00+00:00",
    },
    {
        "name": "spring forward, a 30-minute gap shifts 30 minutes",
        "schedule": {"timezone": "Australia/Lord_Howe", "times": ["02:15"]},
        "after": "2024-10-05T00:00:00+00:00",
    },
    {
        "name": "fall back, first occurrence",
        "schedule": {"timezone": "Europe/Berlin", "times": ["02:30"]},
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, second occurrence",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "second",
        },
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, both occurrences, the first of the two",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "both",
        },
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, both occurrences, the second of the two",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "both",
        },
        "after": "2024-10-27T00:45:00+00:00",
    },
    {
        "name": "a time outside the gap on a transition night is unaffected",
        "schedule": {"timezone": "Europe/Berlin", "times": ["01:30"]},
        "after": "2024-03-30T12:00:00+00:00",
    },
]


def build() -> dict[str, Any]:
    """The whole contract document."""
    return {
        "modules": _modules(),
        "calendar": _calendar(),
        "version_ordering": _version_ordering(),
        "release_signing": _release_signing(),
    }


def _modules() -> dict[str, Any]:
    return {
        "registry_version": registry.REGISTRY_VERSION,
        "document_version": DOCUMENT_VERSION,
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "max_interval_seconds": MAX_INTERVAL_SECONDS,
        "modules": [
            {
                "id": module.id,
                "name": module.name,
                "platform": module.platform.value,
                "schedule_kinds": [kind.value for kind in module.schedule_kinds],
                "min_interval_seconds": module.min_interval_seconds,
                "default_interval_seconds": module.default_interval_seconds,
                "data_schema_version": module.data_schema_version,
                "requires_privileges": module.requires_privileges,
                "allows_duplicate_runs": module.allows_duplicate_runs,
                "metrics": [
                    {
                        "id": metric.id,
                        "name": metric.name,
                        "unit": metric.unit.value,
                        "kind": metric.kind.value,
                        "label_keys": list(metric.label_keys),
                    }
                    for metric in module.metrics
                ],
            }
            for module in registry.MODULE_LIST
        ],
    }


def _calendar() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case in _CALENDAR_CASES:
        schedule = CalendarSchedule.model_validate(case["schedule"])
        after = datetime.fromisoformat(case["after"])
        occurrence = next_run(schedule, after=after)
        cases.append(
            {
                "name": case["name"],
                # The *resolved* schedule, with every default made explicit, so
                # the other implementations are not also asserting agreement
                # about what a default is.
                "schedule": schedule.to_document(),
                "after": after.isoformat(),
                "expected": (
                    None
                    if occurrence is None
                    else {
                        "at": occurrence.at.isoformat(),
                        "local_time": occurrence.local_time,
                        "dst_adjustment": occurrence.dst_adjustment,
                    }
                ),
            }
        )
    return {"cases": cases}


#: Versions whose ordering the agent must agree about.
#:
#: Chosen for the mistakes they catch rather than for coverage. The first group
#: is the lexicographic trap: any implementation comparing strings gets these
#: backwards, and does so only from the tenth release onwards. The second is
#: pre-release precedence, taken from semver 2.0.0 §11 — getting it inverted
#: means a host on the stable channel is offered a beta. The third is build
#: metadata, which must not affect precedence at all, or two builds of one
#: release look like an upgrade from each other.
_VERSION_SAMPLES: Final = (
    # The lexicographic trap.
    "0.1.0",
    "0.2.0",
    "0.9.0",
    "0.10.0",
    "0.11.0",
    "1.0.0",
    "1.99.0",
    "1.100.0",
    "99.0.0",
    "100.0.0",
    "0.1.9",
    "0.1.10",
    # Pre-release precedence, the specification's own example.
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    # A numeric identifier sorts below an alphanumeric one.
    "1.0.0-1",
    "1.0.0-1.alpha",
    # Build metadata: equal precedence to the version without it.
    "0.1.0+abc123",
    "0.1.0+def456",
    "1.0.0-rc.1+build.9",
)

#: Strings the agent must refuse rather than coerce.
#:
#: A parser that accepts "1.2" has to invent a third component, and one that
#: strips a leading "v" has to decide what "version" means. Both are decisions
#: for the build that produced the string.
_VERSION_REJECTIONS: Final = (
    "v1.2.3",
    "1.2",
    "1.2.3.4",
    "latest",
    "",
    "1.2.3-",
    "1.2.3+",
    "01.0.0",
    "1.02.0",
    "1.0.00",
    "1.0.0-alpha..1",
    "1.0.0-01",
    "1.0.0_alpha",
    "1.0.0 extra",
)


def _version_ordering() -> dict[str, Any]:
    """Every pairwise comparison, plus the rejections and the step kinds.

    Pairwise rather than a sorted list: a sorted list only proves the total
    order, while every pair also pins reflexivity and the equal-precedence
    cases that a sort would hide by picking an arbitrary stable order.
    """
    parsed = {raw: semver.parse(raw) for raw in _VERSION_SAMPLES}
    comparisons = [
        {
            "left": left,
            "right": right,
            # -1, 0, +1 rather than a boolean, so the equal-precedence cases
            # are asserted as equal rather than as "not less than".
            "compare": (parsed[left] > parsed[right]) - (parsed[left] < parsed[right]),
        }
        for left, right in itertools.product(_VERSION_SAMPLES, repeat=2)
    ]
    steps = [
        {
            "installed": installed,
            "target": target,
            "step": semver.step_kind(parsed[installed], parsed[target]),
        }
        for installed, target in itertools.product(_VERSION_SAMPLES, repeat=2)
    ]
    return {
        "max_length": semver.MAX_VERSION_LENGTH,
        "versions": [
            {
                "raw": raw,
                "major": parsed[raw].major,
                "minor": parsed[raw].minor,
                "patch": parsed[raw].patch,
                "prerelease": list(parsed[raw].prerelease),
                "build": parsed[raw].build,
                "is_prerelease": parsed[raw].is_prerelease,
            }
            for raw in _VERSION_SAMPLES
        ],
        "comparisons": comparisons,
        "rejections": list(_VERSION_REJECTIONS),
        "steps": steps,
    }


#: The seed for the contract's test signing key.
#:
#: An Ed25519 private key *is* 32 bytes of seed, so a fixed one gives a fixed
#: key, and Ed25519 signatures are deterministic (RFC 8032 §5.1.6) — which is
#: what makes this contract file stable. A random key would produce a new
#: signature on every regeneration, and `contracts.sh --check` would fail on
#: every run.
#:
#: Spelled out in ASCII so that anybody who finds it knows immediately what it
#: is. It signs nothing real: the contract carries only the *public* key, the
#: payload and the signature, so no private key is committed and this value is
#: the only thing that could reconstruct one.
_SIGNING_CONTRACT_SEED: Final = b"hcc-contract-test-key-do-not-use"

#: Payloads whose signatures the agent must agree about.
#:
#: Each is signed with the contract key. The agent replays them and must reach
#: the same verdict — which is what proves the Go verifier accepts what the
#: Python signer produces, rather than both being self-consistently wrong.
_SIGNING_PAYLOADS: Final = (
    b"",
    b"{}",
    b'{"schema_version": 2, "recommended": "0.1.0"}',
    # Non-ASCII and awkward whitespace, because the signature is over exact
    # bytes and any normalisation on either side would show up here.
    '{"notes": "caf\u00e9 \u2014 r\u00e9sum\u00e9"}'.encode(),
    b'{\n  "indented": true\n}\n',
)


def _release_signing() -> dict[str, Any]:
    """Signature test vectors, plus the documents a verifier must refuse.

    Deliberately does not call ``signing.generate_keypair``: that makes a
    random key, which is right for production and wrong here. A random key
    would give a different signature on every run, so the committed contract
    would never match itself and ``contracts.sh --check`` would fail always.
    """
    private = _SIGNING_CONTRACT_SEED
    public = _contract_public_key()

    vectors: list[dict[str, str]] = []
    for payload in _SIGNING_PAYLOADS:
        document = signing.sign_manifest(payload, private)
        vectors.append(
            {
                "payload_base64": base64.b64encode(payload).decode("ascii"),
                "signature_document": json.dumps(document, sort_keys=True),
                "expected_status": signing.SignatureStatus.VALID.value,
            }
        )

    # A valid signature over a *different* payload, which must be INVALID
    # rather than merely "not valid" — the distinction between tampering and a
    # missing key is the one an operator acts on.
    mismatched = signing.sign_manifest(b"a different document", private)
    vectors.append(
        {
            "payload_base64": base64.b64encode(_SIGNING_PAYLOADS[2]).decode("ascii"),
            "signature_document": json.dumps(mismatched, sort_keys=True),
            "expected_status": signing.SignatureStatus.INVALID.value,
        }
    )

    # Signed by a key the store does not hold.
    stranger_seed = b"hcc-contract-stranger-key-nottru"
    stranger = signing.sign_manifest(_SIGNING_PAYLOADS[2], stranger_seed)
    vectors.append(
        {
            "payload_base64": base64.b64encode(_SIGNING_PAYLOADS[2]).decode("ascii"),
            "signature_document": json.dumps(stranger, sort_keys=True),
            "expected_status": signing.SignatureStatus.UNTRUSTED_KEY.value,
        }
    )

    # A relabelled signature: the key_id is a selector, not a credential, so
    # claiming a trusted identifier must not make a foreign signature verify.
    relabelled = dict(stranger)
    relabelled["key_id"] = signing.key_id(public)
    vectors.append(
        {
            "payload_base64": base64.b64encode(_SIGNING_PAYLOADS[2]).decode("ascii"),
            "signature_document": json.dumps(relabelled, sort_keys=True),
            "expected_status": signing.SignatureStatus.INVALID.value,
        }
    )

    # Another algorithm, refused rather than negotiated.
    other_algorithm = dict(signing.sign_manifest(_SIGNING_PAYLOADS[2], private))
    other_algorithm["algorithm"] = "md5-rsa-whatever"
    vectors.append(
        {
            "payload_base64": base64.b64encode(_SIGNING_PAYLOADS[2]).decode("ascii"),
            "signature_document": json.dumps(other_algorithm, sort_keys=True),
            "expected_status": signing.SignatureStatus.UNSUPPORTED_ALGORITHM.value,
        }
    )

    malformed: list[str] = [
        "{not json",
        "[]",
        json.dumps({"key_id": "x", "signature": "AA=="}, sort_keys=True),
        json.dumps({"algorithm": "ed25519", "signature": "AA=="}, sort_keys=True),
        json.dumps({"algorithm": "ed25519", "key_id": "x"}, sort_keys=True),
        json.dumps(
            {"algorithm": "ed25519", "key_id": "x", "signature": "not base64!"},
            sort_keys=True,
        ),
        json.dumps({"algorithm": "ed25519", "key_id": "x", "signature": "AA=="}, sort_keys=True),
    ]
    for malformed_document in malformed:
        vectors.append(
            {
                "payload_base64": base64.b64encode(_SIGNING_PAYLOADS[2]).decode("ascii"),
                "signature_document": malformed_document,
                "expected_status": signing.SignatureStatus.MALFORMED.value,
            }
        )

    return {
        "algorithm": signing.ALGORITHM,
        "key_id_length": signing.KEY_ID_LENGTH,
        "max_signature_bytes": signing.MAX_SIGNATURE_BYTES,
        "trusted_public_key_base64": signing.encode_public_key(public),
        "trusted_key_id": signing.key_id(public),
        "seed_note": (
            "The private key for trusted_public_key_base64 is the ASCII bytes "
            "of 'hcc-contract-test-key-do-not-use'. It is a test vector key and "
            "signs nothing real."
        ),
        "vectors": vectors,
    }


def _contract_public_key() -> bytes:
    """The public half of the deterministic contract key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.from_private_bytes(_SIGNING_CONTRACT_SEED)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def main() -> None:
    """Write the contract document to stdout."""
    json.dump(build(), sys.stdout)


if __name__ == "__main__":  # pragma: no cover - invoked by scripts/contracts.sh
    main()
