"""Unit tests for the self-improvement / self-tuner module.

We test the pure pieces directly — observers, suggester, markdown
renderer. No live bus, no SQLite reads. The reflector's DB readers are
exercised by the live smoke.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ultron_selftuner import (
    EmotionObserver,
    SelfTunerConfig,
    ToolUsageObserver,
    get_service,
    init,
    render_markdown,
    suggest,
)


def _cfg(tmp_path: Path, **overrides) -> SelfTunerConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        flow_db=tmp_path / "flow.db",
        interrupt_db=tmp_path / "interrupts.db",
        readiness_db=tmp_path / "readiness.db",
        recall_db=tmp_path / "recall.db",
        reflection_dir=tmp_path / "self_reflections",
        latest_md_path=tmp_path / "self_reflections" / "latest.md",
        reflection_interval_secs=86400.0,
        boot_delay_secs=0.0,
        tool_usage_window_secs=86400.0,
        tool_error_rate_min_calls=5,
        tool_error_rate_alert=0.20,
        long_session_min_minutes=25.0,
        short_session_max_minutes=5.0,
        interrupt_source_majority=0.40,
        sleep_undercounted_floor=4,
        recall_miss_min_count=3,
    )
    defaults.update(overrides)
    return SelfTunerConfig(**defaults)


# ── ToolUsageObserver ──────────────────────────────────────────────────


def test_tool_observer_counts_ok_and_errors():
    obs = ToolUsageObserver(window_secs=86400.0)
    for _ in range(7):
        obs.record("recall", ok=True, ts=1000.0)
    for _ in range(3):
        obs.record("recall", ok=False, error_reason="timeout", ts=1001.0)
    stats = obs.stats(now=1010.0)
    assert "recall" in stats
    r = stats["recall"]
    assert r["n"] == 10
    assert r["ok"] == 7
    assert r["errors"] == 3
    assert r["ok_rate"] == 0.7
    assert r["last_error_reason"] == "timeout"


def test_tool_observer_prunes_old_entries():
    obs = ToolUsageObserver(window_secs=100.0)
    obs.record("old_tool", ok=True, ts=1000.0)
    obs.record("new_tool", ok=True, ts=2000.0)
    stats = obs.stats(now=2050.0)
    # old_tool fell off the window (gap = 1050s, window = 100s).
    assert "old_tool" not in stats
    assert "new_tool" in stats


def test_tool_observer_last_error_reason_is_most_recent():
    obs = ToolUsageObserver(window_secs=86400.0)
    obs.record("x", ok=False, error_reason="first", ts=1000.0)
    obs.record("x", ok=True, ts=1001.0)
    obs.record("x", ok=False, error_reason="second", ts=1002.0)
    obs.record("x", ok=True, ts=1003.0)
    stats = obs.stats(now=1010.0)
    assert stats["x"]["last_error_reason"] == "second"


# ── EmotionObserver ────────────────────────────────────────────────────


def test_emotion_observer_histogram_and_averages():
    obs = EmotionObserver(window_secs=86400.0)
    obs.record({"mood_label": "frustrated", "valence": -0.6,
                "arousal": 0.5, "frustration": 0.7}, ts=1000.0)
    obs.record({"mood_label": "frustrated", "valence": -0.5,
                "arousal": 0.4, "frustration": 0.6}, ts=1001.0)
    obs.record({"mood_label": "energised", "valence": 0.6,
                "arousal": 0.6, "frustration": 0.1}, ts=1002.0)
    hist = obs.histogram()
    assert hist["frustrated"] == 2
    assert hist["energised"] == 1
    avgs = obs.averages()
    assert avgs["samples"] == 3
    assert avgs["valence"] == pytest.approx(-0.5 / 3 + 0.6 / 3 - 0.6 / 3,
                                            abs=0.01)


def test_emotion_observer_peak_frustration_threshold():
    obs = EmotionObserver(window_secs=86400.0)
    # All samples below threshold → no peak surfaced.
    for f in (0.1, 0.2, 0.3):
        obs.record({"mood_label": "neutral", "valence": 0.0,
                    "arousal": 0.2, "frustration": f}, ts=1000.0)
    assert obs.peak_frustration() is None
    # Add one above threshold.
    obs.record({"mood_label": "frustrated", "valence": -0.5,
                "arousal": 0.5, "frustration": 0.8}, ts=1003.0)
    peak = obs.peak_frustration()
    assert peak is not None
    assert peak["frustration"] == pytest.approx(0.8)


# ── Suggester ──────────────────────────────────────────────────────────


def _empty_facts(now: float = 1000.0) -> dict:
    return {
        "now": now,
        "since": now - 3600,
        "flow": {"sessions": 0},
        "interrupts": {"count": 0},
        "readiness": {"samples": 0},
        "recall": {"turns_today": 0},
        "tools": {},
        "emotion": {"averages": {"samples": 0,
                                  "valence": 0.0,
                                  "arousal": 0.0,
                                  "frustration": 0.0},
                    "histogram": {}, "peak_frustration": None},
    }


def test_suggest_flags_flaky_tool(tmp_path):
    cfg = _cfg(tmp_path, tool_error_rate_min_calls=5,
               tool_error_rate_alert=0.20)
    facts = _empty_facts()
    facts["tools"] = {
        "recall": {"n": 10, "ok": 7, "errors": 3, "ok_rate": 0.7,
                   "last_error_reason": "timeout"},
    }
    out = suggest(facts, cfg)
    titles = [s["title"] for s in out]
    assert any("recall" in t and "failing" in t for t in titles)


def test_suggest_quiet_when_tool_works(tmp_path):
    cfg = _cfg(tmp_path)
    facts = _empty_facts()
    facts["tools"] = {
        "recall": {"n": 50, "ok": 50, "errors": 0, "ok_rate": 1.0,
                   "last_error_reason": ""},
    }
    assert not [s for s in suggest(facts, cfg)
                if "failing" in s["title"]]


def test_suggest_flags_dominant_flow_breaker(tmp_path):
    cfg = _cfg(tmp_path)
    facts = _empty_facts()
    facts["flow"] = {
        "sessions": 5,
        "total_minutes": 60,
        "avg_minutes": 12,
        "longest_minutes": 25,
        "top_breakers": [("app_switch", 4), ("idle", 1)],
        "top_apps": [],
    }
    out = suggest(facts, cfg)
    assert any("app_switch" in s["title"] for s in out)


def test_suggest_flags_short_sessions(tmp_path):
    cfg = _cfg(tmp_path, short_session_max_minutes=5.0)
    facts = _empty_facts()
    facts["flow"] = {"sessions": 6, "total_minutes": 18.0,
                      "avg_minutes": 3.0, "longest_minutes": 5,
                      "top_breakers": [], "top_apps": []}
    out = suggest(facts, cfg)
    assert any("short" in s["title"].lower() for s in out)


def test_suggest_flags_interrupt_source_majority(tmp_path):
    cfg = _cfg(tmp_path, interrupt_source_majority=0.40)
    facts = _empty_facts()
    facts["interrupts"] = {
        "count": 10,
        "by_source": [("wake_word", 6), ("flow_break", 2),
                      ("wellness_nudge", 2)],
        "avg_recovery_secs": 200,
    }
    out = suggest(facts, cfg)
    assert any("wake_word" in s["title"] for s in out)


def test_suggest_flags_high_baseline_frustration(tmp_path):
    cfg = _cfg(tmp_path)
    facts = _empty_facts()
    facts["emotion"]["averages"] = {"valence": -0.3, "arousal": 0.5,
                                     "frustration": 0.5, "samples": 8}
    out = suggest(facts, cfg)
    assert any("Frustration" in s["title"] for s in out)


def test_suggest_celebrates_facts_growth(tmp_path):
    cfg = _cfg(tmp_path)
    facts = _empty_facts()
    facts["recall"] = {"turns_today": 30, "reflections_today": 1,
                       "facts_today": 5}
    out = suggest(facts, cfg)
    assert any("facts learned" in s["title"].lower() for s in out)


def test_suggest_quiet_day(tmp_path):
    """Empty / neutral day should produce zero suggestions."""
    cfg = _cfg(tmp_path)
    assert suggest(_empty_facts(), cfg) == []


# ── Markdown rendering ─────────────────────────────────────────────────


def test_render_markdown_includes_all_sections(tmp_path):
    cfg = _cfg(tmp_path)
    facts = _empty_facts(now=time.time())
    facts["flow"] = {"sessions": 3, "total_minutes": 90, "avg_minutes": 30,
                     "longest_minutes": 45,
                     "top_breakers": [("app_switch", 2)],
                     "top_apps": [("vscode", 3)]}
    facts["interrupts"] = {"count": 5,
                            "by_source": [("wake_word", 3),
                                          ("wellness_nudge", 2)],
                            "avg_recovery_secs": 180}
    facts["readiness"] = {"samples": 4, "latest": 72, "avg": 65,
                           "min": 50, "max": 80, "buckets": ["ready"]}
    facts["recall"] = {"turns_today": 12, "reflections_today": 1,
                       "facts_today": 2}
    facts["tools"] = {"recall": {"n": 5, "ok": 5, "errors": 0,
                                  "ok_rate": 1.0, "last_error_reason": ""}}
    facts["emotion"]["averages"] = {"valence": 0.2, "arousal": 0.3,
                                     "frustration": 0.1, "samples": 10}
    facts["emotion"]["histogram"] = {"neutral": 7, "positive": 3}
    suggestions = suggest(facts, cfg)
    md = render_markdown(facts, suggestions)
    for header in ("# ULTRON Self-Reflection",
                   "## Flow", "## Interrupts", "## Readiness",
                   "## Memory growth", "## Tools", "## Emotion",
                   "## Tuning suggestions"):
        assert header in md
    assert "90.0 min" in md
    assert "wake_word" in md


def test_render_markdown_empty_day(tmp_path):
    """Empty day still produces well-formed markdown with placeholders."""
    cfg = _cfg(tmp_path)
    md = render_markdown(_empty_facts(now=time.time()), [])
    assert "No completed flow sessions" in md
    assert "No interrupts logged" in md
    assert "Nothing to tune" in md


# ── Singleton ──────────────────────────────────────────────────────────


def test_init_returns_same_instance(tmp_path):
    import ultron_selftuner as us
    us._service = None
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    assert a is b
    assert get_service() is a
