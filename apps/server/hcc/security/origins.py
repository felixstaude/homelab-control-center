"""Web origin parsing and validation.

Phase 1, milestone 1.5. An *origin* is the ``scheme://host[:port]`` triple a
browser sends in the ``Origin`` header. It is the unit the CSRF check compares
against, and the unit CORS is configured in, so both need one strict
definition rather than two approximate ones.

Framework-free: no FastAPI, no ASGI.

## Why this is strict

An origin comparison is only as good as the normalisation on both sides of it.
``https://hcc.example.com/`` and ``https://hcc.example.com`` are the same
origin but different strings; ``https://hcc.example.com/path`` is not an
origin at all. Accepting the loose forms in configuration and then comparing
them literally against the header produces a check that silently never
matches — which is how a CSRF defence becomes a deployment outage, or how a
CORS allow-list quietly fails to include the origin the operator meant.

So configuration is validated and canonicalised **at startup**, and anything
ambiguous is rejected with a message naming the problem.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

#: Schemes an origin may use. ``file:`` and extension schemes serialise as the
#: opaque origin ``"null"``, which is handled separately below.
PERMITTED_SCHEMES: Final = frozenset({"http", "https"})

#: The literal a browser sends when the origin is opaque — a sandboxed iframe,
#: a ``file://`` document, or some redirect chains. It is never a legitimate
#: allow-list entry: treating it as one would permit exactly the contexts an
#: origin check exists to exclude.
OPAQUE_ORIGIN: Final = "null"

#: CORS wildcard. Rejected in configuration because this application sends
#: credentials — see :func:`normalize_configured_origin`.
WILDCARD: Final = "*"


class InvalidOriginError(ValueError):
    """An origin string is not a usable origin.

    Subclasses ``ValueError`` so Pydantic surfaces it as a field validation
    error at startup, consistent with the rest of
    :mod:`hcc.config`.
    """


def normalize_origin(value: str) -> str:
    """Return the canonical ``scheme://host[:port]`` form of an origin.

    Lower-cases the scheme and host (both are case-insensitive), drops a
    default port so that ``https://host:443`` and ``https://host`` compare
    equal, and refuses anything carrying a path, query, fragment or userinfo.

    Raises:
        InvalidOriginError: with a message naming what is wrong.
    """
    candidate = value.strip()
    if not candidate:
        raise InvalidOriginError("origin must not be empty")

    if "://" not in candidate:
        raise InvalidOriginError(
            f"{value!r} is not an origin: it must include a scheme, e.g. https://hcc.example.com"
        )

    parts = urlsplit(candidate)

    scheme = parts.scheme.lower()
    if scheme not in PERMITTED_SCHEMES:
        permitted = ", ".join(sorted(PERMITTED_SCHEMES))
        raise InvalidOriginError(f"{value!r} has scheme {scheme!r}; permitted schemes: {permitted}")

    # A path of "/" is the single tolerated extra: operators habitually paste a
    # trailing slash, and rejecting it would be pedantry. Anything more is a
    # URL, not an origin, and silently discarding it would hide a mistake.
    if parts.path not in ("", "/"):
        raise InvalidOriginError(
            f"{value!r} is not an origin: it must have no path (got {parts.path!r})"
        )
    if parts.query or parts.fragment:
        raise InvalidOriginError(f"{value!r} is not an origin: it must have no query or fragment")
    if parts.username is not None or parts.password is not None:
        raise InvalidOriginError(f"{value!r} must not contain credentials")

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        # urlsplit defers port parsing, so a non-numeric port raises here.
        raise InvalidOriginError(f"{value!r} has an invalid port: {exc}") from exc

    if not hostname:
        raise InvalidOriginError(f"{value!r} is not an origin: it has no host")

    host = hostname.lower()
    # Bracket IPv6 literals, as the Origin header does.
    if ":" in host:
        host = f"[{host}]"

    default_port = 443 if scheme == "https" else 80
    if port is None or port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalize_configured_origin(value: str) -> str:
    """Validate an origin supplied by configuration.

    Identical to :func:`normalize_origin` except that it additionally rejects
    the two values that are meaningful in a header but never in an
    allow-list:

    * ``*`` — this application sends credentials (session cookies), and a
      wildcard combined with credentials makes Starlette echo whichever origin
      asked, so **any** website could read authenticated responses. Rejected
      at startup rather than silently narrowed, because an operator who wrote
      ``*`` wants something the application must not provide. Same-origin
      deployment needs no CORS entry at all.
    * ``null`` — the opaque origin. Allow-listing it would permit sandboxed
      and ``file://`` contexts.
    """
    candidate = value.strip()
    if candidate == WILDCARD:
        raise InvalidOriginError(
            "'*' is not permitted: this API sends session cookies, and a "
            "wildcard origin with credentials enabled would let any website "
            "read authenticated responses. List the exact origins instead, or "
            "leave the list empty for a same-origin deployment."
        )
    if candidate.lower() == OPAQUE_ORIGIN:
        raise InvalidOriginError(
            "'null' is not permitted: it is the opaque origin sent by "
            "sandboxed and file:// contexts, which must not be allow-listed."
        )
    return normalize_origin(candidate)


#: WebSocket schemes map onto the HTTP scheme a browser reports in ``Origin``.
#:
#: A browser opening ``wss://host/path`` sends ``Origin: https://host`` — the
#: origin is a property of the document, not of the socket's URL scheme. The
#: ASGI scope for a WebSocket carries ``ws``/``wss``, so without this
#: translation a handshake's own origin could never be reconstructed, and
#: every session-authenticated socket would be refused.
_SCHEME_EQUIVALENTS: Final = {"ws": "http", "wss": "https"}


def origin_from_parts(scheme: str, netloc: str) -> str | None:
    """Build a canonical origin from a connection's scheme and host.

    Accepts ``ws`` and ``wss`` as well as ``http`` and ``https``, mapping them
    onto the HTTP scheme the browser will have sent — see
    :data:`_SCHEME_EQUIVALENTS`.

    Returns ``None`` if the pair does not form a valid origin, so a caller can
    treat "cannot determine my own origin" as a failure rather than
    constructing a bogus string and comparing against it.
    """
    normalized_scheme = _SCHEME_EQUIVALENTS.get(scheme.lower(), scheme)
    try:
        return normalize_origin(f"{normalized_scheme}://{netloc}")
    except InvalidOriginError:
        return None
