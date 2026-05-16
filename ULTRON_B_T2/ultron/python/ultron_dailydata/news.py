"""News collector — Google News RSS for India headlines.

Google News exposes free RSS feeds per topic/country/language. No key
needed. We pull a topic feed, dedupe by title hash, and return a short
list of headlines + sources + published timestamps.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("ultron.dailydata.news")


def _feed_url(country: str, lang: str, topic: str) -> str:
    # Topic feeds: https://news.google.com/rss/headlines/section/topic/<TOPIC>
    # The trailing ``hl/gl/ceid`` params filter by language and country.
    topic_path = f"headlines/section/topic/{topic.upper()}" if topic else ""
    base = "https://news.google.com/rss"
    if topic_path:
        return f"{base}/{topic_path}?hl={lang}&gl={country}&ceid={country}:{lang}"
    return f"{base}?hl={lang}&gl={country}&ceid={country}:{lang}"


def _hash_title(title: str) -> str:
    return hashlib.sha1(title.lower().encode("utf-8", errors="ignore")).hexdigest()[:12]


async def fetch_headlines(
    client: httpx.AsyncClient,
    *, country: str, lang: str, topic: str, limit: int,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` deduped headlines, freshest first."""
    try:
        import feedparser  # type: ignore[import]
    except ImportError:
        return []
    url = _feed_url(country, lang, topic)
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        raw = r.text
    except httpx.HTTPError as exc:
        logger.warning("google news fetch failed: %s", exc)
        return []
    # feedparser is sync; OK to call inline — parsing 5KB of RSS is sub-ms.
    parsed = feedparser.parse(raw)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entry in (parsed.entries or [])[: max(limit * 2, limit)]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        h = _hash_title(title)
        if h in seen:
            continue
        seen.add(h)
        source = ""
        src = entry.get("source")
        if isinstance(src, dict):
            source = str(src.get("title") or src.get("href") or "")
        elif isinstance(src, str):
            source = src
        # entry.published_parsed is a time.struct_time in UTC.
        ts: float = 0.0
        if entry.get("published_parsed"):
            try:
                tup = entry["published_parsed"]
                ts = datetime(*tup[:6], tzinfo=timezone.utc).timestamp()
            except Exception:  # noqa: BLE001
                ts = 0.0
        out.append({
            "title": title,
            "source": source,
            "link": (entry.get("link") or "").strip(),
            "ts": ts,
        })
        if len(out) >= limit:
            break
    return out
