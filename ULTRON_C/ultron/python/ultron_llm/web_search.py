"""web_search.py — DuckDuckGo search for Module C (same as B_T2 copy)."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("ultron.llm.websearch")


_SEARCH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(search (?:for|the)|look (?:up|it up)|google)\b",
        r"\b(?:find|fetch|grab) me (?:info|details|news|article)\b",
        r"\b(?:any|latest|recent|current) news (?:on|about)\b",
        r"\bwhat'?s (?:the latest|happening|new) (?:on|with|in|about)\b",
        r"\b(?:online|on the web|on internet)\b",
        r"\b(?:weather|forecast) (?:in|for|today)\b",
        r"\b(?:price|cost) of\b",
        r"\b(?:release date|when (?:does|did|will))\b",
        r"\bsearch (?:online|the web|the internet)\b",
        r"\bquery (?:the web|google|duckduckgo)\b",
    )
)


def looks_searchable(query: str) -> bool:
    if not query:
        return False
    return any(p.search(query) for p in _SEARCH_PATTERNS)


@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    url: str


async def search(query: str, max_results: int = 5) -> list[SearchResult]:
    def _do() -> list[SearchResult]:
        DDGS = None
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-not-found,no-redef]
            except ImportError:
                logger.warning("ddgs not installed; web search disabled")
                return []
        out: list[SearchResult] = []
        try:
            with DDGS() as d:
                for r in d.text(query, max_results=max_results):
                    out.append(SearchResult(
                        title=str(r.get("title", "")),
                        snippet=str(r.get("body") or r.get("snippet") or ""),
                        url=str(r.get("href") or r.get("url") or ""),
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("DDG search failed: %s", exc)
        return out

    return await asyncio.to_thread(_do)


def format_results(results: list[SearchResult]) -> str:
    if not results:
        return ""
    lines = ["[WEB SEARCH RESULTS]"]
    for i, r in enumerate(results, 1):
        snippet = r.snippet.replace("\n", " ").strip()
        if len(snippet) > 300:
            snippet = snippet[:300] + "…"
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {snippet}")
        lines.append(f"   <{r.url}>")
    return "\n".join(lines)
