"""Roadmap #2 — Re-entry Protocol.

Watches presence (via ``input_metrics_updated.idle_secs``); when the
user has been away >= 5 min and returns, ULTRON speaks a ~10-second
context brief: where they were, what was on screen, the last thing
the LLM said, and any git delta during the absence.

Public entry::

    from ultron_reentry import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .composer import compose_brief
from .config import ReentryConfig, load_reentry_config
from .context import ContextSnapshot, ReentryContext
from .detector import PresenceState, PresenceTransition, ReentryDetector
from .service import ReentryService

_service: Optional[ReentryService] = None


def init(config: Optional[ReentryConfig] = None) -> ReentryService:
    global _service
    if _service is None:
        cfg = config or load_reentry_config()
        _service = ReentryService(cfg)
    return _service


def get_service() -> Optional[ReentryService]:
    return _service


__all__ = [
    "ContextSnapshot",
    "PresenceState",
    "PresenceTransition",
    "ReentryConfig",
    "ReentryContext",
    "ReentryDetector",
    "ReentryService",
    "compose_brief",
    "get_service",
    "init",
    "load_reentry_config",
]
