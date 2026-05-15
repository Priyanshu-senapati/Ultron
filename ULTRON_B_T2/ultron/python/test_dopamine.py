"""Tests for Module Y (Dopamine Marker)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ultron_dopamine.config import DopamineConfig
from ultron_dopamine.scorer import DopamineScorer
from ultron_dopamine.service import DopamineService
from ultron_dopamine.store import DopamineStore


def _cfg(tmp_path: Path) -> DopamineConfig:
    return DopamineConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "dopamine.db",
        ewma_alpha=0.5,           # bigger alpha for snappy test assertions
        drift_floor=-2.0,
        flow_ceiling=2.0,
        alert_cooldown_seconds=10,
    )


@pytest.fixture
def store(tmp_path: Path) -> DopamineStore:
    return DopamineStore(_cfg(tmp_path))


@pytest.fixture
def scorer(tmp_path: Path) -> DopamineScorer:
    return DopamineScorer(_cfg(tmp_path))


# ── Store ─────────────────────────────────────────────────────────────


def test_default_patterns_seeded(store: DopamineStore) -> None:
    names = {p["name"] for p in store.list_patterns()}
    assert "instagram_reels" in names
    assert "focus_dev" in names


def test_upsert_pattern_override(store: DopamineStore) -> None:
    store.upsert_pattern(name="instagram_reels", substring="instagram",
                         weight=-10, kind="wasteful")
    rows = {p["name"]: p for p in store.list_patterns()}
    assert rows["instagram_reels"]["weight"] == -10


def test_upsert_pattern_rejects_bad_kind(store: DopamineStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_pattern(name="x", substring="x", weight=1, kind="weird")


def test_record_mark_and_list(store: DopamineStore) -> None:
    mid = store.record_mark(
        ts=time.time(), pattern="instagram_reels",
        weight=-3, kind="wasteful", source="focus_app",
        context="Instagram – Reels",
    )
    assert mid > 0
    rows = store.list_marks()
    assert rows[0]["pattern"] == "instagram_reels"


def test_rollup_by_pattern(store: DopamineStore) -> None:
    base = time.time()
    for _ in range(3):
        store.record_mark(ts=base, pattern="instagram_reels", weight=-3, kind="wasteful")
    store.record_mark(ts=base, pattern="focus_dev", weight=+2, kind="rewarding")
    rows = store.rollup_by_pattern(since_ts=base - 60)
    by_name = {r["pattern"]: r for r in rows}
    assert by_name["instagram_reels"]["hits"] == 3
    assert by_name["instagram_reels"]["total_weight"] == -9


# ── Scorer ────────────────────────────────────────────────────────────


def test_scorer_matches_substring(scorer: DopamineScorer) -> None:
    pats = [{"name": "ig", "substring": "instagram", "weight": -3, "kind": "wasteful"}]
    matches = scorer.match("Brave - instagram.com/reels", pats)
    assert len(matches) == 1
    assert matches[0].pattern == "ig"
    assert matches[0].weight == -3


def test_scorer_case_insensitive(scorer: DopamineScorer) -> None:
    pats = [{"name": "ig", "substring": "instagram", "weight": -3, "kind": "wasteful"}]
    assert scorer.match("Visiting INSTAGRAM today", pats)


def test_scorer_multiple_patterns_same_text(scorer: DopamineScorer) -> None:
    pats = [
        {"name": "a", "substring": "reel", "weight": -2, "kind": "wasteful"},
        {"name": "b", "substring": "instagram", "weight": -3, "kind": "wasteful"},
    ]
    matches = scorer.match("instagram reels watcher", pats)
    assert {m.pattern for m in matches} == {"a", "b"}


def test_scorer_apply_moves_score_toward_weight(scorer: DopamineScorer) -> None:
    from ultron_dopamine.scorer import MatchResult
    # alpha=0.5 → one mark moves the score halfway toward its weight.
    scorer.apply([MatchResult(pattern="x", substring="x", weight=4, kind="rewarding")])
    assert scorer.score == pytest.approx(2.0, rel=0.01)
    # Negative mark pulls it back the other way.
    scorer.apply([MatchResult(pattern="y", substring="y", weight=-6, kind="wasteful")])
    # (1-0.5)*2 + 0.5*(-6) = 1 - 3 = -2
    assert scorer.score == pytest.approx(-2.0, rel=0.01)


def test_scorer_decay_pulls_toward_zero(scorer: DopamineScorer) -> None:
    from ultron_dopamine.scorer import MatchResult
    scorer.apply([MatchResult(pattern="x", substring="x", weight=10, kind="rewarding")])
    before = scorer.score
    scorer.decay(factor=0.5)
    assert scorer.score == pytest.approx(before * 0.5)


def test_scorer_reset(scorer: DopamineScorer) -> None:
    from ultron_dopamine.scorer import MatchResult
    scorer.apply([MatchResult(pattern="x", substring="x", weight=8, kind="rewarding")])
    scorer.reset()
    assert scorer.score == 0.0


# ── Service (in-process, no bridge) ───────────────────────────────────


@pytest.mark.asyncio
async def test_service_ingest_records_marks(tmp_path: Path) -> None:
    svc = DopamineService(_cfg(tmp_path))
    res = await svc.ingest_text("Brave - instagram.com - reels", source="focus_app")
    assert res["matches"] >= 1
    rows = svc.store.list_marks()
    assert any(r["pattern"] == "instagram_reels" for r in rows)


@pytest.mark.asyncio
async def test_service_ingest_no_match_returns_zero(tmp_path: Path) -> None:
    svc = DopamineService(_cfg(tmp_path))
    res = await svc.ingest_text("hello world", source="focus_app")
    assert res["matches"] == 0


@pytest.mark.asyncio
async def test_service_text_from_event_focus_app() -> None:
    text = DopamineService._text_from_event("focus_app", {"app": "Brave", "title": "Reels"})
    assert "Brave" in text
    assert "Reels" in text


@pytest.mark.asyncio
async def test_service_text_from_event_visual_label() -> None:
    text = DopamineService._text_from_event("visual_label", {"label": "instagram reels"})
    assert "instagram" in text


# ── Singleton ─────────────────────────────────────────────────────────


def test_dopamine_service_singleton(tmp_path: Path) -> None:
    from ultron_dopamine import get_service, init
    import ultron_dopamine
    ultron_dopamine._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_dopamine._service = None  # noqa: SLF001
