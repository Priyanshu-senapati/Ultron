"""SQLite persistence for Module P (Money OS).

All writes funnel through this class. Threading model is single-writer:
the service awaits on its own asyncio loop and offloads sync I/O via
``run_in_executor`` (see ``service.py``), so we don't need WAL gymnastics.

Schema is intentionally small — five tables:

* ``accounts``      — wallets/cards/bank handles
* ``categories``    — spend/income buckets with a kind tag
* ``transactions``  — the ledger
* ``budgets``       — per-month category limits
* ``schema_meta``   — internal version tag
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_CATEGORIES, MoneyConfig
from .models import TX_KINDS, Account, Budget, Category, Transaction

logger = logging.getLogger("ultron.money.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    currency TEXT NOT NULL,
    opening_balance REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    category TEXT NOT NULL,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    merchant TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tx_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS ix_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS ix_tx_account ON transactions(account);

CREATE TABLE IF NOT EXISTS budgets (
    month TEXT NOT NULL,
    category TEXT NOT NULL,
    limit_amount REAL NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (month, category)
);
"""


class MoneyStore:
    def __init__(self, config: MoneyConfig) -> None:
        self._cfg = config
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Connection / schema ────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
                (str(SCHEMA_VERSION),),
            )
            for name, kind in DEFAULT_CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO categories(name,kind) VALUES(?,?)",
                    (name, kind),
                )
        logger.info("money store ready at %s", self._path)

    # ── Account API ────────────────────────────────────────────────────

    def upsert_account(self, account: Account) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(name,kind,currency,opening_balance,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    kind=excluded.kind,
                    currency=excluded.currency,
                    opening_balance=excluded.opening_balance
                """,
                (account.name, account.kind, account.currency,
                 account.opening_balance, account.created_at),
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name,kind,currency,opening_balance,created_at "
                "FROM accounts ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Category API ───────────────────────────────────────────────────

    def upsert_category(self, category: Category) -> None:
        if category.kind not in ("need", "want", "save", "income"):
            raise ValueError(f"bad category kind {category.kind!r}")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO categories(name,kind) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind",
                (category.name, category.kind),
            )

    def list_categories(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name,kind FROM categories ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Transaction API ────────────────────────────────────────────────

    def record_transaction(self, tx: Transaction) -> int:
        if tx.kind not in TX_KINDS:
            raise ValueError(f"bad tx kind {tx.kind!r}")
        if tx.amount <= 0:
            raise ValueError("amount must be > 0; use kind=expense for spend")
        with self._connect() as conn:
            # Auto-create unknown category as 'want' (sensible default).
            conn.execute(
                "INSERT OR IGNORE INTO categories(name,kind) VALUES(?, 'want')",
                (tx.category,),
            )
            cur = conn.execute(
                """
                INSERT INTO transactions(ts,amount,currency,category,account,kind,merchant,note)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (tx.ts, abs(tx.amount), tx.currency, tx.category,
                 tx.account, tx.kind, tx.merchant, tx.note),
            )
            tx_id = int(cur.lastrowid or 0)
        return tx_id

    def list_transactions(
        self,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        category: Optional[str] = None,
        account: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?")
            params.append(until_ts)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if account:
            clauses.append("account = ?")
            params.append(account)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        sql = (
            "SELECT id,ts,amount,currency,category,account,kind,merchant,note "
            f"FROM transactions {where} ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def delete_transaction(self, tx_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
            return cur.rowcount > 0

    # ── Budget API ─────────────────────────────────────────────────────

    def set_budget(self, budget: Budget) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO budgets(month,category,limit_amount,currency)
                VALUES(?,?,?,?)
                ON CONFLICT(month,category) DO UPDATE SET
                    limit_amount=excluded.limit_amount,
                    currency=excluded.currency
                """,
                (budget.month, budget.category, budget.limit, budget.currency),
            )

    def list_budgets(self, month: Optional[str] = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if month:
                rows = conn.execute(
                    "SELECT month,category,limit_amount AS \"limit\",currency "
                    "FROM budgets WHERE month=? ORDER BY category",
                    (month,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT month,category,limit_amount AS \"limit\",currency "
                    "FROM budgets ORDER BY month DESC, category"
                ).fetchall()
        return [dict(r) for r in rows]
