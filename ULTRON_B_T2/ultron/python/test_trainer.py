"""Tests for Module TT (Trainer Twin)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ultron_trainer.analytics import TrainerAnalytics
from ultron_trainer.config import TrainerConfig
from ultron_trainer.models import BodyMetric, SleepLog, Workout
from ultron_trainer.store import TrainerStore


def _cfg(tmp_path: Path) -> TrainerConfig:
    return TrainerConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "trainer.db",
    )


@pytest.fixture
def store(tmp_path: Path) -> TrainerStore:
    return TrainerStore(_cfg(tmp_path))


@pytest.fixture
def pair(tmp_path: Path) -> tuple[TrainerStore, TrainerAnalytics]:
    cfg = _cfg(tmp_path)
    s = TrainerStore(cfg)
    return s, TrainerAnalytics(s, cfg)


def _today_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _days_ago_ts(days: int) -> float:
    return (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()


def _days_ago_date(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# ── Workout store ─────────────────────────────────────────────────────


def test_record_workout(store: TrainerStore) -> None:
    wid = store.record_workout(Workout(
        ts=_today_ts(), exercise="pushups", sets=3, reps=15, duration_secs=180,
    ))
    assert wid > 0
    rows = store.list_workouts()
    assert len(rows) == 1
    assert rows[0]["exercise"] == "pushups"


def test_record_workout_rejects_missing_exercise(store: TrainerStore) -> None:
    with pytest.raises(ValueError):
        store.record_workout(Workout(ts=_today_ts(), exercise=""))


def test_record_workout_rejects_negative(store: TrainerStore) -> None:
    with pytest.raises(ValueError):
        store.record_workout(Workout(ts=_today_ts(), exercise="x", reps=-1))


def test_list_workouts_filter_by_exercise(store: TrainerStore) -> None:
    store.record_workout(Workout(ts=_today_ts(), exercise="pushups"))
    store.record_workout(Workout(ts=_today_ts(), exercise="squats"))
    rows = store.list_workouts(exercise="squats")
    assert len(rows) == 1
    assert rows[0]["exercise"] == "squats"


def test_delete_workout(store: TrainerStore) -> None:
    wid = store.record_workout(Workout(ts=_today_ts(), exercise="x"))
    assert store.delete_workout(wid) is True
    assert store.delete_workout(wid) is False


# ── Sleep store ───────────────────────────────────────────────────────


def test_record_sleep(store: TrainerStore) -> None:
    bedtime = _today_ts() - 8 * 3600
    wake = _today_ts()
    store.record_sleep(SleepLog(
        date="2026-05-16", bedtime_ts=bedtime, wake_ts=wake, quality=4,
    ))
    rows = store.list_sleep()
    assert len(rows) == 1
    assert rows[0]["quality"] == 4


def test_sleep_upsert_overwrites(store: TrainerStore) -> None:
    bedtime = _today_ts() - 8 * 3600
    wake = _today_ts()
    store.record_sleep(SleepLog(date="2026-05-16", bedtime_ts=bedtime, wake_ts=wake, quality=3))
    store.record_sleep(SleepLog(date="2026-05-16", bedtime_ts=bedtime, wake_ts=wake, quality=5))
    rows = store.list_sleep()
    assert len(rows) == 1
    assert rows[0]["quality"] == 5


def test_sleep_rejects_wake_before_bed(store: TrainerStore) -> None:
    with pytest.raises(ValueError):
        store.record_sleep(SleepLog(date="2026-05-16", bedtime_ts=100, wake_ts=50))


# ── Body metric store ─────────────────────────────────────────────────


def test_record_metric_all_fields(store: TrainerStore) -> None:
    mid = store.record_metric(BodyMetric(
        ts=_today_ts(), weight_kg=72.5, mood=4, energy=3, note="solid",
    ))
    assert mid > 0
    rows = store.list_metrics()
    assert rows[0]["weight_kg"] == 72.5


def test_record_metric_requires_at_least_one_value(store: TrainerStore) -> None:
    with pytest.raises(ValueError):
        store.record_metric(BodyMetric(ts=_today_ts()))


def test_record_metric_validates_ranges(store: TrainerStore) -> None:
    with pytest.raises(ValueError):
        store.record_metric(BodyMetric(ts=_today_ts(), mood=9))
    with pytest.raises(ValueError):
        store.record_metric(BodyMetric(ts=_today_ts(), weight_kg=-1))


# ── Analytics ─────────────────────────────────────────────────────────


def test_workout_streak_three_days(pair: tuple[TrainerStore, TrainerAnalytics]) -> None:
    store, ana = pair
    for d in (0, 1, 2):
        store.record_workout(Workout(ts=_days_ago_ts(d), exercise="x"))
    s = ana.streak("workout")
    assert s["current"] == 3


def test_workout_streak_breaks_on_gap(pair: tuple[TrainerStore, TrainerAnalytics]) -> None:
    store, ana = pair
    for d in (0, 1, 3):  # missing day 2
        store.record_workout(Workout(ts=_days_ago_ts(d), exercise="x"))
    assert ana.streak("workout")["current"] == 2


def test_all_streaks_returns_each_kind(pair: tuple[TrainerStore, TrainerAnalytics]) -> None:
    store, ana = pair
    store.record_workout(Workout(ts=_today_ts(), exercise="x"))
    rows = ana.all_streaks()
    kinds = {r["kind"] for r in rows}
    assert {"workout", "sleep", "weight"} <= kinds


def test_weekly_workout_summary(pair: tuple[TrainerStore, TrainerAnalytics]) -> None:
    store, ana = pair
    for d in range(0, 5):
        store.record_workout(Workout(
            ts=_days_ago_ts(d), exercise="pushups",
            sets=3, reps=10, duration_secs=600,
        ))
    summary = ana.weekly_workout_summary(weeks=1)
    assert summary["sessions"] == 5
    assert summary["active_days"] == 5
    assert summary["total_minutes"] == 50.0
    assert summary["total_reps"] == 150


def test_weekly_sleep_summary_flags_below_target(
    pair: tuple[TrainerStore, TrainerAnalytics],
) -> None:
    store, ana = pair
    for d in range(0, 4):
        wake = _days_ago_ts(d)
        bed = wake - 6 * 3600  # only 6 hours
        store.record_sleep(SleepLog(date=_days_ago_date(d), bedtime_ts=bed, wake_ts=wake, quality=3))
    summary = ana.weekly_sleep_summary(weeks=1)
    assert summary["nights"] == 4
    assert summary["below_target"] == 4  # all under 7.5
    assert summary["avg_hours"] == pytest.approx(6.0, rel=0.01)


def test_weight_trend(pair: tuple[TrainerStore, TrainerAnalytics]) -> None:
    store, ana = pair
    store.record_metric(BodyMetric(ts=_days_ago_ts(20), weight_kg=75))
    store.record_metric(BodyMetric(ts=_days_ago_ts(10), weight_kg=73))
    store.record_metric(BodyMetric(ts=_days_ago_ts(1), weight_kg=72))
    trend = ana.weight_trend(days=30)
    assert trend["samples"] == 3
    assert trend["first"] == 75
    assert trend["last"] == 72
    assert trend["delta"] == -3.0


def test_latest_metrics_returns_most_recent_non_null(
    pair: tuple[TrainerStore, TrainerAnalytics],
) -> None:
    store, ana = pair
    store.record_metric(BodyMetric(ts=_days_ago_ts(5), weight_kg=75, mood=3))
    store.record_metric(BodyMetric(ts=_days_ago_ts(2), energy=4))
    latest = ana.latest_metrics()
    assert latest["weight_kg"] == 75
    assert latest["mood"] == 3
    assert latest["energy"] == 4


# ── Singleton ─────────────────────────────────────────────────────────


def test_trainer_service_singleton(tmp_path: Path) -> None:
    from ultron_trainer import get_service, init
    import ultron_trainer
    ultron_trainer._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_trainer._service = None  # noqa: SLF001
