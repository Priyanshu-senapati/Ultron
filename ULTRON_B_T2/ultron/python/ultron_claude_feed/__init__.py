"""Claude Code feed — captures ULTRON failures into a file Claude reads.

Subscribes to error-bearing events on the bus and appends them to a
daily file at ``C:/dev/.ultron-feed/YYYY-MM-DD.md``. Future Claude
Code sessions read that path on demand ("look at the ULTRON feed")
to debug without copy-paste from the user.

The feed is intentionally markdown so it's pleasant to read and easy
for Claude to grep through.

Public entry::

    from ultron_claude_feed import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import ClaudeFeedConfig, load_claude_feed_config
from .service import ClaudeFeedService

_service: Optional[ClaudeFeedService] = None


def init(config: Optional[ClaudeFeedConfig] = None) -> ClaudeFeedService:
    global _service
    if _service is None:
        cfg = config or load_claude_feed_config()
        _service = ClaudeFeedService(cfg)
    return _service


def get_service() -> Optional[ClaudeFeedService]:
    return _service


__all__ = [
    "ClaudeFeedConfig",
    "ClaudeFeedService",
    "get_service",
    "init",
    "load_claude_feed_config",
]
