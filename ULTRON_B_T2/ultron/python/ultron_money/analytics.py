"""Aggregate / analytics views over the money ledger.

Pure functions of ``MoneyStore``. Nothing here mutates state; everything
is safe to call from a query path.
"""
from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import MoneyConfig
from .store import MoneyStore

logger = logging.getLogger("ultron.money.analytics")


def _month_bounds(month: str) -> tuple[float, float]:
    """Return (start_ts, end_ts_exclusive) for "YYYY-MM" in UTC."""
    year_s, mon_s = month.split("-", 1)
    year, mon = int(year_s), int(mon_s)
    start = datetime(year, mon, 1, tzinfo=timezone.utc).timestamp()
    last_day = calendar.monthrange(year, mon)[1]
    next_year = year + (1 if mon == 12 else 0)
    next_mon = 1 if mon == 12 else mon + 1
    end = datetime(next_year, next_mon, 1, tzinfo=timezone.utc).timestamp()
    _ = last_day  # silence linter; bound check via next-month start
    return start, end


def _current_month() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


class MoneyAnalytics:
    def __init__(self, store: MoneyStore, config: MoneyConfig) -> None:
        self._store = store
        self._cfg = config

    # ── Balances ───────────────────────────────────────────────────────

    def account_balances(self) -> list[dict[str, Any]]:
        """Opening balance + signed sum of transactions per account."""
        accounts = self._store.list_accounts()
        out: list[dict[str, Any]] = []
        for acct in accounts:
            txs = self._store.list_transactions(
                account=acct["name"], limit=self._cfg.max_query_rows
            )
            net = 0.0
            for t in txs:
                if t["kind"] == "income":
                    net += t["amount"]
                elif t["kind"] == "expense":
                    net -= t["amount"]
            balance = float(acct["opening_balance"]) + net
            out.append({
                "account": acct["name"],
                "kind": acct["kind"],
                "currency": acct["currency"],
                "opening_balance": acct["opening_balance"],
                "net_change": round(net, 2),
                "balance": round(balance, 2),
                "tx_count": len(txs),
            })
        return out

    # ── Monthly summary ────────────────────────────────────────────────

    def monthly_summary(self, month: Optional[str] = None) -> dict[str, Any]:
        month = month or _current_month()
        start, end = _month_bounds(month)
        txs = self._store.list_transactions(
            since_ts=start, until_ts=end, limit=self._cfg.max_query_rows
        )
        income = sum(t["amount"] for t in txs if t["kind"] == "income")
        expense = sum(t["amount"] for t in txs if t["kind"] == "expense")
        return {
            "month": month,
            "tx_count": len(txs),
            "income": round(income, 2),
            "expense": round(expense, 2),
            "net": round(income - expense, 2),
            "currency": self._cfg.default_currency,
        }

    # ── Category rollup ────────────────────────────────────────────────

    def category_rollup(self, month: Optional[str] = None) -> list[dict[str, Any]]:
        month = month or _current_month()
        start, end = _month_bounds(month)
        txs = self._store.list_transactions(
            since_ts=start, until_ts=end, limit=self._cfg.max_query_rows
        )
        agg: dict[str, dict[str, Any]] = {}
        for t in txs:
            if t["kind"] != "expense":
                continue
            entry = agg.setdefault(t["category"], {"category": t["category"], "spent": 0.0, "tx_count": 0})
            entry["spent"] += t["amount"]
            entry["tx_count"] += 1
        rows = sorted(agg.values(), key=lambda r: r["spent"], reverse=True)
        for r in rows:
            r["spent"] = round(r["spent"], 2)
        return rows

    # ── Top merchants ──────────────────────────────────────────────────

    def top_merchants(self, month: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        month = month or _current_month()
        start, end = _month_bounds(month)
        txs = self._store.list_transactions(
            since_ts=start, until_ts=end, limit=self._cfg.max_query_rows
        )
        agg: dict[str, dict[str, Any]] = {}
        for t in txs:
            if t["kind"] != "expense":
                continue
            merchant = (t["merchant"] or "(unknown)").strip()
            entry = agg.setdefault(merchant, {"merchant": merchant, "spent": 0.0, "tx_count": 0})
            entry["spent"] += t["amount"]
            entry["tx_count"] += 1
        rows = sorted(agg.values(), key=lambda r: r["spent"], reverse=True)[:max(1, limit)]
        for r in rows:
            r["spent"] = round(r["spent"], 2)
        return rows

    # ── Budget check ───────────────────────────────────────────────────

    def budget_check(self, month: Optional[str] = None) -> list[dict[str, Any]]:
        """Per-budget status for the given month.

        Each row carries ``spent``, ``limit``, ``remaining``, ``utilization``
        (0-1), and a string ``status`` of ok / warn / over.
        """
        month = month or _current_month()
        rollup = {r["category"]: r["spent"] for r in self.category_rollup(month)}
        budgets = self._store.list_budgets(month=month)
        threshold = self._cfg.budget_alert_threshold
        out: list[dict[str, Any]] = []
        for b in budgets:
            spent = float(rollup.get(b["category"], 0.0))
            limit = float(b["limit"])
            utilization = (spent / limit) if limit > 0 else 0.0
            if utilization >= 1.0:
                status = "over"
            elif utilization >= threshold:
                status = "warn"
            else:
                status = "ok"
            out.append({
                "month": b["month"],
                "category": b["category"],
                "spent": round(spent, 2),
                "limit": round(limit, 2),
                "remaining": round(limit - spent, 2),
                "utilization": round(utilization, 3),
                "currency": b["currency"],
                "status": status,
            })
        return out

    # ── Convenience helpers ────────────────────────────────────────────

    @staticmethod
    def current_month() -> str:
        return _current_month()

    @staticmethod
    def month_bounds(month: str) -> tuple[float, float]:
        return _month_bounds(month)
