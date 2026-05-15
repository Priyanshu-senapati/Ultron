"""code_service.py — Module G entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from ultron_code import init


logging.basicConfig(
    level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ultron.code_service")


async def _main() -> None:
    svc = init()
    try:
        await svc.run()
    except Exception as exc:  # noqa: BLE001
        logger.exception("code service crashed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
