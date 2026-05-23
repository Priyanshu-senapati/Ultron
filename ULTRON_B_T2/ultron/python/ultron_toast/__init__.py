"""Windows toast notifications — surfaces significant bus events into
the OS notification centre. No Python toast dep; we call PowerShell +
WinRT directly so the bridge works on every Windows 10/11 box.

Public entry::

    from ultron_toast import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import ToastConfig, load_toast_config
from .notifier import show
from .router import ToastRouter, ToastSpec
from .service import ToastService

_service: Optional[ToastService] = None


def init(config: Optional[ToastConfig] = None) -> ToastService:
    global _service
    if _service is None:
        cfg = config or load_toast_config()
        _service = ToastService(cfg)
    return _service


def get_service() -> Optional[ToastService]:
    return _service


__all__ = [
    "ToastConfig",
    "ToastRouter",
    "ToastService",
    "ToastSpec",
    "get_service",
    "init",
    "load_toast_config",
    "show",
]
