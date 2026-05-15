"""Module S+J — Dream Weaver + Scheduler.

Long-term goals decompose into outcomes; outcomes are pursued via
scheduled time blocks; events surface as alarms/reminders.

The scheduler runs a background tick (default 30 s) and emits
``upcoming_event`` / ``alarm_fire`` events before they happen.

Public entry::

    from ultron_planner import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import PlannerConfig, load_planner_config
from .planner import Planner
from .service import PlannerService
from .store import PlannerStore

_service: Optional[PlannerService] = None


def init(config: Optional[PlannerConfig] = None) -> PlannerService:
    global _service
    if _service is None:
        cfg = config or load_planner_config()
        _service = PlannerService(cfg)
    return _service


def get_service() -> Optional[PlannerService]:
    return _service


__all__ = [
    "Planner",
    "PlannerConfig",
    "PlannerService",
    "PlannerStore",
    "get_service",
    "init",
    "load_planner_config",
]
