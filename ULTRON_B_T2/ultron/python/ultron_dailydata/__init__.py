"""Daily Data bridge — weather, sensex, and India news in one sidecar.

All three are free APIs (Open-Meteo, yfinance, Google News RSS) so no
keys end up in config.toml. Each source has its own poll cadence; one
service hosts all three to keep the stack footprint small.

Public entry::

    from ultron_dailydata import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import DailyDataConfig, load_dailydata_config
from .service import DailyDataService

_service: Optional[DailyDataService] = None


def init(config: Optional[DailyDataConfig] = None) -> DailyDataService:
    global _service
    if _service is None:
        cfg = config or load_dailydata_config()
        _service = DailyDataService(cfg)
    return _service


def get_service() -> Optional[DailyDataService]:
    return _service


__all__ = [
    "DailyDataConfig",
    "DailyDataService",
    "get_service",
    "init",
    "load_dailydata_config",
]
