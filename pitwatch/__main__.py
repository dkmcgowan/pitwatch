"""Container entry point.

Resolve the session key, which also applies migrations, then hand the built
application to uvicorn. Run with `python -m pitwatch`.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from pitwatch.app import configure_logging, create_app
from pitwatch.bootstrap import resolve_secret_key
from pitwatch.config import get_config

log = logging.getLogger(__name__)


def main() -> int:
    config = get_config()
    configure_logging(config)
    try:
        secret_key = asyncio.run(resolve_secret_key(config))
    except Exception as error:  # noqa: BLE001 -- report and exit rather than traceback
        log.error("Could not start: %s", error)
        return 1

    uvicorn.run(
        create_app(config, secret_key=secret_key),
        host=config.host,
        port=config.port,
        log_config=None,
        # Every proxy in front of this is going to be someone's home reverse
        # proxy, so trust the forwarded headers it sets.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
