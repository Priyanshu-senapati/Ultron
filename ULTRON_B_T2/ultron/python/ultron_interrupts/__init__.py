"""Roadmap #4 — Interrupt Ledger.

Logs every focus interruption (flow break, voice command, wellness
nudge, etc.), pairs each with a recovery time when the user re-enters
flow, and surfaces rollups via ``interrupt_query_request``.

Public entry::

    from ultron_interrupts import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import InterruptConfig, load_interrupt_config
from .service import InterruptService
from .store import Interrupt, InterruptStore

_service: Optional[InterruptService] = None


def init(config: Optional[InterruptConfig] = None) -> InterruptService:
    global _service
    if _service is None:
        cfg = config or load_interrupt_config()
        _service = InterruptService(cfg)
    return _service


def get_service() -> Optional[InterruptService]:
    return _service


__all__ = [
    "Interrupt",
    "InterruptConfig",
    "InterruptService",
    "InterruptStore",
    "get_service",
    "init",
    "load_interrupt_config",
]
