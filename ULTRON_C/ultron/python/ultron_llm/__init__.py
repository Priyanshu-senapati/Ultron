"""
ultron_llm — Module C public API.

Usage:
    from ultron_llm import get_service
    svc = get_service()
    response = await svc.ask("explain async queues", mode="default")
    # or for voice:
    response = await svc.ask("what am I working on", mode="voice")
"""
from __future__ import annotations

from typing import Optional

from .config import load_config
from .service import LLMService

_service: Optional[LLMService] = None


def init(config=None) -> LLMService:
    """Initialise the singleton service. Safe to call multiple times — the
    first call wins; later calls return the same instance regardless of any
    `config` they pass."""
    global _service
    if _service is None:
        cfg = config or load_config()
        _service = LLMService(cfg)
    return _service


def get_service() -> LLMService:
    """Return the initialised service. Raises if `init()` was never called."""
    if _service is None:
        raise RuntimeError(
            "ultron_llm not initialized — call init() or run llm_service.py"
        )
    return _service


__all__ = ["init", "get_service", "LLMService"]
