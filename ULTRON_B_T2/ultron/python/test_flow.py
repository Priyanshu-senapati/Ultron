"""Tests for the Flow State Protector."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ultron_flow.config import FlowConfig
from ultron_flow.detector import FlowDetector, FlowSample, FlowState
from ultron_flow.store import FlowStore


def _cfg(tmp_path: Path) -> FlowConfig:
    return FlowConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "flow.db",
        samples_to_activate=2, samples_to_break=2,  # faster tests
    )


def _ok_sample(ts: float = 0.0) -> FlowSample:
    return FlowSample(
        ts=ts or time.time(),
        cognitive_load=0.55, tension=0.30,
        cadence_band="steady", focus_category="editor",
        app_switch_per_min=0.0, backspace_per_min=2.0,
        idle_secs=5.0, focus_app="vscode",
    )


# ── Eligibility ──────────────────────────────────────────────────────


def test_ineligible_when_tense(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(); s.tension = 0.80
    assert d.feed(s) is None
    assert d.state == FlowState.IDLE


def test_ineligible_when_idle_too_long(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(); s.idle_secs = 180.0
    assert d.feed(s) is None
    assert d.state == FlowState.IDLE


def test_ineligible_when_overloaded(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(); s.cognitive_load = 0.95
    assert d.feed(s) is None
    assert d.state == FlowState.IDLE


def test_ineligible_when_app_switching(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(); s.app_switch_per_min = 7.0
    assert d.feed(s) is None
    assert d.state == FlowState.IDLE


def test_ineligible_when_unproductive_app(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(); s.focus_category = "social"
    assert d.feed(s) is None
    assert d.state == FlowState.IDLE


# ── State transitions ────────────────────────────────────────────────


def test_idle_to_entering_to_active(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    t1 = d.feed(_ok_sample(1.0))
    assert t1 is not None and t1.to_state == FlowState.ENTERING
    assert d.state == FlowState.ENTERING
    t2 = d.feed(_ok_sample(6.0))
    assert t2 is not None and t2.to_state == FlowState.ACTIVE
    assert d.state == FlowState.ACTIVE


def test_entering_falls_back_to_idle_silently(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    d.feed(_ok_sample(1.0))  # ENTERING
    bad = _ok_sample(2.0); bad.tension = 0.80
    t = d.feed(bad)
    assert t is not None and t.to_state == FlowState.IDLE
    assert t.from_state == FlowState.ENTERING
    assert d.state == FlowState.IDLE


def test_active_breaks_on_sustained_violation(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    # Reach ACTIVE
    d.feed(_ok_sample(1.0))
    d.feed(_ok_sample(6.0))
    assert d.state == FlowState.ACTIVE
    bad = _ok_sample(11.0); bad.app_switch_per_min = 9.0
    assert d.feed(bad) is None  # first violation — held
    assert d.state == FlowState.ACTIVE
    bad2 = _ok_sample(16.0); bad2.app_switch_per_min = 9.0
    t = d.feed(bad2)
    assert t is not None and t.to_state == FlowState.BROKEN
    assert t.reason == "app_switch"
    assert t.duration_seconds >= 15.0
    # After break, detector returns to IDLE so a new flow can start.
    assert d.state == FlowState.IDLE


def test_active_single_blip_does_not_break(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    d.feed(_ok_sample(1.0)); d.feed(_ok_sample(6.0))
    assert d.state == FlowState.ACTIVE
    bad = _ok_sample(11.0); bad.tension = 0.80
    assert d.feed(bad) is None
    assert d.state == FlowState.ACTIVE
    # Good sample resets the violation counter.
    assert d.feed(_ok_sample(16.0)) is None
    assert d.state == FlowState.ACTIVE


def test_break_reason_reflects_first_violation_kind(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    d.feed(_ok_sample(1.0)); d.feed(_ok_sample(6.0))
    bad = _ok_sample(11.0); bad.backspace_per_min = 25.0
    d.feed(bad)
    bad2 = _ok_sample(16.0); bad2.backspace_per_min = 25.0
    t = d.feed(bad2)
    assert t is not None and t.reason == "backspace_burst"


def test_break_carries_last_focus_app(tmp_path: Path) -> None:
    d = FlowDetector(_cfg(tmp_path))
    s = _ok_sample(1.0); s.focus_app = "vscode"
    d.feed(s)
    s2 = _ok_sample(6.0); s2.focus_app = "vscode"
    d.feed(s2)
    bad = _ok_sample(11.0); bad.app_switch_per_min = 7.0; bad.focus_app = "brave"
    d.feed(bad)
    bad2 = _ok_sample(16.0); bad2.app_switch_per_min = 7.0; bad2.focus_app = "brave"
    t = d.feed(bad2)
    assert t is not None and t.last_focus_app == "vscode"


# ── Store ────────────────────────────────────────────────────────────


def test_store_record_and_recent(tmp_path: Path) -> None:
    store = FlowStore(_cfg(tmp_path))
    rid = store.record_session(
        start_ts=1000, end_ts=2200, broken_by="app_switch", last_focus_app="vscode",
    )
    assert rid > 0
    rows = store.recent(limit=5)
    assert rows[0]["duration_secs"] == 1200
    assert rows[0]["broken_by"] == "app_switch"


def test_store_stats_rollup(tmp_path: Path) -> None:
    store = FlowStore(_cfg(tmp_path))
    base = time.time()
    for i in range(4):
        store.record_session(
            start_ts=base + i * 600, end_ts=base + i * 600 + (i + 1) * 300,
            broken_by="app_switch" if i % 2 == 0 else "idle",
        )
    s = store.stats(since_ts=base - 1)
    assert s["sessions"] == 4
    assert s["total_minutes"] > 0
    by_reason = {r["reason"]: r["count"] for r in s["top_breakers"]}
    assert by_reason.get("app_switch") == 2
    assert by_reason.get("idle") == 2


# ── Singleton ────────────────────────────────────────────────────────


def test_flow_service_singleton(tmp_path: Path) -> None:
    from ultron_flow import get_service, init
    import ultron_flow
    ultron_flow._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_flow._service = None  # noqa: SLF001
