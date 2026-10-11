"""Process entrypoint.

Run in development with hot reload:

    make dev-server

Run directly:

    python -m hcc.main

In production the app is served by uvicorn pointed at the factory:

    uvicorn --factory hcc.app:create_app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import uvicorn

from hcc.app import create_app
from hcc.config import load_settings_or_exit


def main() -> None:
    """Start the HTTP server using validated settings."""
    settings = load_settings_or_exit()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Logging is configured by the application itself (hcc.logging), so
        # uvicorn's own dictConfig is disabled to avoid two handler chains.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
