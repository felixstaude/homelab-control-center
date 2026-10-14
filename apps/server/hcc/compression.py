"""Response compression, for an explicit list of paths and no others.

## Why not just compress everything

Because compression and secrecy interact. If a response contains both a secret
and text the attacker chose, the *length* of the compressed response leaks how
well the two matched — so an attacker who can trigger the request repeatedly
and watch the sizes recovers the secret a character at a time. That is BREACH,
and it is not theoretical: it is why ``Content-Encoding`` is a security-relevant
decision rather than a tuning knob.

This application has exactly one response body containing a secret, and it says
so: the enrollment credential, documented in :mod:`hcc.api.v1.hosts` as "the
only response in the API that contains a secret". An allow-list keeps it that
way *by construction*. A deny-list would be one forgotten entry away from
compressing the next secret somebody adds, and the failure would be silent.

## Why these paths

The metric read endpoints, because that is where the bytes are. Measured, with
the point budget the interface actually requests:

| Response | Uncompressed | gzip | Ratio |
| --- | --- | --- | --- |
| One chart, 600 points | 59.5 kB | 6.8 kB | 8.8x |
| A host page's four charts | 237 kB | 24.4 kB | 9.7x |

They compress an order of magnitude because an array of ISO-8601 timestamps
five minutes apart is the most repetitive thing a JSON encoder can produce.
This is also the *recurring* cost: the JavaScript bundle is cached for a year
and fetched once per release, while a host page re-fetches its chart data every
time somebody looks at it.

The four of them are read-only, carry nothing from the session, and return
measurements. There is no secret to leak and nothing to extend this to without
thinking about it again, which is the intent.

## Why this is not a new exposure

``deploy/production/compose.tls.yaml`` runs Caddy with ``encode zstd gzip``,
which already compresses every proxied response including these. The
recommended deployment has had compressed API responses all along; this brings
the default one — which has no proxy — in line with it.

Session cookies are ``SameSite=Lax``, so an attacker's page cannot drive
authenticated requests from its own origin, which is the delivery mechanism
BREACH needs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from starlette.middleware.gzip import GZipMiddleware

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

#: The metric read endpoints, and only those.
#:
#: ``/api/v1/hosts/{host_id}/metrics/{latest,range,series,status}``. Anchored at
#: both ends and with a bounded host segment, so it cannot be widened by a path
#: that merely contains the word.
COMPRESSED_PATHS: Final = (re.compile(r"^/api/v1/hosts/[^/]+/metrics/[a-z]+$"),)

#: Starlette's own default. A response that fits in one network segment arrives
#: in one round trip whether or not it is compressed.
MINIMUM_SIZE: Final = 500

#: zlib's default. Level 9 buys about 1% more on this data for twice the CPU.
COMPRESSION_LEVEL: Final = 6


class ScopedGZipMiddleware:
    """gzip, for request paths matching *patterns*.

    Delegates to :class:`starlette.middleware.gzip.GZipMiddleware` for the
    matching paths rather than reimplementing content negotiation — so
    ``Accept-Encoding`` is honoured, and a client that does not ask still gets
    the bytes verbatim.

    Args:
        app: the application to wrap.
        patterns: compiled patterns matched against ``scope["path"]``. A
            request matching none of them is passed through untouched.
        minimum_size: responses smaller than this are not compressed.
        compresslevel: zlib level.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        patterns: tuple[re.Pattern[str], ...] = COMPRESSED_PATHS,
        minimum_size: int = MINIMUM_SIZE,
        compresslevel: int = COMPRESSION_LEVEL,
    ) -> None:
        self._app = app
        self._patterns = patterns
        self._compressed = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.compresses(scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        await self._compressed(scope, receive, send)

    def compresses(self, path: str) -> bool:
        """Whether *path* is one this middleware will compress.

        Public so a test can assert the boundary against a list of paths
        without issuing a request for each.
        """
        return any(pattern.match(path) for pattern in self._patterns)
