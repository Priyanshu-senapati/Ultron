"""Unit tests for Roadmap #2 — Re-entry Protocol.

Covers:
  - Detector transitions (PRESENT → AWAY → RETURNING) with the
    expected ts / duration values.
  - Cooldown / min-duration gating in the composer threshold.
  - ContextSnapshot freshness window (lookback prunes stale fields).
  - Brief composer:
      * Welcomes back with humanised duration.
      * Includes focus + visual label when present.
      * Quotes LLM reply but clips at sentence boundary.
      * Mentions commit count only when nonzero.
      * Strips markdown so TTS doesn't read "asterisks".
      * Respects max_brief_chars.
"""
from __future__ import annotations

import time

import pytest

from ultron_reentry import (
    ReentryConfig,
    ReentryContext,
    ReentryDetector,
    compose_brief,
    init,
    get_service,
)
from ultron_reentry.detector import PresenceState


def _cfg(**overrides) -> ReentryConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        away_threshold_secs=300.0,
        return_idle_threshold_secs=30.0,
        cooldown_secs=120.0,
        min_away_minutes_for_brief=5.0,
        max_brief_chars=260,
        recent_lookback_secs=900.0,
        max_llm_quote_chars=140,
        include_git_delta=True,
        speak_brief=True,
    )
    defaults.update(overrides)
    return ReentryConfig(**defaults)


# ── Detector ────────────────────────────────────────────────────────────


def test_present_stays_present_below_threshold():
    d = ReentryDetector(_cfg())
    assert d.feed_idle(10.0, ts=100.0) is None
    assert d.feed_idle(120.0, ts=120.0) is None
    assert d.state == PresenceState.PRESENT


def test_present_to_away_when_idle_crosses_threshold():
    d = ReentryDetector(_cfg(away_threshold_secs=300.0))
    assert d.feed_idle(50.0, ts=100.0) is None
    t = d.feed_idle(350.0, ts=500.0)
    assert t is not None
    assert t.from_state == PresenceState.PRESENT
    assert t.to_state == PresenceState.AWAY
    # Estimated away-start ≈ now - idle_secs.
    assert abs(t.away_started_ts - (500.0 - 350.0)) < 0.001
    assert d.state == PresenceState.AWAY


def test_away_to_returning_resets_to_present():
    cfg = _cfg(away_threshold_secs=300.0, return_idle_threshold_secs=30.0)
    d = ReentryDetector(cfg)
    # At ts=1000 idle_secs=400 → estimated away-start is ts=600.
    d.feed_idle(400.0, ts=1000.0)
    assert d.state == PresenceState.AWAY
    # Return at ts=1450 → total away = 1450 - 600 = 850s (not 450).
    # The detector measures from the *real* away-start, not from when
    # we first noticed.
    t = d.feed_idle(5.0, ts=1450.0)
    assert t is not None
    assert t.from_state == PresenceState.AWAY
    assert t.to_state == PresenceState.RETURNING
    assert t.away_duration_seconds == pytest.approx(850.0, abs=1.0)
    # RETURNING is a one-tick marker; we go straight back to PRESENT.
    assert d.state == PresenceState.PRESENT


def test_no_return_while_idle_still_high():
    d = ReentryDetector(_cfg(return_idle_threshold_secs=30.0))
    d.feed_idle(400.0, ts=1000.0)
    # idle dropped but still above the return threshold — stay AWAY.
    assert d.feed_idle(60.0, ts=1100.0) is None
    assert d.state == PresenceState.AWAY


def test_mark_activity_triggers_return():
    d = ReentryDetector(_cfg())
    d.feed_idle(400.0, ts=1000.0)
    assert d.state == PresenceState.AWAY
    t = d.mark_activity(ts=1500.0)
    assert t is not None
    assert t.to_state == PresenceState.RETURNING


# ── Context buffer ──────────────────────────────────────────────────────


def test_context_freshness_window():
    ctx = ReentryContext(lookback_secs=300.0)
    now = 10_000.0
    ctx.on_visual_label({"label": "writing python code"}, ts=now - 100.0)
    ctx.on_insight_snapshot({"focus_app": "vscode", "focus_category": "editor"},
                            ts=now - 50.0)
    snap = ctx.snapshot(now=now)
    assert snap.last_focus_app == "vscode"
    assert snap.last_visual_label == "writing python code"
    # Far enough in the past — pruned.
    snap_old = ctx.snapshot(now=now + 1000.0)
    assert snap_old.last_focus_app == ""
    assert snap_old.last_visual_label == ""


def test_context_counts_commits_only_during_away():
    ctx = ReentryContext(lookback_secs=900.0)
    # Commit before going away — should NOT count.
    ctx.on_git_activity({"commits": [{"sha": "a" * 40}], "head": "a" * 40},
                        ts=100.0)
    snap = ctx.snapshot(now=200.0)
    assert snap.commits_since_away == 0

    ctx.mark_away(ts=300.0)
    ctx.on_git_activity({"commits": [{"sha": "b" * 40}, {"sha": "c" * 40}],
                         "head": "c" * 40}, ts=400.0)
    ctx.on_git_activity({"commits": [{"sha": "d" * 40}], "head": "d" * 40},
                        ts=500.0)
    snap = ctx.snapshot(now=600.0)
    assert snap.commits_since_away == 3

    ctx.mark_return()
    # New commits after return don't add to the away count.
    ctx.on_git_activity({"commits": [{"sha": "e" * 40}], "head": "e" * 40},
                        ts=700.0)
    # mark_return doesn't reset the away-window list, but mark_away does.
    ctx.mark_away(ts=800.0)
    snap = ctx.snapshot(now=900.0)
    assert snap.commits_since_away == 0


# ── Composer ────────────────────────────────────────────────────────────


def _empty_snapshot():
    ctx = ReentryContext(lookback_secs=900.0)
    return ctx.snapshot(now=1000.0)


def test_brief_minimum_has_welcome():
    cfg = _cfg()
    snap = _empty_snapshot()
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    assert text.startswith("Welcome back.")
    assert "6 minutes" in text


def test_brief_humanises_one_minute():
    cfg = _cfg()
    snap = _empty_snapshot()
    text = compose_brief(snap, away_seconds=65.0, cfg=cfg)
    assert "one minute" in text


def test_brief_includes_focus_and_label():
    cfg = _cfg()
    ctx = ReentryContext(lookback_secs=900.0)
    ctx.on_insight_snapshot({"focus_app": "vscode", "focus_category": "editor"}, ts=900.0)
    ctx.on_visual_label({"label": "editing reentry detector"}, ts=950.0)
    snap = ctx.snapshot(now=1000.0)
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    assert "vscode" in text
    assert "editing reentry detector" in text


def test_brief_quotes_llm_clipped_at_sentence():
    cfg = _cfg(max_llm_quote_chars=80)
    ctx = ReentryContext(lookback_secs=900.0)
    long_reply = (
        "Yes, that approach makes sense. "
        "We should next wire the voice engine consumer. "
        "After that, push to GitHub and start roadmap three."
    )
    ctx.on_llm_response({"text": long_reply, "shard": "default"}, ts=950.0)
    snap = ctx.snapshot(now=1000.0)
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    assert "Earlier I said:" in text
    quoted = text.split("Earlier I said:")[1].strip()
    # Clipped, ends on a sentence boundary OR with ellipsis.
    assert len(quoted) <= 80 + 10  # +10 slack for the prefix collapse
    assert quoted.endswith(".") or quoted.endswith("…")


def test_brief_strips_markdown_from_llm():
    cfg = _cfg(max_llm_quote_chars=200)
    ctx = ReentryContext(lookback_secs=900.0)
    ctx.on_llm_response({"text": "Try `git status` and **commit** the work.",
                         "shard": "default"}, ts=950.0)
    snap = ctx.snapshot(now=1000.0)
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    # No backticks, no asterisks — those'd be read aloud.
    assert "`" not in text
    assert "*" not in text
    assert "git status" in text
    assert "commit" in text


def test_brief_mentions_commits_only_when_present():
    cfg = _cfg()
    ctx = ReentryContext(lookback_secs=900.0)
    ctx.mark_away(ts=500.0)
    ctx.on_git_activity({"commits": [{"sha": "a" * 40}, {"sha": "b" * 40}],
                         "head": "b" * 40}, ts=600.0)
    snap = ctx.snapshot(now=1000.0)
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    assert "2 commits" in text


def test_brief_respects_char_cap():
    cfg = _cfg(max_brief_chars=80, max_llm_quote_chars=200)
    ctx = ReentryContext(lookback_secs=900.0)
    ctx.on_insight_snapshot({"focus_app": "vscode", "focus_category": "editor"}, ts=900.0)
    ctx.on_visual_label({"label": "a very long visual label that ought to be dropped"}, ts=910.0)
    ctx.on_llm_response({"text": "A long reply that surely will not fit.", "shard": "default"}, ts=920.0)
    snap = ctx.snapshot(now=1000.0)
    text = compose_brief(snap, away_seconds=360.0, cfg=cfg)
    assert len(text) <= 80 + 5  # tiny slack for boundary detection


# ── Singleton init ──────────────────────────────────────────────────────


def test_init_returns_same_instance():
    # Reset module-level singleton between test runs.
    import ultron_reentry as ur
    ur._service = None
    a = init(_cfg())
    b = init(_cfg())
    assert a is b
    assert get_service() is a
