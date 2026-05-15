"""Tests for Module S+J (Dream Weaver + Scheduler)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ultron_planner.config import PlannerConfig
from ultron_planner.models import Block, Event, Goal, Outcome
from ultron_planner.planner import Planner
from ultron_planner.store import PlannerStore


def _cfg(tmp_path: Path) -> PlannerConfig:
    return PlannerConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "planner.db",
        tick_seconds=30,
        upcoming_horizon_seconds=300,
    )


@pytest.fixture
def store(tmp_path: Path) -> PlannerStore:
    return PlannerStore(_cfg(tmp_path))


@pytest.fixture
def planner(tmp_path: Path) -> tuple[PlannerStore, Planner]:
    cfg = _cfg(tmp_path)
    s = PlannerStore(cfg)
    return s, Planner(s, cfg)


# ── Goals ─────────────────────────────────────────────────────────────


def test_upsert_goal_inserts(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="Ship ULTRON", dream_kind="career"))
    assert gid > 0
    rows = store.list_goals()
    assert len(rows) == 1
    assert rows[0]["title"] == "Ship ULTRON"


def test_upsert_goal_updates_existing(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="A"))
    store.upsert_goal(Goal(title="A-renamed", status="paused", id=gid))
    rows = store.list_goals()
    assert rows[0]["title"] == "A-renamed"
    assert rows[0]["status"] == "paused"


def test_goal_rejects_bad_status(store: PlannerStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_goal(Goal(title="x", status="bogus"))


def test_delete_goal_cascades_outcomes(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="x"))
    store.upsert_outcome(Outcome(goal_id=gid, title="o1"))
    assert store.delete_goal(gid) is True
    assert store.list_outcomes(goal_id=gid) == []


# ── Outcomes ──────────────────────────────────────────────────────────


def test_upsert_outcome(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="g"))
    oid = store.upsert_outcome(Outcome(goal_id=gid, title="o", weight=2.0))
    assert oid > 0
    rows = store.list_outcomes(goal_id=gid)
    assert rows[0]["weight"] == 2.0


def test_outcome_requires_real_goal(store: PlannerStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_outcome(Outcome(goal_id=999, title="orphan"))


def test_outcome_bad_status(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="g"))
    with pytest.raises(ValueError):
        store.upsert_outcome(Outcome(goal_id=gid, title="o", status="weird"))


# ── Blocks ────────────────────────────────────────────────────────────


def test_schedule_block(store: PlannerStore) -> None:
    now = time.time()
    bid = store.schedule_block(Block(
        ts_start=now, ts_end=now + 1800, title="focus", kind="focus",
    ))
    assert bid > 0
    rows = store.list_blocks()
    assert len(rows) == 1
    assert rows[0]["title"] == "focus"


def test_block_requires_positive_duration(store: PlannerStore) -> None:
    now = time.time()
    with pytest.raises(ValueError):
        store.schedule_block(Block(ts_start=now, ts_end=now, title="x"))


def test_block_links_to_outcome(store: PlannerStore) -> None:
    gid = store.upsert_goal(Goal(title="g"))
    oid = store.upsert_outcome(Outcome(goal_id=gid, title="o"))
    now = time.time()
    store.schedule_block(Block(
        ts_start=now, ts_end=now + 600, title="x", outcome_id=oid,
    ))
    rows = store.list_blocks(outcome_id=oid)
    assert len(rows) == 1
    assert rows[0]["outcome_id"] == oid


def test_block_rejects_missing_outcome(store: PlannerStore) -> None:
    now = time.time()
    with pytest.raises(ValueError):
        store.schedule_block(Block(
            ts_start=now, ts_end=now + 60, title="x", outcome_id=999,
        ))


# ── Events ────────────────────────────────────────────────────────────


def test_schedule_and_fire_event(store: PlannerStore) -> None:
    now = time.time()
    eid = store.schedule_event(Event(ts=now - 60, title="ping"))
    pending = store.pending_events(until_ts=now)
    assert len(pending) == 1
    store.mark_event_fired(eid, now)
    assert store.pending_events(until_ts=now) == []


def test_pending_events_window(store: PlannerStore) -> None:
    now = time.time()
    store.schedule_event(Event(ts=now + 100, title="future"))
    store.schedule_event(Event(ts=now - 10, title="past"))
    pending = store.pending_events(until_ts=now)
    assert len(pending) == 1
    assert pending[0]["title"] == "past"


# ── Planner (derived views) ───────────────────────────────────────────


def test_goal_progress(planner: tuple[PlannerStore, Planner]) -> None:
    store, plan = planner
    gid = store.upsert_goal(Goal(title="g"))
    store.upsert_outcome(Outcome(goal_id=gid, title="o1", weight=1.0, status="done"))
    store.upsert_outcome(Outcome(goal_id=gid, title="o2", weight=3.0, status="pending"))
    p = plan.goal_progress(gid)
    assert p["outcomes_total"] == 2
    assert p["outcomes_done"] == 1
    # done weight 1 / total weight 4 = 0.25
    assert p["progress"] == 0.25


def test_today_summary(planner: tuple[PlannerStore, Planner]) -> None:
    store, plan = planner
    now = time.time()
    store.schedule_block(Block(ts_start=now + 60, ts_end=now + 1800, title="focus"))
    store.schedule_event(Event(ts=now + 120, title="standup"))
    s = plan.today_summary()
    assert len(s["blocks"]) >= 1
    assert len(s["events"]) >= 1


def test_outcome_time_spent(planner: tuple[PlannerStore, Planner]) -> None:
    store, plan = planner
    gid = store.upsert_goal(Goal(title="g"))
    oid = store.upsert_outcome(Outcome(goal_id=gid, title="o"))
    now = time.time()
    store.schedule_block(Block(
        ts_start=now - 600, ts_end=now, title="x", outcome_id=oid,
    ))
    spent = plan.outcome_time_spent(oid, days=7)
    assert spent["block_count"] == 1
    assert spent["minutes"] == 10.0


def test_all_goal_progress_only_active(planner: tuple[PlannerStore, Planner]) -> None:
    store, plan = planner
    a = store.upsert_goal(Goal(title="active"))
    p = store.upsert_goal(Goal(title="paused", status="paused"))
    store.upsert_outcome(Outcome(goal_id=a, title="o"))
    store.upsert_outcome(Outcome(goal_id=p, title="o2"))
    rows = plan.all_goal_progress()
    titles = {r["title"] for r in rows}
    assert "active" in titles
    assert "paused" not in titles


# ── Singleton ─────────────────────────────────────────────────────────


def test_planner_service_singleton(tmp_path: Path) -> None:
    from ultron_planner import get_service, init
    import ultron_planner
    ultron_planner._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_planner._service = None  # noqa: SLF001
