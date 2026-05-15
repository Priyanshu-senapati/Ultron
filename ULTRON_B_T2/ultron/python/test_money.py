"""Tests for Module P (Money OS)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ultron_money.analytics import MoneyAnalytics
from ultron_money.config import MoneyConfig
from ultron_money.models import Account, Budget, Transaction
from ultron_money.store import MoneyStore


# ── Fixtures ──────────────────────────────────────────────────────────


def _cfg(tmp_path: Path) -> MoneyConfig:
    return MoneyConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "money.db",
        default_currency="INR",
        budget_alert_threshold=0.8,
        max_query_rows=500,
    )


@pytest.fixture
def store(tmp_path: Path) -> MoneyStore:
    return MoneyStore(_cfg(tmp_path))


@pytest.fixture
def analytics(tmp_path: Path) -> tuple[MoneyStore, MoneyAnalytics]:
    cfg = _cfg(tmp_path)
    s = MoneyStore(cfg)
    return s, MoneyAnalytics(s, cfg)


def _month_ts(year: int, month: int, day: int = 15) -> float:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp()


# ── Store ─────────────────────────────────────────────────────────────


def test_default_categories_seeded(store: MoneyStore) -> None:
    cats = {c["name"] for c in store.list_categories()}
    assert "food" in cats
    assert "salary" in cats
    assert "savings" in cats


def test_record_and_list_transaction(store: MoneyStore) -> None:
    tx = Transaction(
        ts=time.time(), amount=250.0, currency="INR",
        category="food", account="cash", kind="expense",
        merchant="Cafe", note="lunch",
    )
    tx_id = store.record_transaction(tx)
    assert tx_id > 0
    rows = store.list_transactions(limit=10)
    assert len(rows) == 1
    assert rows[0]["merchant"] == "Cafe"
    assert rows[0]["amount"] == 250.0


def test_record_rejects_bad_kind(store: MoneyStore) -> None:
    with pytest.raises(ValueError):
        store.record_transaction(Transaction(
            ts=time.time(), amount=10, currency="INR",
            category="food", account="cash", kind="bogus",
        ))


def test_record_rejects_non_positive_amount(store: MoneyStore) -> None:
    with pytest.raises(ValueError):
        store.record_transaction(Transaction(
            ts=time.time(), amount=0, currency="INR",
            category="food", account="cash", kind="expense",
        ))


def test_auto_create_unknown_category(store: MoneyStore) -> None:
    store.record_transaction(Transaction(
        ts=time.time(), amount=10, currency="INR",
        category="hover-board-fuel", account="cash", kind="expense",
    ))
    cats = {c["name"]: c["kind"] for c in store.list_categories()}
    assert cats.get("hover-board-fuel") == "want"


def test_upsert_account(store: MoneyStore) -> None:
    store.upsert_account(Account(name="hdfc", kind="bank", currency="INR", opening_balance=1000))
    rows = store.list_accounts()
    assert any(r["name"] == "hdfc" and r["opening_balance"] == 1000 for r in rows)
    store.upsert_account(Account(name="hdfc", kind="bank", currency="INR", opening_balance=2500))
    rows = store.list_accounts()
    assert next(r for r in rows if r["name"] == "hdfc")["opening_balance"] == 2500


def test_set_and_list_budget(store: MoneyStore) -> None:
    store.set_budget(Budget(month="2026-05", category="food", limit=5000, currency="INR"))
    store.set_budget(Budget(month="2026-05", category="food", limit=6000, currency="INR"))
    rows = store.list_budgets("2026-05")
    assert len(rows) == 1
    assert rows[0]["limit"] == 6000


def test_delete_transaction(store: MoneyStore) -> None:
    tx_id = store.record_transaction(Transaction(
        ts=time.time(), amount=100, currency="INR",
        category="food", account="cash", kind="expense",
    ))
    assert store.delete_transaction(tx_id) is True
    assert store.delete_transaction(tx_id) is False
    assert store.list_transactions() == []


def test_list_transactions_filters(store: MoneyStore) -> None:
    base = _month_ts(2026, 4)
    for i, kind in enumerate(("expense", "income", "expense")):
        store.record_transaction(Transaction(
            ts=base + i, amount=10 * (i + 1), currency="INR",
            category="food" if kind == "expense" else "salary",
            account="cash", kind=kind,
        ))
    expenses = store.list_transactions(kind="expense")
    assert len(expenses) == 2
    foods = store.list_transactions(category="food")
    assert len(foods) == 2


# ── Analytics ─────────────────────────────────────────────────────────


def test_monthly_summary_income_expense_net(
    analytics: tuple[MoneyStore, MoneyAnalytics],
) -> None:
    store, ana = analytics
    base = _month_ts(2026, 5)
    store.record_transaction(Transaction(
        ts=base, amount=50000, currency="INR",
        category="salary", account="hdfc", kind="income",
    ))
    store.record_transaction(Transaction(
        ts=base + 1, amount=12000, currency="INR",
        category="rent", account="hdfc", kind="expense",
    ))
    summary = ana.monthly_summary("2026-05")
    assert summary["income"] == 50000
    assert summary["expense"] == 12000
    assert summary["net"] == 38000


def test_category_rollup_orders_by_spend(
    analytics: tuple[MoneyStore, MoneyAnalytics],
) -> None:
    store, ana = analytics
    base = _month_ts(2026, 5)
    store.record_transaction(Transaction(ts=base, amount=200, currency="INR",
        category="food", account="cash", kind="expense"))
    store.record_transaction(Transaction(ts=base + 1, amount=900, currency="INR",
        category="rent", account="hdfc", kind="expense"))
    store.record_transaction(Transaction(ts=base + 2, amount=100, currency="INR",
        category="food", account="cash", kind="expense"))
    rows = ana.category_rollup("2026-05")
    assert rows[0]["category"] == "rent"
    assert rows[1]["category"] == "food"
    assert rows[1]["spent"] == 300


def test_top_merchants(analytics: tuple[MoneyStore, MoneyAnalytics]) -> None:
    store, ana = analytics
    base = _month_ts(2026, 5)
    for m, amt in (("Zomato", 200), ("Zomato", 300), ("Amazon", 1500)):
        store.record_transaction(Transaction(ts=base, amount=amt, currency="INR",
            category="food", account="cash", kind="expense", merchant=m))
    rows = ana.top_merchants("2026-05", limit=5)
    assert rows[0]["merchant"] == "Amazon"
    assert rows[1]["merchant"] == "Zomato"
    assert rows[1]["spent"] == 500


def test_budget_check_status(analytics: tuple[MoneyStore, MoneyAnalytics]) -> None:
    store, ana = analytics
    base = _month_ts(2026, 5)
    store.set_budget(Budget(month="2026-05", category="food", limit=1000, currency="INR"))
    store.set_budget(Budget(month="2026-05", category="rent", limit=10000, currency="INR"))
    store.record_transaction(Transaction(ts=base, amount=850, currency="INR",
        category="food", account="cash", kind="expense"))   # 85 % → warn
    store.record_transaction(Transaction(ts=base, amount=15000, currency="INR",
        category="rent", account="hdfc", kind="expense"))   # 150 % → over
    rows = {r["category"]: r for r in ana.budget_check("2026-05")}
    assert rows["food"]["status"] == "warn"
    assert rows["rent"]["status"] == "over"
    assert rows["rent"]["remaining"] == -5000


def test_account_balances(analytics: tuple[MoneyStore, MoneyAnalytics]) -> None:
    store, ana = analytics
    store.upsert_account(Account(name="hdfc", kind="bank", currency="INR", opening_balance=10000))
    base = _month_ts(2026, 5)
    store.record_transaction(Transaction(ts=base, amount=2000, currency="INR",
        category="salary", account="hdfc", kind="income"))
    store.record_transaction(Transaction(ts=base + 1, amount=500, currency="INR",
        category="food", account="hdfc", kind="expense"))
    balances = {b["account"]: b for b in ana.account_balances()}
    assert balances["hdfc"]["balance"] == 11500
    assert balances["hdfc"]["net_change"] == 1500


def test_month_bounds_correct() -> None:
    start, end = MoneyAnalytics.month_bounds("2026-12")
    assert datetime.fromtimestamp(start, tz=timezone.utc).month == 12
    # Wraps to Jan of the next year.
    assert datetime.fromtimestamp(end, tz=timezone.utc).year == 2027
    assert datetime.fromtimestamp(end, tz=timezone.utc).month == 1


# ── Singleton ─────────────────────────────────────────────────────────


def test_money_service_singleton(tmp_path: Path) -> None:
    from ultron_money import get_service, init
    import ultron_money
    ultron_money._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_money._service = None  # noqa: SLF001
