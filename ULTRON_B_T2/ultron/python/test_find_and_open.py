"""Unit tests for find_and_open + the rerouted web-search intents.

The ranking function is pure — we test it directly with synthetic
result rows. Intent-router tests confirm "search X" / "find X" / "look
up X" now hit find_and_open, while "google X" / "X on youtube" keep
opening the search results page.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ultron_llm.intent_router import route
from ultron_tools.builtin.find_and_open import (
    _host_of,
    _score,
    _site_hint_for,
)


def _state():
    return SimpleNamespace(spotify={}, sysinfo={}, weather={},
                            stocks={}, news={})


# ── _site_hint_for ─────────────────────────────────────────────────────


@pytest.mark.parametrize("query, expected", [
    ("wikipedia python decorators", "wikipedia.org"),
    ("github faster-whisper",       "github.com"),
    ("stackoverflow asyncio gather","stackoverflow.com"),
    ("anthropic prompt caching",    "anthropic.com"),
    ("youtube tiny desk concerts",  "youtube.com"),
    ("imdb dune part two",          "imdb.com"),
    ("recursion in scheme",         None),
])
def test_site_hint_for(query, expected):
    assert _site_hint_for(query) == expected


def test_longest_hint_wins():
    # "stack overflow" should beat "stack" if both were keys (only
    # "stack overflow" is). Mostly a no-regress test for the
    # iteration order in the impl.
    assert _site_hint_for("ask on stack overflow") == "stackoverflow.com"


# ── _score (ranking) ───────────────────────────────────────────────────


def _r(url: str, title: str = "") -> dict[str, str]:
    return {"url": url, "title": title, "snippet": ""}


def test_score_first_result_wins_by_default():
    q = "python decorators"
    results = [
        _r("https://realpython.com/primer-on-python-decorators/", "Primer"),
        _r("https://wiki.python.org/moin/PythonDecorators", "wiki"),
    ]
    scored = [(_score(r, i, q, None), r) for i, r in enumerate(results)]
    scored.sort(key=lambda kv: kv[0])
    assert scored[0][1]["url"].startswith("https://realpython.com")


def test_score_site_hint_boosts_matching_domain():
    q = "wikipedia python decorators"
    site_hint = _site_hint_for(q)
    assert site_hint == "wikipedia.org"
    results = [
        # The DDG #1 result is realpython (no site hint match).
        _r("https://realpython.com/primer-on-python-decorators/", "Primer"),
        # The DDG #4 result is wikipedia (should win due to hint).
        _r("https://example.com/x", "X"),
        _r("https://example.com/y", "Y"),
        _r("https://en.wikipedia.org/wiki/Python_syntax", "Wikipedia"),
    ]
    scored = sorted(enumerate(results),
                    key=lambda kv: _score(kv[1], kv[0], q, site_hint))
    assert "wikipedia.org" in scored[0][1]["url"]


def test_score_penalises_junk_hosts():
    """Pinterest / wikihow / similar should NEVER outrank a real result."""
    q = "python tutorial"
    results = [
        # Pinterest at DDG #1 must lose to a real result.
        _r("https://www.pinterest.com/explore/python-tutorial/",
           "Python tutorial"),
        _r("https://docs.python.org/3/tutorial/", "Python tutorial — Official"),
    ]
    scored = sorted(enumerate(results),
                    key=lambda kv: _score(kv[1], kv[0], q, None))
    assert "docs.python.org" in scored[0][1]["url"]
    assert "pinterest" not in scored[0][1]["url"]


def test_score_preferred_docs_host_gets_small_bump():
    q = "asyncio gather"
    results = [
        _r("https://example.com/article", "asyncio gather"),
        _r("https://docs.python.org/3/library/asyncio.html",
           "asyncio gather"),
    ]
    # example.com is at rank 0 (DDG #1), docs.python.org at rank 1.
    # Both have title overlap = 2. The preferred-host bump (-5) should
    # let docs.python.org overcome the 1-rank gap.
    scored = sorted(enumerate(results),
                    key=lambda kv: _score(kv[1], kv[0], q, None))
    assert "docs.python.org" in scored[0][1]["url"]


def test_score_title_overlap_breaks_ties():
    q = "asyncio gather example"
    results = [
        _r("https://example.com/a", "unrelated random article"),
        _r("https://example.com/b", "asyncio gather example notes"),
    ]
    scored = sorted(enumerate(results),
                    key=lambda kv: _score(kv[1], kv[0], q, None))
    # Title-overlap nudge is small (0.1 each) but enough to win when
    # DDG-rank delta is 1. example.com/b is at rank 1 (worse) but has
    # 3 word overlap → score = 1 - 0.3 = 0.7 vs a at score 0.
    # So actually a wins here — keep this as the no-regress baseline.
    assert scored[0][1]["url"].endswith("/a")
    # But b should rank above a if both are at the same DDG position.
    a_score = _score(results[0], 0, q, None)
    b_score = _score(results[1], 0, q, None)
    assert b_score < a_score


# ── _host_of ───────────────────────────────────────────────────────────


def test_host_of_handles_subdomain():
    assert _host_of("https://docs.python.org/3/tutorial/") == "docs.python.org"


def test_host_of_handles_bad_url():
    assert _host_of("not a url") == ""


# ── Intent router routing ──────────────────────────────────────────────


def test_find_routes_to_find_and_open():
    r = route("find me the python decorators wikipedia page", _state())
    assert r is not None
    assert r.tool_name == "find_and_open"


def test_look_up_routes_to_find_and_open():
    r = route("look up faster whisper", _state())
    assert r is not None
    assert r.tool_name == "find_and_open"


def test_search_X_routes_to_find_and_open():
    r = route("search for the anthropic prompt caching docs", _state())
    assert r is not None
    assert r.tool_name == "find_and_open"


def test_google_X_keeps_search_page():
    """Carve-out: 'google X' must still open the Google SERP, not
    auto-jump to a result."""
    r = route("google asyncio gather", _state())
    assert r is not None
    assert r.tool_name == "web_open"
    assert r.args.get("query") == "asyncio gather"


def test_search_X_on_youtube_keeps_site_search():
    """Carve-out: 'on youtube' user wants the YouTube SERP."""
    r = route("search tiny desk concerts on youtube", _state())
    assert r is not None
    assert r.tool_name == "web_open"
    assert r.args.get("site") == "youtube.com"


def test_search_via_chrome_passes_browser():
    r = route("search faster whisper on chrome", _state())
    assert r is not None
    assert r.tool_name == "find_and_open"
    assert r.args.get("browser") == "chrome"
