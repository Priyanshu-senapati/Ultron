"""Roadmap #3 — Readiness Score.

Computes a daily 0-100 readiness score from four signals:
  - Sleep (weight 40): hours vs nightly target.
  - Yesterday's flow (weight 30): total flow minutes in trailing 24h.
  - Calm (weight 15): EWMA tension over the recent window.
  - Activity (weight 15): workout in the last 24h.

The score auto-recomputes every 5 minutes and on every sleep/workout
event. Subscribers (HUD, voice, tooling) listen to
``readiness_score_update`` for the full breakdown.

Public entry::

    from ultron_readiness import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .calculator import (
    ReadinessComponent,
    ReadinessScore,
    compute_score,
    score_activity,
    score_calm,
    score_flow_yesterday,
    score_sleep,
)
from .config import ReadinessConfig, load_readiness_config
from .service import ReadinessService
from .state import ReadinessState
from .store import ReadinessStore

_service: Optional[ReadinessService] = None


def init(config: Optional[ReadinessConfig] = None) -> ReadinessService:
    global _service
    if _service is None:
        cfg = config or load_readiness_config()
        _service = ReadinessService(cfg)
    return _service


def get_service() -> Optional[ReadinessService]:
    return _service


__all__ = [
    "ReadinessComponent",
    "ReadinessConfig",
    "ReadinessScore",
    "ReadinessService",
    "ReadinessState",
    "ReadinessStore",
    "compute_score",
    "get_service",
    "init",
    "load_readiness_config",
    "score_activity",
    "score_calm",
    "score_flow_yesterday",
    "score_sleep",
]
