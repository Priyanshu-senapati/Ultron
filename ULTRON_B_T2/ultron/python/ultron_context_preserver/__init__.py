"""Roadmap #5 — Context Preserver.

Listens to the bus and persists a Markdown + JSON packet so the next
session (after `bye ultron`, a crash, or a Windows reboot) can read
``context_packet.md`` and know exactly where work left off:

  - last focus app + vision label
  - last user turn + last ULTRON reply
  - current flow state + last completed flow session
  - latest readiness score + components
  - today's interrupt count + top source + avg recovery
  - recent commits
  - latest Claude Code session snippet

Triggers a write on:
  - ``voice_shutdown_initiated`` (the "bye ultron" path)
  - every ``heartbeat_interval_secs`` while running
  - ``context_packet_request`` (manual)

Public entry::

    from ultron_context_preserver import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import ContextPreserverConfig, load_context_preserver_config
from .markdown import render_packet
from .service import ContextPreserverService
from .snapshot import ContextSnapshot

_service: Optional[ContextPreserverService] = None


def init(config: Optional[ContextPreserverConfig] = None) -> ContextPreserverService:
    global _service
    if _service is None:
        cfg = config or load_context_preserver_config()
        _service = ContextPreserverService(cfg)
    return _service


def get_service() -> Optional[ContextPreserverService]:
    return _service


__all__ = [
    "ContextPreserverConfig",
    "ContextPreserverService",
    "ContextSnapshot",
    "get_service",
    "init",
    "load_context_preserver_config",
    "render_packet",
]
