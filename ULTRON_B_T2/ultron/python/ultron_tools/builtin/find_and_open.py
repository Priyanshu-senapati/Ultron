"""find_and_open — search the web and open the BEST result in the
default browser. One-shot version of (web_search → web_open).

Why:
  ``web_open`` with a ``query`` argument builds a Google search URL —
  i.e. it opens the SEARCH PAGE, not the actual result. That forces the
  user to click before they get to the answer. ``find_and_open`` does
  the search server-side, scores the hits, and launches the top URL
  directly so the user lands on the destination page.

Ranking:
  Defaults to "first DDG result wins" because DDG already ranks
  reasonably. We bump matching-domain results when the query mentions
  a site (``"wikipedia python decorators"`` → prefer wikipedia.org),
  and penalise low-signal aggregators we'd never recommend
  (pinterest, w3schools-style spam, "answers.com").
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.find_and_open")


# Site hint → preferred domain. Triggered when the query mentions the
# bare keyword anywhere (case-insensitive). Bumps results whose host
# ends with the preferred domain.
_SITE_HINTS: dict[str, str] = {
    "wikipedia": "wikipedia.org",
    "wiki": "wikipedia.org",
    "youtube": "youtube.com",
    "yt": "youtube.com",
    "github": "github.com",
    "stackoverflow": "stackoverflow.com",
    "stack overflow": "stackoverflow.com",
    "reddit": "reddit.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "amazon": "amazon.in",
    "imdb": "imdb.com",
    "spotify": "open.spotify.com",
    "docs": "",     # ambiguous — bumps any host containing "docs" via _docs_bump
    "documentation": "",
    "anthropic": "anthropic.com",
    "openai": "openai.com",
}

# Domains we de-prioritise hard — they show up high in DDG but rarely
# match what an experienced user actually wants.
_PENALISED_HOSTS = {
    "pinterest.com", "pinterest.in",
    "answers.com",
    "ehow.com",
    "wikihow.com",      # often outranks more specific docs
    "yahoo.com",
    "ask.com",
    "geeksforgeeks.org",  # SEO-heavy, often outranks official docs
}

# Domains we lightly prefer — typically the authoritative source for
# what the user is searching for. The bump is small (-5) so DDG
# top-ranked beats a mid-ranked preferred host only by a hair.
_PREFERRED_HOSTS = {
    "docs.python.org",
    "developer.mozilla.org",
    "docs.anthropic.com",
    "platform.openai.com",
    "github.com",
    "docs.rust-lang.org",
    "doc.rust-lang.org",
    "kubernetes.io",
    "stackoverflow.com",
}


_BROWSER_EXE: dict[str, list[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "edge": [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
}


def _find_browser(name: str) -> Optional[str]:
    name = (name or "").strip().lower()
    if not name or name not in _BROWSER_EXE:
        return None
    for p in _BROWSER_EXE[name]:
        if Path(p).exists():
            return p
    return None


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _site_hint_for(query: str) -> Optional[str]:
    q = (query or "").lower()
    # Longest hint first so "stack overflow" wins over "stack".
    for hint in sorted(_SITE_HINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(hint)}\b", q):
            domain = _SITE_HINTS[hint]
            return domain or hint   # empty domain → bump-by-substring
    return None


def _score(result: dict[str, str], idx: int,
           query: str, site_hint: Optional[str]) -> float:
    """Lower is better — used to sort ascending."""
    host = _host_of(result.get("url", ""))
    # Base rank: DDG order matters a lot.
    score = float(idx)
    # Penalise junk hosts hard.
    if any(host == h or host.endswith("." + h) for h in _PENALISED_HOSTS):
        score += 50.0
    # Light bonus for known-authoritative docs hosts.
    if any(host == h or host.endswith("." + h) for h in _PREFERRED_HOSTS):
        score -= 5.0
    # Site hint match: massive bonus.
    if site_hint:
        if site_hint and ("." in site_hint) and (host == site_hint
                                                  or host.endswith("." + site_hint)):
            score -= 30.0
        elif site_hint and "." not in site_hint:
            # Substring bump (e.g. "docs" → any host containing "docs").
            if site_hint in host or site_hint in (result.get("url", "") or "").lower():
                score -= 10.0
    # Title containing the query terms helps a little.
    title = (result.get("title") or "").lower()
    q_lower = query.lower()
    overlap = len(set(q_lower.split()) & set(title.split()))
    score -= 0.1 * overlap
    return score


def _launch_url(url: str, browser_name: Optional[str]) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "find_and_open only supports Windows"
    if "\n" in url or "\r" in url:
        return False, "url contains newline"
    if browser_name:
        exe = _find_browser(browser_name)
        if not exe:
            return False, f"browser {browser_name!r} not found"
        cmdline = [exe, url]
    else:
        cmdline = ["cmd.exe", "/c", "start", "", url]
    try:
        subprocess.Popen(
            cmdline,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        return False, f"launch failed: {exc}"
    return True, ""


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "reason": "query is required"}
        max_consider = max(3, min(int(args.get("max_consider", 8)), 10))
        browser_name = (args.get("browser") or "").strip().lower() or None
        # Optional explicit site that overrides the hint heuristic.
        explicit_site = (args.get("site") or "").strip().lower() or None

        # Run the search via the same DDG helper Module C uses.
        from ultron_llm.web_search import search as _ddg_search  # type: ignore[import]
        try:
            results = await _ddg_search(query, max_results=max_consider)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"search failed: {exc}"}
        if not results:
            return {"ok": False, "reason": "no search results"}

        rows = [{"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results]

        site_hint = explicit_site or _site_hint_for(query)
        ranked = sorted(enumerate(rows),
                        key=lambda kv: _score(kv[1], kv[0], query, site_hint))
        best_idx, best = ranked[0]
        ok, reason = _launch_url(best["url"], browser_name)
        if not ok:
            return {"ok": False, "reason": reason, "best_url": best["url"]}
        return {
            "ok": True,
            "query": query,
            "url": best["url"],
            "title": best["title"],
            "host": _host_of(best["url"]),
            "site_hint": site_hint,
            "browser": browser_name or "default",
            "considered": len(rows),
            "rank_of_winner_in_ddg": best_idx,
            "alternates": [
                {"title": r["title"], "url": r["url"]}
                for _, r in ranked[1:4]
            ],
        }

    return Tool(
        name="find_and_open",
        description=(
            "Search the web, pick the most relevant result, and open it "
            "directly in the default browser (or a specific browser via "
            "the browser arg). Use this INSTEAD OF web_search+web_open "
            "when the user wants to GO to the page rather than see a "
            "list of results. Site hints in the query "
            "('wikipedia python decorators', 'github faster-whisper') "
            "bias the ranking toward that domain. Add the explicit site "
            "arg to force a domain."
        ),
        category="internet",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "site": {"type": "string", "maxLength": 128},
                "browser": {"type": "string",
                            "enum": ["chrome", "brave", "edge", "firefox"]},
                "max_consider": {"type": "integer", "minimum": 3, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )
