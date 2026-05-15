"""Module P — Money OS.

Private personal-finance ledger. Stores transactions, accounts, categories
and per-month budgets in a small SQLite database. Surfaces read-only
queries to agents via Module E's ``money_query`` tool.

All money data is classified LOCAL_ONLY by Module N — it never leaves the
machine and never enters an LLM prompt unless the user explicitly opts
in by quoting a specific number.

Public entry::

    from ultron_money import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .analytics import MoneyAnalytics
from .config import MoneyConfig, load_money_config
from .service import MoneyService
from .store import MoneyStore

_service: Optional[MoneyService] = None


def init(config: Optional[MoneyConfig] = None) -> MoneyService:
    global _service
    if _service is None:
        cfg = config or load_money_config()
        _service = MoneyService(cfg)
    return _service


def get_service() -> Optional[MoneyService]:
    return _service


__all__ = [
    "MoneyAnalytics",
    "MoneyConfig",
    "MoneyService",
    "MoneyStore",
    "get_service",
    "init",
    "load_money_config",
]
