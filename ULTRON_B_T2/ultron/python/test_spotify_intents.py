"""Unit tests for the Spotify intent + data-answer changes.

Pure intent_router tests — no live bridge, no Ollama. Verifies:
  - "what's playing" / variants resolve to the now_playing data intent
    and format the reply from state.spotify correctly.
  - Extra media verbs (skip, back, fast forward, replay) route to
    media_control with the right action.
  - "play <song>" routes to spotify_control (Web API path).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ultron_llm.intent_router import route


def _state(**spotify_fields):
    """Build a minimal LiveState-shaped object with a spotify dict."""
    return SimpleNamespace(
        spotify=spotify_fields if spotify_fields else {},
        sysinfo={}, weather={}, stocks={}, news={},
    )


# ── now_playing data intent ────────────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "what's playing",
    "whats playing",
    "what song is this",
    "what song is playing",
    "what am i listening to",
    "now playing",
    "currently playing",
    "name of this song",
    "name of the song",
    "what is playing",
    "who's the artist",
    "who is the artist",
    "who sings this",
])
def test_now_playing_phrasings_match(phrase):
    state = _state(is_playing=True, track="Closer",
                   artist="The Chainsmokers")
    r = route(phrase, state)
    assert r is not None, f"no match for {phrase!r}"
    assert r.is_data_intent, f"{phrase!r} should be a data intent"
    assert "Closer" in r.reply
    assert "Chainsmokers" in r.reply


def test_now_playing_paused_track():
    state = _state(is_playing=False, track="Closer",
                   artist="The Chainsmokers")
    r = route("what's playing", state)
    assert r is not None and "Paused on" in r.reply


def test_now_playing_nothing_when_bridge_explicitly_empty():
    """Bridge said is_playing=False with no track (status 204 path)."""
    state = _state(is_playing=False)
    r = route("what's playing", state)
    assert r is not None
    assert "Nothing playing" in r.reply


def test_now_playing_falls_through_when_state_unknown():
    """No spotify state at all → fall through to LLM, not silent."""
    state = _state()
    r = route("what's playing", state)
    assert r is None   # fall through


# ── New media verbs ────────────────────────────────────────────────────


@pytest.mark.parametrize("phrase,expected_what", [
    ("skip", "next"),
    ("skip song", "next"),
    ("skip this", "next"),
    ("skip this song", "next"),
    ("skip ahead", "next"),
    ("go forward", "next"),
    ("forward", "next"),
    ("fast forward", "next"),
    ("next song", "next"),
    ("next one", "next"),
    ("back", "prev"),
    ("back one", "prev"),
    ("go back", "prev"),
    ("previous track", "prev"),
    ("previous one", "prev"),
    ("last song", "prev"),
    ("last track", "prev"),
    ("replay", "prev"),
])
def test_media_verb_routes_correctly(phrase, expected_what):
    state = _state()
    r = route(phrase, state)
    assert r is not None, f"{phrase!r} did not match"
    assert r.tool_name == "media_control"
    assert r.args.get("what") == expected_what


# ── play <song> routes to spotify_control ──────────────────────────────


def test_play_specific_song_routes_to_spotify_control():
    state = _state()
    r = route("play Closer by The Chainsmokers", state)
    assert r is not None
    assert r.tool_name == "spotify_control"
    assert r.args.get("action") == "play_query"
    assert "closer" in r.args.get("query", "").lower()


def test_play_song_on_spotify_still_routes_to_spotify_control():
    state = _state()
    r = route("play Yellow by Coldplay on Spotify", state)
    assert r is not None
    assert r.tool_name == "spotify_control"
    assert "yellow" in r.args.get("query", "").lower()


def test_generic_play_music_still_uses_media_key():
    """'play music' / 'play some music' must NOT search Spotify — it's
    just a resume-playback request."""
    state = _state()
    for q in ("play music", "play some music", "play a song", "play something"):
        r = route(q, state)
        assert r is not None, f"{q!r} did not match"
        assert r.tool_name == "media_control", f"{q!r} should be media_control"
        assert r.args.get("what") == "play_pause"


def test_bare_pause_still_works():
    state = _state()
    r = route("pause", state)
    assert r is not None and r.tool_name == "media_control"
    assert r.args.get("what") == "play_pause"
