"""privacy_service.py — Module N entry point.

Run:
    python python/privacy_service.py

Loads [privacy] from %APPDATA%/ULTRON/config.toml, initialises the
singleton PrivacyService, and runs the WS subscriber.

C and Q import the singleton via `ultron_privacy.get_service()`. If
N isn't running, get_service() returns None and callers should treat
that as "don't send anything outbound".
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from ultron_privacy import init


logging.basicConfig(
    level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ultron.privacy_service")


async def _main() -> None:
    svc = init()
    try:
        await svc.run()
    except Exception as exc:  # noqa: BLE001
        logger.exception("privacy service crashed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
