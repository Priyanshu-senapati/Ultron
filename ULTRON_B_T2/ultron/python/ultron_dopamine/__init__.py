"""Module Y — Dopamine Marker.

Watches activity events (focus_app, visual_label, voice_user_said) and
flags them against user-tunable pattern rules — *rewarding* or
*wasteful*. Maintains a rolling EWMA "dopamine score" and emits a
``dopamine_drift_alert`` when the score crosses the configured floor.

The point of this module isn't gamification — it's awareness. Marks
are stored locally and surfaced via ``dopamine_query`` so the user
(or an agent) can spot patterns later.

Public entry::

    from ultron_dopamine import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import DopamineConfig, load_dopamine_config
from .scorer import DopamineScorer
from .service import DopamineService
from .store import DopamineStore

_service: Optional[DopamineService] = None


def init(config: Optional[DopamineConfig] = None) -> DopamineService:
    global _service
    if _service is None:
        cfg = config or load_dopamine_config()
        _service = DopamineService(cfg)
    return _service


def get_service() -> Optional[DopamineService]:
    return _service


__all__ = [
    "DopamineConfig",
    "DopamineScorer",
    "DopamineService",
    "DopamineStore",
    "get_service",
    "init",
    "load_dopamine_config",
]
