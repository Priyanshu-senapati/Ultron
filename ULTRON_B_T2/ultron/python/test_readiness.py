"""Unit tests for Roadmap #3 — Readiness Score.

Covers calculator math, EWMA state, flow 24h pool, and end-to-end
``compute_score`` integration.
"""
from __future__ import annotations

import time

from ultron_readiness import (
    ReadinessConfig,
    ReadinessState,
    compute_score,
    get_service,
    init,
    score_activity,
    score_calm,
    score_flow_yesterday,
    score_sleep,
)


def _cfg(**overrides) -> ReadinessConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        db_path="C:/tmp/readiness_test.db",
        weight_sleep=40.0,
        weight_flow_yesterday=30.0,
        weight_calm=15.0,
        weight_activity=15.0,
        sleep_target_hours=7.5,
        flow_target_minutes=120.0,
        calm_tension_threshold=0.3,
        calm_ewma_half_life_secs=1800.0,
        activity_window_hours=24.0,
        recompute_interval_secs=300.0,
        boot_delay_secs=0.0,
    )
    defaults.update(overrides)
    return ReadinessConfig(**defaults)


# ── Sleep component ────────────────────────────────────────────────────


def test_sleep_full_score_within_half_hour_of_target():
    cfg = _cfg(sleep_target_hours=7.5)
    assert score_sleep(7.5, cfg).score == 40.0
    assert score_sleep(7.0, cfg).score == 40.0
    assert score_sleep(8.0, cfg).score == 40.0


def test_sleep_partial_score_within_one_and_a_half_hours():
    cfg = _cfg()
    assert score_sleep(6.0, cfg).score == 30.0   # 75% of 40
    assert score_sleep(9.0, cfg).score == 30.0


def test_sleep_low_score_far_from_target():
    cfg = _cfg()
    assert score_sleep(4.0, cfg).score == 8.0    # 20% of 40
    assert score_sleep(3.0, cfg).score == 2.0    # 5% of 40


def test_sleep_no_data_neutral():
    cfg = _cfg()
    c = score_sleep(None, cfg)
    assert c.score == 20.0
    assert "no sleep" in c.detail.lower()


# ── Flow component ─────────────────────────────────────────────────────


def test_flow_full_score_at_target_minutes():
    cfg = _cfg(flow_target_minutes=120.0)
    assert score_flow_yesterday(120.0, cfg).score == 30.0
    assert score_flow_yesterday(200.0, cfg).score == 30.0


def test_flow_zero_minutes_zero_score():
    cfg = _cfg()
    assert score_flow_yesterday(0.0, cfg).score == 0.0


def test_flow_intermediate_thresholds():
    cfg = _cfg(flow_target_minutes=120.0)
    assert score_flow_yesterday(80.0, cfg).score == 22.5   # 75% of 30
    assert score_flow_yesterday(40.0, cfg).score == 15.0   # 50% of 30
    assert score_flow_yesterday(10.0, cfg).score == 7.5    # 25% of 30


# ── Calm component ─────────────────────────────────────────────────────


def test_calm_full_when_tension_below_threshold():
    cfg = _cfg(calm_tension_threshold=0.3)
    assert score_calm(0.10, cfg).score == 15.0
    assert score_calm(0.30, cfg).score == 15.0


def test_calm_partial_at_moderate_tension():
    cfg = _cfg(calm_tension_threshold=0.3)
    c = score_calm(0.45, cfg)
    assert 9.5 <= c.score <= 10.5


def test_calm_zero_when_tension_high():
    cfg = _cfg(calm_tension_threshold=0.3)
    assert score_calm(0.80, cfg).score == 0.0


# ── Activity component ─────────────────────────────────────────────────


def test_activity_full_when_workout_within_window():
    cfg = _cfg(activity_window_hours=24.0)
    now = 10_000.0
    c = score_activity(now - 3600.0, now, cfg)
    assert c.score == 15.0


def test_activity_neutral_when_no_workout():
    cfg = _cfg()
    c = score_activity(None, time.time(), cfg)
    assert 3.5 <= c.score <= 4.5


def test_activity_neutral_when_workout_too_old():
    cfg = _cfg(activity_window_hours=24.0)
    now = 10_000.0
    c = score_activity(now - 30 * 3600.0, now, cfg)
    assert 3.5 <= c.score <= 4.5


# ── State (EWMA tension + flow 24h) ────────────────────────────────────


def test_state_tension_ewma_decays_over_time():
    s = ReadinessState(calm_half_life_secs=600.0)   # 10-min half-life
    s.update_tension(0.8, ts=1000.0)
    assert s.tension_ewma == 0.8
    # Pure-calm sample one full half-life later → average is ~0.4.
    s.update_tension(0.0, ts=1600.0)
    assert 0.35 <= s.tension_ewma <= 0.45


def test_state_flow_minutes_24h_pool_prunes_old():
    s = ReadinessState(calm_half_life_secs=1800.0)
    now = 1_000_000.0
    # Old: 30 hours ago, should be pruned out of 24h window.
    s.update_flow_session(end_ts=now - 30 * 3600.0, duration_secs=600.0)
    # Recent: 1h ago, 25 min.
    s.update_flow_session(end_ts=now - 3600.0, duration_secs=25 * 60.0)
    # Even more recent: 10 min ago, 45 min.
    s.update_flow_session(end_ts=now - 600.0, duration_secs=45 * 60.0)
    mins = s.flow_minutes_in_last_24h(now=now)
    assert 69.0 <= mins <= 71.0


def test_state_sleep_overwrites_only_more_recent():
    s = ReadinessState(calm_half_life_secs=1800.0)
    s.update_sleep(7.0, ts=1000.0)
    s.update_sleep(6.5, ts=500.0)   # older, ignored
    assert s.last_sleep_hours == 7.0
    s.update_sleep(8.0, ts=2000.0)
    assert s.last_sleep_hours == 8.0


# ── End-to-end compute_score ───────────────────────────────────────────


def test_compute_score_primed_morning():
    """Good sleep + plenty of flow + calm + workout = high score."""
    cfg = _cfg()
    score = compute_score(
        sleep_hours=7.7,
        flow_minutes_yesterday=130.0,
        avg_tension=0.20,
        last_workout_ts=time.time() - 5 * 3600.0,
        now=time.time(),
        cfg=cfg,
    )
    assert score.total >= 90.0
    assert score.bucket == "primed"


def test_compute_score_depleted_morning():
    """Poor sleep + no flow + high tension + no workout = low score."""
    cfg = _cfg()
    score = compute_score(
        sleep_hours=4.0,
        flow_minutes_yesterday=0.0,
        avg_tension=0.85,
        last_workout_ts=None,
        now=time.time(),
        cfg=cfg,
    )
    assert score.total <= 20.0
    assert score.bucket == "depleted"


def test_compute_score_components_sum_matches_total():
    cfg = _cfg()
    score = compute_score(
        sleep_hours=7.0,
        flow_minutes_yesterday=60.0,
        avg_tension=0.4,
        last_workout_ts=time.time() - 3600.0,
        now=time.time(),
        cfg=cfg,
    )
    summed = round(sum(c.score for c in score.components), 1)
    assert summed == score.total


def test_compute_score_no_data_neutral_bucket():
    """Empty state — should land in the middle, not in 'depleted'."""
    cfg = _cfg()
    score = compute_score(
        sleep_hours=None,
        flow_minutes_yesterday=0.0,
        avg_tension=None,
        last_workout_ts=None,
        now=time.time(),
        cfg=cfg,
    )
    # 20 (sleep half) + 0 (flow) + 7.5 (calm half) + 4.05 (activity) ≈ 31.6
    assert 28.0 <= score.total <= 36.0


# ── Singleton ──────────────────────────────────────────────────────────


def test_init_returns_same_instance(tmp_path):
    import ultron_readiness as ur
    ur._service = None
    cfg = _cfg(db_path=str(tmp_path / "r.db"))
    a = init(cfg)
    b = init(cfg)
    assert a is b
    assert get_service() is a
