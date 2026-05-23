"""Unit tests for the phonetic-variant wake-word matching.

We feed the listener through `_extract_query` directly so we don't need
to spin a real mic stream / Whisper. The variants live in `wake_word.py`
as module-level constants — these tests pin them so a future drop of
"ultraman" or accidental promotion of "a" as a hey-variant gets caught.
"""
from __future__ import annotations

import pytest

from ultron_voice.wake_word import (
    WakeWordListener,
    _build_phonetic_phrases,
    _normalise,
)


def _listener(wake_words: list[str] | None = None) -> WakeWordListener:
    """Build a listener with stub deps. Only methods that touch
    `self.wake_words` / `self._shutdown_norm` are exercised below."""
    return WakeWordListener(
        stt=None,                   # type: ignore[arg-type]
        vad=None,
        sample_rate=16000,
        segment_max_secs=3,
        silence_timeout_ms=2500,
        device=None,
        wake_words=wake_words or ["hey ultron", "hey altron"],
        on_wake_word=lambda q: None,  # type: ignore[arg-type]
        is_busy=lambda: False,
    )


# ── Phonetic phrase table ─────────────────────────────────────────────


def test_phonetic_phrases_are_hey_prefixed():
    phrases = _build_phonetic_phrases()
    # Every phrase MUST begin with a hey-variant so a bare "ultra" or
    # "ultron" buried in normal speech doesn't trigger.
    hey_words = {"hey", "hi", "yo", "ay", "hello"}
    for p in phrases:
        first = p.split(" ", 1)[0]
        assert first in hey_words, f"{p!r} has non-hey prefix"


def test_phonetic_phrases_exclude_bare_a():
    # "a ultra" must NOT be in the table — would trigger on innocent
    # speech like "so a ultra fan" under the 25-char prefix anchor.
    phrases = _build_phonetic_phrases()
    assert "a ultra" not in phrases
    assert "a ultron" not in phrases


def test_phonetic_phrases_sorted_longest_first():
    phrases = _build_phonetic_phrases()
    lengths = [len(p) for p in phrases]
    assert lengths == sorted(lengths, reverse=True), \
        "longest-first sort lets multi-word matches beat substrings"


def test_phonetic_phrases_include_common_mishearings():
    phrases = _build_phonetic_phrases()
    # Whisper's known greatest hits on "hey ultron":
    must_have = ["hey ultron", "hey altron", "hey ultra",
                 "hi ultron", "hello ultron"]
    for m in must_have:
        assert m in phrases, f"missing phonetic variant: {m!r}"


# ── _extract_query: positive cases ────────────────────────────────────


def test_extract_query_plain_hey_ultron():
    lst = _listener()
    assert lst._extract_query("Hey, Ultron.") == ""
    assert lst._extract_query("hey ultron") == ""


def test_extract_query_with_trailing_command():
    lst = _listener()
    assert lst._extract_query("Hey, Ultron, play music.") == "play music"
    assert lst._extract_query("hey ultron what's the weather") \
        == "what's the weather"


def test_extract_query_accepts_phonetic_variants():
    lst = _listener()
    # Each of these matches via the auto-injected phonetic table.
    assert lst._extract_query("Hey, Altron.") == ""
    assert lst._extract_query("hey ultra play music") == "play music"
    assert lst._extract_query("hi ultron remind me at 5") == "remind me at 5"
    assert lst._extract_query("yo altron open spotify") == "open spotify"


def test_extract_query_handles_punctuation_and_case():
    lst = _listener()
    assert lst._extract_query("HEY!!! ULTRON??? open the door.") \
        == "open the door"
    assert lst._extract_query("    hey   ultron   skip song   ") == "skip song"


# ── _extract_query: negative cases ────────────────────────────────────


def test_extract_query_rejects_buried_ultron():
    lst = _listener()
    # Wake must appear within the first 25 chars of the normalised text.
    # A phrase like "yesterday I was telling someone hey ultron was cool"
    # is narration about ULTRON, not a command, so it must not fire.
    long_lead = "Yesterday I was telling someone hey ultron was cool"
    assert lst._extract_query(long_lead) is None


def test_extract_query_rejects_bare_ultron_without_hey():
    lst = _listener()
    # "Ultron is cool" — no hey prefix, must not match.
    assert lst._extract_query("Ultron is cool") is None
    # And not when buried in a sentence either.
    assert lst._extract_query("I think ultron is cool") is None


def test_extract_query_rejects_a_ultra_fan_problem():
    # The bare "a" was deliberately excluded from hey-variants because
    # otherwise "so a ultra fan" would falsely fire under the prefix
    # anchor. Pin this so a future PR doesn't reintroduce the bug.
    lst = _listener()
    assert lst._extract_query("So a ultra fan called today") is None
    assert lst._extract_query("an ultra fan called today") is None


def test_extract_query_rejects_unrelated_speech():
    lst = _listener()
    assert lst._extract_query("How's the weather today") is None
    assert lst._extract_query("Hello world") is None  # "hello" alone, no ultron-variant
    assert lst._extract_query("Hey there, friend") is None


# ── Shutdown phrase still works ────────────────────────────────────────


def test_shutdown_phrase_detection_unchanged():
    lst = _listener()
    assert lst._is_shutdown_phrase("Bye Ultron")
    assert lst._is_shutdown_phrase("bye ultron")
    assert lst._is_shutdown_phrase("goodnight ultron")
    assert not lst._is_shutdown_phrase("hey ultron")
    assert not lst._is_shutdown_phrase("ultron is great")


# ── _normalise ─────────────────────────────────────────────────────────


def test_normalise_collapses_whitespace_and_punctuation():
    assert _normalise("Hey,   Ultron!!!") == "hey ultron"
    assert _normalise("  hello\tworld\n") == "hello world"
    assert _normalise("it's me") == "it's me"  # apostrophes survive
