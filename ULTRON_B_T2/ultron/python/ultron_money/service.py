"""MoneyService — WS-facing owner of the ledger.

Subscribes:
  - ``money_record_request``    — payload: a Transaction dict
  - ``money_query_request``     — payload: ``{kind, ...}``
  - ``money_budget_set_request``— payload: a Budget dict
  - ``money_account_set_request``— payload: an Account dict

Publishes:
  - ``money_recorded``        — payload: ``{id, tx}``
  - ``money_query_result``    — payload: query result
  - ``money_budget_alert``    — payload: rows where status != "ok"
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .analytics import MoneyAnalytics
from .config import MoneyConfig
from .models import Account, Budget, Category, Transaction
from .store import MoneyStore

logger = logging.getLogger("ultron.money.service")


class MoneyService:
    def __init__(self, config: MoneyConfig) -> None:
        self._cfg = config
        self._store = MoneyStore(config)
        self._analytics = MoneyAnalytics(self._store, config)
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()

    @property
    def store(self) -> MoneyStore:
        return self._store

    @property
    def analytics(self) -> MoneyAnalytics:
        return self._analytics

    # ── Public Python API ──────────────────────────────────────────────

    async def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            tx = Transaction(
                ts=float(payload.get("ts") or time.time()),
                amount=float(payload["amount"]),
                currency=str(payload.get("currency") or self._cfg.default_currency),
                category=str(payload.get("category") or "other"),
                account=str(payload.get("account") or "default"),
                kind=str(payload.get("kind") or "expense"),
                merchant=str(payload.get("merchant") or ""),
                note=str(payload.get("note") or ""),
            )
            tx_id = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.record_transaction(tx)
            )
            tx.id = tx_id
            result = {"id": tx_id, "tx": tx.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("money_recorded", result)
            # If this transaction is in the current month, check budgets and
            # surface any alerts. Cheap — runs over month-bounded data.
            try:
                alerts = [
                    r for r in self._analytics.budget_check()
                    if r["status"] != "ok" and r["category"] == tx.category
                ]
                if alerts:
                    await self._bridge.publish("money_budget_alert", {"alerts": alerts})
            except Exception:  # noqa: BLE001
                logger.exception("budget alert check failed")
        return result

    async def set_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            b = Budget(
                month=str(payload["month"]),
                category=str(payload["category"]),
                limit=float(payload["limit"]),
                currency=str(payload.get("currency") or self._cfg.default_currency),
            )
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.set_budget(b)
            )
        return {"budget": b.as_dict()}

    async def set_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            a = Account(
                name=str(payload["name"]),
                kind=str(payload.get("kind") or "cash"),
                currency=str(payload.get("currency") or self._cfg.default_currency),
                opening_balance=float(payload.get("opening_balance") or 0.0),
            )
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.upsert_account(a)
            )
        return {"account": a.as_dict()}

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "monthly_summary"))
        loop = asyncio.get_running_loop()
        if kind == "list_transactions":
            rows = await loop.run_in_executor(None, lambda: self._store.list_transactions(
                since_ts=payload.get("since_ts"),
                until_ts=payload.get("until_ts"),
                category=payload.get("category"),
                account=payload.get("account"),
                kind=payload.get("tx_kind"),
                limit=int(payload.get("limit", 100)),
            ))
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "monthly_summary":
            result = {"kind": kind, "summary": await loop.run_in_executor(
                None, lambda: self._analytics.monthly_summary(payload.get("month"))
            )}
        elif kind == "category_rollup":
            rows = await loop.run_in_executor(
                None, lambda: self._analytics.category_rollup(payload.get("month"))
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "top_merchants":
            rows = await loop.run_in_executor(
                None, lambda: self._analytics.top_merchants(
                    payload.get("month"), int(payload.get("limit", 10))
                )
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "budget_check":
            rows = await loop.run_in_executor(
                None, lambda: self._analytics.budget_check(payload.get("month"))
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "account_balances":
            rows = await loop.run_in_executor(
                None, lambda: self._analytics.account_balances()
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_budgets":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_budgets(payload.get("month"))
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_categories":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_categories()
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_accounts":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_accounts()
            )
            result = {"kind": kind, "rows": rows}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("money_query_result", result)
        return result

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start money service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "money_record_request",
                "money_query_request",
                "money_budget_set_request",
                "money_account_set_request",
            ],
            role="money-os",
        )
        logger.info("MoneyService starting — db=%s", self._cfg.db_path)
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "money_record_request":
                await self.record(payload)
            elif kind == "money_query_request":
                await self.query(payload)
            elif kind == "money_budget_set_request":
                await self.set_budget(payload)
            elif kind == "money_account_set_request":
                await self.set_account(payload)
        except Exception:  # noqa: BLE001
            logger.exception("handler failed for kind=%s", kind)
