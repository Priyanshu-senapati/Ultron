"""
llm_service.py — Module C entry point.

Run:
    python python/llm_service.py

or with debug logging:
    ULTRON_LOG_LEVEL=DEBUG python python/llm_service.py
"""
import asyncio
import logging
import os
import sys

from ultron_llm import init
from ultron_llm.config import load_config

logging.basicConfig(
    level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ultron.llm_service")


async def _run() -> None:
    cfg = load_config()
    if not cfg.token:
        sys.exit(
            "bridge.token not set in config.toml and ULTRON_TOKEN env var not set"
        )
    svc = init(cfg)
    await svc.run()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
