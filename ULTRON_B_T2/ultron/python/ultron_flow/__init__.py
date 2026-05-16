"""Roadmap #1 — Flow State Protector.

Watches the live signal stream (insight_snapshot, input_metrics_updated)
and detects sustained flow. When flow is active, the rest of the stack
backs off: voice engine queues non-urgent speech, the HUD dims, alerts
hold. When flow breaks, ULTRON logs *why* and how long.

Public entry::

    from ultron_flow import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import FlowConfig, load_flow_config
from .detector import FlowDetector
from .service import FlowService
from .store import FlowStore

_service: Optional[FlowService] = None


def init(config: Optional[FlowConfig] = None) -> FlowService:
    global _service
    if _service is None:
        cfg = config or load_flow_config()
        _service = FlowService(cfg)
    return _service


def get_service() -> Optional[FlowService]:
    return _service


__all__ = [
    "FlowConfig",
    "FlowDetector",
    "FlowService",
    "FlowStore",
    "get_service",
    "init",
    "load_flow_config",
]
