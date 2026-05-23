"""Unit tests for the Windows-toast bridge.

We test the router + throttle logic and the XML escaping in the
notifier. The live notifier popping a real toast is exercised by the
smoke (smoke_toast.py).
"""
from __future__ import annotations

import time

import pytest

from ultron_toast import (
    ToastConfig,
    ToastRouter,
    ToastSpec,
    get_service,
    init,
)
from ultron_toast.notifier import _build_xml


def _cfg(**overrides) -> ToastConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        enabled=True,
        enable_wellness_nudge=True,
        enable_flow_break=True,
        enable_tuning_suggestion=True,
        enable_self_reflection=True,
        enable_readiness_bucket_change=True,
        enable_voice_shutdown=True,
        min_interval_wellness_nudge=300.0,
        min_interval_flow_break=60.0,
        min_interval_tuning_suggestion=1800.0,
        min_interval_self_reflection=3600.0,
        min_interval_readiness_change=600.0,
        min_interval_voice_shutdown=5.0,
        flow_break_min_minutes=5.0,
    )
    defaults.update(overrides)
    return ToastConfig(**defaults)


# ── XML escaping ───────────────────────────────────────────────────────


def test_build_xml_escapes_specials():
    xml = _build_xml("a & b", "<tag>", "it's & co.")
    # Less-than / ampersand must be entity-escaped so the XmlDocument
    # parser doesn't think they're markup.
    assert "&amp;" in xml
    assert "&lt;tag&gt;" in xml
    # Either '&apos;' or '&#39;' or saxutils' default (just &apos; not used);
    # we just need NO raw apostrophe that closes the surrounding tag string.
    # The here-string uses @'...'@ so apostrophe handling is up to xml only.
    # Validate at minimum that there are no raw "<tag>" sequences left.
    assert "<tag>" not in xml.replace("<text>", "").replace("</text>", "")


def test_build_xml_optional_footer_omitted():
    xml = _build_xml("title", "body")
    assert "placement='attribution'" not in xml


# ── Router: master switch ──────────────────────────────────────────────


def test_disabled_router_returns_none():
    cfg = _cfg(enabled=False)
    r = ToastRouter(cfg)
    out = r.route("wellness_nudge", {"kind": "low_sleep", "hours": 4.5,
                                      "target": 7.5}, now=1000.0)
    assert out is None


# ── Wellness nudge ─────────────────────────────────────────────────────


def test_wellness_low_sleep_produces_toast():
    cfg = _cfg()
    r = ToastRouter(cfg)
    out = r.route("wellness_nudge", {"kind": "low_sleep", "hours": 4.5,
                                      "target": 7.5}, now=1000.0)
    assert out is not None
    assert "Low sleep" in out.title
    assert "4.5" in out.body and "7.5" in out.body
    assert out.footer and "wellness" in out.footer


def test_wellness_streak_milestone_skipped():
    """Streak milestones are celebratory — voice handles them, no toast."""
    cfg = _cfg()
    r = ToastRouter(cfg)
    out = r.route("wellness_nudge",
                  {"kind": "streak_milestone", "habit": "workout",
                   "current": 7}, now=1000.0)
    assert out is None


def test_wellness_throttle():
    cfg = _cfg(min_interval_wellness_nudge=300.0)
    r = ToastRouter(cfg)
    # First fires.
    assert r.route("wellness_nudge", {"kind": "low_sleep", "hours": 4.5,
                                       "target": 7.5}, now=1000.0) is not None
    # Within window — suppressed.
    assert r.route("wellness_nudge", {"kind": "low_sleep", "hours": 4.6,
                                       "target": 7.5}, now=1100.0) is None
    # Past window — fires again.
    assert r.route("wellness_nudge", {"kind": "low_sleep", "hours": 4.7,
                                       "target": 7.5}, now=1400.0) is not None


# ── Flow break ─────────────────────────────────────────────────────────


def test_flow_break_below_floor_suppressed():
    cfg = _cfg(flow_break_min_minutes=5.0)
    r = ToastRouter(cfg)
    out = r.route("flow_state_changed",
                  {"prev_state": "active", "state": "broken",
                   "duration_minutes": 3.0,
                   "reason": "app_switch", "last_focus_app": "vscode"},
                  now=1000.0)
    assert out is None


def test_flow_break_substantial_session_fires():
    cfg = _cfg()
    r = ToastRouter(cfg)
    out = r.route("flow_state_changed",
                  {"prev_state": "active", "state": "broken",
                   "duration_minutes": 25.0,
                   "reason": "app_switch", "last_focus_app": "vscode"},
                  now=1000.0)
    assert out is not None
    assert out.title == "Flow ended"
    assert "25 min" in out.body
    assert "app_switch" in out.body
    assert "vscode" in out.body


def test_flow_non_break_transitions_skipped():
    cfg = _cfg()
    r = ToastRouter(cfg)
    # idle → entering and entering → active should NOT fire toasts.
    for prev, state in (("idle", "entering"), ("entering", "active"),
                        ("active", "idle")):
        out = r.route("flow_state_changed",
                      {"prev_state": prev, "state": state,
                       "duration_minutes": 30.0}, now=1000.0)
        assert out is None, f"unexpected toast for {prev}->{state}"


# ── Tuning suggestion ──────────────────────────────────────────────────


def test_tuning_suggestion_fires_once_per_window():
    cfg = _cfg(min_interval_tuning_suggestion=1800.0)
    r = ToastRouter(cfg)
    payload = {"title": "Tool X is failing",
                "rationale": "8 errors in 10 calls (80% failure rate)."}
    out = r.route("tuning_suggestion", payload, now=1000.0)
    assert out is not None
    assert "Tool X" in out.title
    # A second within the window is suppressed.
    out2 = r.route("tuning_suggestion", payload, now=1500.0)
    assert out2 is None


# ── Self-reflection ────────────────────────────────────────────────────


def test_self_reflection_fires():
    cfg = _cfg()
    r = ToastRouter(cfg)
    out = r.route("self_reflection_written",
                  {"date": "2026-05-23", "suggestion_count": 3,
                   "md_path": r"C:\fake.md", "latest_md_path": r"C:\latest.md"},
                  now=1000.0)
    assert out is not None
    assert "2026-05-23" in out.body
    assert "3 suggestions" in out.body


# ── Readiness bucket transition ────────────────────────────────────────


def test_readiness_no_toast_on_first_reading():
    cfg = _cfg()
    r = ToastRouter(cfg)
    # First reading just records the bucket — never toasts.
    out = r.route("readiness_score_update",
                  {"total": 75, "bucket": "ready"}, now=1000.0)
    assert out is None


def test_readiness_toasts_on_bucket_change():
    cfg = _cfg(min_interval_readiness_change=10.0)
    r = ToastRouter(cfg)
    # Prime: first reading.
    r.route("readiness_score_update",
            {"total": 75, "bucket": "ready"}, now=1000.0)
    # Same bucket — no toast.
    assert r.route("readiness_score_update",
                   {"total": 72, "bucket": "ready"},
                   now=1020.0) is None
    # Different bucket — toast.
    out = r.route("readiness_score_update",
                  {"total": 35, "bucket": "depleted"}, now=1100.0)
    assert out is not None
    assert "ready" in out.body and "depleted" in out.body


# ── Voice shutdown ─────────────────────────────────────────────────────


def test_voice_shutdown_fires():
    cfg = _cfg()
    r = ToastRouter(cfg)
    out = r.route("voice_shutdown_initiated", {}, now=1000.0)
    assert out is not None
    assert "shutting down" in out.title.lower()


# ── Per-kind toggle ────────────────────────────────────────────────────


def test_disabled_kind_returns_none():
    cfg = _cfg(enable_flow_break=False)
    r = ToastRouter(cfg)
    out = r.route("flow_state_changed",
                  {"prev_state": "active", "state": "broken",
                   "duration_minutes": 25.0,
                   "reason": "app_switch"}, now=1000.0)
    assert out is None


# ── Singleton ──────────────────────────────────────────────────────────


def test_init_returns_same_instance():
    import ultron_toast as ut
    ut._service = None
    a = init(_cfg())
    b = init(_cfg())
    assert a is b
    assert get_service() is a
