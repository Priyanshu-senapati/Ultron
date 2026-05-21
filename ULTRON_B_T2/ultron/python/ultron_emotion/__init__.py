"""Roadmap upgrade #76 — Emotional Intelligence layer.

ULTRON now reads voice_transcript through a curated lexicon (cross-
referenced with the live tension EWMA) to estimate the user's
valence / arousal / frustration. The state is EWMA-decayed so a
single frustrated turn doesn't pin ULTRON in supportive mode for
hours. Module C subscribes to ``emotion_state_changed`` and injects a
compact mood block when the signal is significant.

Public entry::

    from ultron_emotion import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import EmotionConfig, load_emotion_config
from .detector import EmotionSignal, analyze
from .lexicon import LEXICON
from .service import EmotionService
from .state import EmotionTracker

_service: Optional[EmotionService] = None


def init(config: Optional[EmotionConfig] = None) -> EmotionService:
    global _service
    if _service is None:
        cfg = config or load_emotion_config()
        _service = EmotionService(cfg)
    return _service


def get_service() -> Optional[EmotionService]:
    return _service


__all__ = [
    "EmotionConfig",
    "EmotionSignal",
    "EmotionService",
    "EmotionTracker",
    "LEXICON",
    "analyze",
    "get_service",
    "init",
    "load_emotion_config",
]
