"""Unit tests for the Emotional Intelligence layer.

Pure detector + tracker tests — no live bus, no Ollama. The lexicon is
deterministic so we can assert exact expected directions.
"""
from __future__ import annotations

import time

import pytest

from ultron_emotion import (
    EmotionConfig,
    EmotionTracker,
    analyze,
    get_service,
    init,
)
from ultron_emotion.detector import EmotionSignal
from ultron_emotion.lexicon import LEXICON


def _cfg(**overrides) -> EmotionConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        half_life_secs=600.0,
        tension_corroboration_threshold=0.55,
        immediate_publish_frustration=0.6,
        min_change_for_publish=0.10,
        min_publish_interval_secs=2.0,
        inject_when_frustration_at_least=0.4,
        inject_when_negative_valence_at_most=-0.4,
        inject_when_positive_valence_at_least=0.6,
    )
    defaults.update(overrides)
    return EmotionConfig(**defaults)


# ── Lexicon sanity ─────────────────────────────────────────────────────


def test_lexicon_entries_are_tuples_of_three():
    for k, v in LEXICON.items():
        assert isinstance(v, tuple), k
        assert len(v) == 3, k
        dv, da, df = v
        assert -1.0 <= dv <= 1.0, k
        assert -1.0 <= da <= 1.0, k
        assert -1.0 <= df <= 1.0, k


# ── Detector: clear cases ──────────────────────────────────────────────


def test_detect_neutral_text_no_emotion():
    s = analyze("what time is it")
    assert s.valence == 0.0
    assert s.arousal == 0.0
    assert s.frustration == 0.0
    assert s.source == "neutral"


def test_detect_frustration_keyword():
    s = analyze("ugh this is broken again")
    assert s.frustration >= 0.7
    assert s.valence < 0
    assert s.source.startswith("lexicon")
    assert "ugh" in s.matched_phrases or "this is broken" in s.matched_phrases


def test_detect_strong_positive():
    s = analyze("perfect, that's amazing, thank you")
    assert s.valence >= 0.7
    assert s.arousal > 0
    assert s.frustration <= 0
    assert s.source == "lexicon"


def test_detect_low_energy_negative():
    s = analyze("I'm exhausted and stressed")
    assert s.valence < -0.3
    assert s.source == "lexicon"


def test_longest_phrase_wins_over_substring():
    """'not bad' must score positive, not get parsed as 'bad'."""
    s = analyze("yeah, not bad at all")
    assert s.valence > 0  # not negative — the longer 'not bad' phrase won


def test_unrelated_text_yields_neutral():
    s = analyze("the file is at /tmp/test.txt")
    assert s.source == "neutral"
    assert s.confidence == 0.0


# ── Tension cross-reference ────────────────────────────────────────────


def test_tension_boosts_frustration_when_text_negative():
    cfg = _cfg()
    base = analyze("this is annoying", tension=0.1, cfg=cfg)
    boosted = analyze("this is annoying", tension=0.8, cfg=cfg)
    assert boosted.frustration > base.frustration
    assert boosted.source == "lexicon+tension"
    assert boosted.confidence > base.confidence


def test_tension_alone_with_no_keywords_still_signals():
    """High tension with neutral text → some signal, low confidence."""
    s = analyze("ok", tension=0.85)
    # "ok" matches the lexicon (delta 0,0,0) so it's lexicon-source.
    # The behaviour we care about: tension-driven inference fires when
    # NO lexicon hit happens.
    s2 = analyze("the build runs", tension=0.85)
    assert s2.source == "tension_only"
    assert s2.arousal > 0.5
    assert s2.frustration > 0


def test_tension_low_negative_text_no_boost():
    cfg = _cfg(tension_corroboration_threshold=0.55)
    s = analyze("this is annoying", tension=0.3, cfg=cfg)
    assert s.source == "lexicon"
    # No boost — confidence is still moderate.
    assert s.frustration < 0.85


# ── Tracker EWMA + decay ───────────────────────────────────────────────


def test_tracker_blends_new_signal():
    t = EmotionTracker(half_life_secs=600.0)
    sig1 = analyze("ugh this is broken", ts=1000.0)
    t.apply(sig1)
    assert t.frustration > 0.5
    sig2 = analyze("ok thanks", ts=1010.0)
    t.apply(sig2)
    # Strong frustration shouldn't be wiped instantly; new positive
    # blends in but the prior persists.
    assert t.frustration > 0.3


def test_tracker_decays_over_long_silence():
    t = EmotionTracker(half_life_secs=60.0)
    sig = analyze("this is awful", ts=1000.0)
    t.apply(sig)
    high_v = t.valence
    # Half-life of 60s; after 120s = 2 half-lives → factor 0.25.
    t._decay_to_now(1120.0)
    assert abs(t.valence) < abs(high_v) * 0.5


def test_tracker_mood_label_categorisation():
    t = EmotionTracker(half_life_secs=600.0)
    t.frustration = 0.7
    assert t.mood_label() == "frustrated"
    t.frustration = 0.0
    t.valence = -0.6
    assert t.mood_label() == "low"
    t.valence = 0.6
    t.arousal = 0.5
    assert t.mood_label() == "energised"
    t.valence = 0.5
    t.arousal = 0.1
    assert t.mood_label() == "positive"
    t.valence = 0.0
    t.arousal = 0.0
    assert t.mood_label() in ("calm", "neutral")


def test_tracker_is_significant_change():
    t = EmotionTracker()
    t.valence = 0.5
    t.arousal = 0.0
    t.frustration = 0.0
    prior = {"valence": 0.0, "arousal": 0.0, "frustration": 0.0}
    assert t.is_significant_change(prior, 0.1) is True
    prior = {"valence": 0.49, "arousal": 0.0, "frustration": 0.0}
    assert t.is_significant_change(prior, 0.1) is False


def test_tracker_first_publish_always_significant():
    t = EmotionTracker()
    t.valence = 0.0
    t.arousal = 0.0
    t.frustration = 0.0
    assert t.is_significant_change({}, 0.1) is True


# ── Singleton init ─────────────────────────────────────────────────────


def test_init_returns_same_instance():
    import ultron_emotion as ue
    ue._service = None
    a = init(_cfg())
    b = init(_cfg())
    assert a is b
    assert get_service() is a
