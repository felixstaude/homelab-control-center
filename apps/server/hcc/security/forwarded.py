"""Trusted-proxy handling: client address and forwarded scheme.

Phase 1, milestone 1.5. Framework-free: takes the peer address and the raw
header values, returns decisions. No FastAPI, no ASGI.

## The problem this exists to solve

``X-Forwarded-For`` and ``X-Forwarded-Proto`` are **request headers**, which
means any client can set them. Trusting them unconditionally lets a caller
claim any address it likes, which defeats per-address rate limiting entirely;
ignoring them unconditionally means that behind a reverse proxy every request
appears to come from the proxy, which collapses per-address rate limiting onto
one shared budget and is equally useless.

Neither failure is acceptable, so the headers are trusted **only when the
immediate peer is a configured trusted proxy**. That is a property of the
deployment, which the operator knows and the application cannot guess, so it
is configuration: ``HCC_TRUSTED_PROXIES``.

Default: empty. A direct deployment therefore ignores the headers, and an
operator who puts a proxy in front must say so. Defaulting to "trust
localhost" would be wrong too — on a single-host Compose deployment the proxy
is a sibling container on a bridge network, not loopback, which is exactly the
case that is easy to get silently wrong.

## Walking the chain

``X-Forwarded-For`` is a list, appended to by each hop:

    X-Forwarded-For: <client>, <proxy-1>, <proxy-2>

The **right-hand** entries are the ones added by infrastructure closest to us
and are therefore the trustworthy ones; the left-hand entries were supplied by
whatever came before and may be forged by the client. So the chain is walked
from the right, discarding addresses that are themselves trusted proxies, and
the first untrusted address found is the client. If every entry is a trusted
proxy the right-most is returned, because there is no untrusted hop to
attribute the request to.

Taking the **left-most** entry instead — the common mistake — means a client
sending ``X-Forwarded-For: 1.2.3.4`` is attributed to ``1.2.3.4``, and can
rotate that value per request to bypass any per-address budget.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Canonical header names. Fixed rather than configurable: these are the de
#: facto standard that every reverse proxy writes, and making them
#: configurable would invite trusting a header a proxy does not overwrite.
FORWARDED_FOR_HEADER: Final = "x-forwarded-for"
FORWARDED_PROTO_HEADER: Final = "x-forwarded-proto"

#: Upper bound on how many chain entries are examined. A header is
#: attacker-controlled and unbounded; parsing thousands of addresses per
#: request would be a cheap way to make the server work.
MAX_CHAIN_ENTRIES: Final = 16

#: Returned when no address can be determined. A fixed sentinel rather than an
#: empty string or ``None``, so that such requests share one rate-limit budget
#: instead of each getting a distinct key — or worse, bypassing the limiter.
UNKNOWN_ADDRESS: Final = "unknown"

#: Schemes a proxy may legitimately report.
_VALID_SCHEMES: Final = frozenset({"http", "https"})

TrustedNetworks = tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


def parse_trusted_networks(values: Iterable[str]) -> TrustedNetworks:
    """Parse configured trusted proxies into networks.

    Accepts a bare address (``10.0.0.5``, ``::1``) or CIDR notation
    (``172.16.0.0/12``). A bare address becomes a single-host network, so one
    code path handles both.

    Raises:
        ValueError: naming the offending entry. Invalid proxy configuration is
            a startup failure, not something to skip silently — a typo'd CIDR
            that is quietly dropped would leave the operator believing the
            proxy is trusted when it is not.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in values:
        entry = raw.strip()
        if not entry:
            raise ValueError("trusted proxy entry must not be empty")
        try:
            # strict=False so that 10.0.0.5/24 is accepted as the 10.0.0.0/24
            # network rather than rejected for having host bits set.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"{entry!r} is not a valid IP address or CIDR network: {exc}") from exc
    return tuple(networks)


def _parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse one address, tolerating the forms proxies actually emit.

    Returns ``None`` for anything unparseable rather than raising: the input
    is an attacker-controlled header, and a malformed entry must be ignored,
    not turned into a 500.
    """
    candidate = value.strip()
    if not candidate:
        return None

    # Some proxies bracket IPv6 and append a port: "[::1]:443".
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1:
        # Exactly one colon means IPv4:port. Several colons mean bare IPv6,
        # which must not be split.
        candidate = candidate.split(":", 1)[0]

    # An IPv6 scope id ("fe80::1%eth0") is not part of the address.
    candidate = candidate.partition("%")[0]

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_trusted_proxy(peer: str | None, networks: TrustedNetworks) -> bool:
    """Whether ``peer`` is one of the configured trusted proxies."""
    if not networks or peer is None:
        return False
    address = _parse_address(peer)
    if address is None:
        return False
    return any(address in network for network in networks)


def client_address(
    *,
    peer: str | None,
    forwarded_for: str | None,
    networks: TrustedNetworks,
) -> str:
    """Determine the address to attribute a request to.

    ``peer`` is the transport-level address — the only value that cannot be
    forged. ``forwarded_for`` is the raw ``X-Forwarded-For`` header, consulted
    **only** if ``peer`` is a trusted proxy.

    Returns the canonical string form of the address, or
    :data:`UNKNOWN_ADDRESS` when none can be determined.
    """
    if peer is None:
        return UNKNOWN_ADDRESS

    if not is_trusted_proxy(peer, networks):
        # Direct connection, or a proxy the operator has not declared. Either
        # way the header is not evidence of anything.
        parsed = _parse_address(peer)
        return str(parsed) if parsed is not None else UNKNOWN_ADDRESS

    if not forwarded_for:
        # Trusted proxy that sent no chain: the proxy itself is the client.
        parsed = _parse_address(peer)
        return str(parsed) if parsed is not None else UNKNOWN_ADDRESS

    entries = forwarded_for.split(",")[-MAX_CHAIN_ENTRIES:]
    rightmost_valid: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None

    # Right to left: entries added by infrastructure closest to us first.
    for entry in reversed(entries):
        address = _parse_address(entry)
        if address is None:
            # A malformed hop. Stop rather than skipping past it: beyond a
            # value we cannot interpret, we cannot tell which hop added what.
            break
        if rightmost_valid is None:
            rightmost_valid = address
        if not any(address in network for network in networks):
            return str(address)

    if rightmost_valid is not None:
        # Every hop was itself a trusted proxy, so there is no untrusted
        # address in the chain to attribute the request to.
        return str(rightmost_valid)

    parsed = _parse_address(peer)
    return str(parsed) if parsed is not None else UNKNOWN_ADDRESS


def forwarded_scheme(
    *,
    peer: str | None,
    forwarded_proto: str | None,
    networks: TrustedNetworks,
) -> str | None:
    """Return the scheme a trusted proxy reports, or ``None``.

    ``None`` means "no trustworthy statement was made" — either the peer is
    not a trusted proxy, the header is absent, or its value is not a scheme we
    recognise. The caller then falls back to the transport scheme.

    A proxy chain may send a comma-separated list, in which case the
    **left-most** entry is the scheme the original client used, which is the
    one that matters for deciding what origin a browser will send.
    """
    if not forwarded_proto or not is_trusted_proxy(peer, networks):
        return None

    scheme = forwarded_proto.split(",")[0].strip().lower()
    return scheme if scheme in _VALID_SCHEMES else None


def describe(networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> str:
    """Render the trusted set for a startup log line."""
    return ", ".join(str(network) for network in networks) if networks else "none"
