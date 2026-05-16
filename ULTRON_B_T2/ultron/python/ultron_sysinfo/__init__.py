"""System Info bridge — publishes a periodic ``system_info`` event.

Battery percent + AC state via psutil. Wifi / Bluetooth via PowerShell
queries against the netsh / Get-PnpDevice surfaces. Local time included
so HUD doesn't have to recompute it on every render.

This is a tiny self-contained service — no DB, no analytics, just a
heartbeat the HUD subscribes to.
"""
from __future__ import annotations

from typing import Optional

from .config import SysInfoConfig, load_sysinfo_config
from .service import SysInfoService

_service: Optional[SysInfoService] = None


def init(config: Optional[SysInfoConfig] = None) -> SysInfoService:
    global _service
    if _service is None:
        cfg = config or load_sysinfo_config()
        _service = SysInfoService(cfg)
    return _service


def get_service() -> Optional[SysInfoService]:
    return _service


__all__ = [
    "SysInfoConfig",
    "SysInfoService",
    "get_service",
    "init",
    "load_sysinfo_config",
]
