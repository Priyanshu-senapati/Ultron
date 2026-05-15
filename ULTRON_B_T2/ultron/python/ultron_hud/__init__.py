"""Module L — HUD aggregator (and optional tray icon).

Subscribes to every read-side service in the stack and emits a single
``hud_status_tick`` every ``tick_seconds``. Other UIs (the existing
hud.py, voice, an eventual Tauri front-end) consume one event instead
of polling each service individually.

If ``pystray`` is installed, the service additionally renders a
Windows tray icon with quick actions. Without pystray the tray is a
no-op — the aggregator still runs.

Public entry::

    from ultron_hud import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .aggregator import HudAggregator
from .config import HudConfig, load_hud_config
from .service import HudService

_service: Optional[HudService] = None


def init(config: Optional[HudConfig] = None) -> HudService:
    global _service
    if _service is None:
        cfg = config or load_hud_config()
        _service = HudService(cfg)
    return _service


def get_service() -> Optional[HudService]:
    return _service


__all__ = [
    "HudAggregator",
    "HudConfig",
    "HudService",
    "get_service",
    "init",
    "load_hud_config",
]
