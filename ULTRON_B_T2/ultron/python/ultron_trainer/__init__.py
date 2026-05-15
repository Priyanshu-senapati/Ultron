"""Module TT — Trainer Twin.

Private wellness ledger. Tracks workouts, sleep, and body metrics
(weight, mood, energy) plus daily streaks per habit kind. Read-only
queries reach agents via Module E's ``wellness_query`` tool.

Like Money OS, all data here is LOCAL_ONLY — it never leaves the
machine and is never copied into LLM prompts without an explicit
user prompt that quotes a specific datum.

Public entry::

    from ultron_trainer import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .analytics import TrainerAnalytics
from .config import TrainerConfig, load_trainer_config
from .service import TrainerService
from .store import TrainerStore

_service: Optional[TrainerService] = None


def init(config: Optional[TrainerConfig] = None) -> TrainerService:
    global _service
    if _service is None:
        cfg = config or load_trainer_config()
        _service = TrainerService(cfg)
    return _service


def get_service() -> Optional[TrainerService]:
    return _service


__all__ = [
    "TrainerAnalytics",
    "TrainerConfig",
    "TrainerService",
    "TrainerStore",
    "get_service",
    "init",
    "load_trainer_config",
]
