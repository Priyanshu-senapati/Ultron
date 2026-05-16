"""Hard intent router — turn common voice commands into tool calls
WITHOUT going through the LLM.

The local model is unreliable at the tool-call protocol under load:
it will narrate instead of acting, leak "Commander <FirstName>", and
volunteer surveillance preambles. For the common verbs ("play …",
"open …", "search …", "brightness …", "pause / next / volume up")
we don't need the LLM at all — a regex match is deterministic, fast,
and never sycophantic.

If ``route(text)`` returns an ``IntentMatch``, the LLM service:
  - publishes the tool_call_request directly,
  - returns a short canned confirmation as the response,
  - skips the ollama round-trip.

Anything not matched falls through to the LLM as before.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("ultron.llm.intent")


@dataclass
class IntentMatch:
    tool_name: str
    args: dict[str, Any]
    # What ULTRON says back. Short — TTS-friendly. Empty string = silent.
    reply: str = ""
    # Confidence in the match. Used only for logging; we treat all
    # successful regex matches as confident enough to skip the LLM.
    confidence: float = 1.0


# ── Normalisation ────────────────────────────────────────────────────


_LEADING_WAKE = re.compile(
    r"^(?:hey\s+)?(?:ultron|altron|hello\s+ultron)[,!.\s]*",
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r"[\s.,!?;:]+$")


def _normalise(text: str) -> str:
    """Strip leading wake word, surrounding punctuation, collapse spaces."""
    t = (text or "").strip()
    t = _LEADING_WAKE.sub("", t).strip()
    t = _TRAILING_PUNCT.sub("", t).strip()
    return re.sub(r"\s+", " ", t)


# ── Browser / app helpers ────────────────────────────────────────────


_BROWSER_MENTIONS = {
    "chrome": "chrome", "google chrome": "chrome",
    "brave": "brave", "edge": "edge", "microsoft edge": "edge",
    "firefox": "firefox",
}

_SEARCH_SITES = {
    "youtube": "youtube.com",
    "yt": "youtube.com",
    "google": None,           # default
    "amazon": "amazon.in",
    "wikipedia": "wikipedia.org",
    "reddit": "reddit.com",
    "github": "github.com",
    "stack overflow": "stackoverflow.com",
    "stackoverflow": "stackoverflow.com",
    "twitter": "twitter.com",
    "x": "x.com",
}


_MEDIA_VERBS = {
    "pause": "play_pause",
    "play": None,            # too generic — handled by spotify path
    "resume": "play_pause",
    "next": "next",
    "next song": "next",
    "next track": "next",
    "previous": "prev",
    "previous song": "prev",
    "previous track": "prev",
    "prev": "prev",
    "stop": "stop",
    "mute": "mute",
    "unmute": "mute",
    "volume up": "volume_up",
    "louder": "volume_up",
    "turn it up": "volume_up",
    "volume down": "volume_down",
    "quieter": "volume_down",
    "turn it down": "volume_down",
}


# ── Pattern routes ───────────────────────────────────────────────────
#
# Each route is a (regex, builder) pair. The first match wins; order
# matters — put narrower patterns above broader ones.


def _intent_spotify_play(m: re.Match[str]) -> IntentMatch:
    query = m.group("query").strip().rstrip(".!?,")
    return IntentMatch(
        tool_name="spotify_play",
        args={"query": query},
        reply=f"Playing {query} on Spotify.",
    )


def _intent_open_spotify_bare(_m: re.Match[str]) -> IntentMatch:
    return IntentMatch(
        tool_name="open_app",
        args={"name": "spotify"},
        reply="Opening Spotify.",
    )


def _intent_open_app(m: re.Match[str]) -> IntentMatch:
    name = m.group("app").strip().rstrip(".!?,").lower()
    # Reject very long names — likely not an app, fall back to LLM.
    if len(name) > 64:
        return IntentMatch(tool_name="", args={})
    return IntentMatch(
        tool_name="open_app",
        args={"name": name},
        reply=f"Opening {name}.",
    )


def _intent_web_search(m: re.Match[str]) -> IntentMatch:
    raw_q = m.group("query").strip().rstrip(".!?,")
    # Use groupdict().get so missing optional groups (the generic
    # fallback pattern has no site/browser groups) don't IndexError —
    # which `route()` silently swallows, falling through to the LLM.
    groups = m.groupdict()
    site_kw = (groups.get("site") or "").strip().lower()
    browser_kw = (groups.get("browser") or "").strip().lower()
    args: dict[str, Any] = {"query": raw_q}
    # Resolve site: "on youtube" → site=youtube.com
    if site_kw in _SEARCH_SITES and _SEARCH_SITES[site_kw]:
        args["site"] = _SEARCH_SITES[site_kw]
        reply = f"Searching {site_kw} for {raw_q}."
    elif browser_kw and browser_kw in _BROWSER_MENTIONS:
        args["browser"] = _BROWSER_MENTIONS[browser_kw]
        reply = f"Searching {raw_q} on {browser_kw}."
    else:
        reply = f"Searching for {raw_q}."
    return IntentMatch(tool_name="web_open", args=args, reply=reply)


def _intent_open_url(m: re.Match[str]) -> IntentMatch:
    url = m.group("url").strip().rstrip(".!?,")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return IntentMatch(
        tool_name="web_open",
        args={"url": url},
        reply=f"Opening {url}.",
    )


def _intent_brightness_level(m: re.Match[str]) -> IntentMatch:
    level = max(0, min(100, int(m.group("n"))))
    return IntentMatch(
        tool_name="brightness",
        args={"action": "set", "level": level},
        reply=f"Brightness set to {level}.",
    )


def _intent_brightness_dir(m: re.Match[str]) -> IntentMatch:
    direction = m.group("dir").lower()
    # "dim" / "darker" → down. "brighter" → up.
    if direction in ("up", "increase", "raise", "brighter"):
        action = "up"
    else:
        action = "down"
    return IntentMatch(
        tool_name="brightness",
        args={"action": action, "step": 10},
        reply=f"Brightness {action}.",
    )


def _intent_media(m: re.Match[str]) -> IntentMatch:
    verb = m.group("verb").lower().strip()
    what = _MEDIA_VERBS.get(verb)
    if not what:
        return IntentMatch(tool_name="", args={})
    return IntentMatch(
        tool_name="media_control",
        args={"what": what},
        reply="",   # silent — the action is its own confirmation
    )


_ROUTES: list[tuple[re.Pattern[str], Callable[[re.Match[str]], IntentMatch]]] = [
    # — Music — keep above generic "open" so "play X on spotify" wins
    (re.compile(
        r"^(?:please\s+)?(?:can\s+you\s+)?(?:play|put\s+on)\s+"
        r"(?P<query>.+?)\s+(?:on|via|with|using)\s+spotify$",
        re.IGNORECASE), _intent_spotify_play),
    (re.compile(
        r"^(?:please\s+)?(?:can\s+you\s+)?(?:play|put\s+on)\s+(?P<query>.+)$",
        re.IGNORECASE), _intent_spotify_play),
    (re.compile(r"^(?:open\s+)?spotify$", re.IGNORECASE), _intent_open_spotify_bare),

    # — Search — narrower (with browser or site) first
    (re.compile(
        r"^(?:please\s+)?(?:can\s+you\s+)?"
        r"(?:search(?:\s+for)?|google|look\s+up|find)\s+"
        r"(?P<query>.+?)\s+on\s+(?P<site>youtube|yt|google|amazon|wikipedia|reddit|github|"
        r"stack\s*overflow|twitter|x)$",
        re.IGNORECASE), _intent_web_search),
    (re.compile(
        r"^(?:please\s+)?(?:can\s+you\s+)?"
        r"(?:search(?:\s+for)?|google|look\s+up|find)\s+"
        r"(?P<query>.+?)\s+(?:on|in|with|using)\s+"
        r"(?P<browser>chrome|brave|edge|firefox|microsoft\s+edge|google\s+chrome)$",
        re.IGNORECASE), _intent_web_search),
    (re.compile(
        r"^(?:please\s+)?(?:can\s+you\s+)?"
        r"(?:search(?:\s+for)?|google|look\s+up|find|search)\s+(?P<query>.+)$",
        re.IGNORECASE), _intent_web_search),

    # — Direct URL
    (re.compile(r"^(?:open\s+)?(?P<url>(?:https?://)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?)$",
                re.IGNORECASE), _intent_open_url),

    # — Brightness
    (re.compile(
        r"^(?:please\s+)?(?:set\s+|change\s+|make\s+)?brightness\s+(?:to\s+)?(?P<n>\d{1,3})%?$",
        re.IGNORECASE), _intent_brightness_level),
    (re.compile(
        r"^(?:please\s+)?(?:brightness|screen)\s+(?P<dir>up|down|increase|raise|brighter)$",
        re.IGNORECASE), _intent_brightness_dir),
    (re.compile(r"^(?:dim\s+(?:the\s+)?(?:screen|display)|make\s+(?:it|screen)\s+darker)$",
                re.IGNORECASE),
     lambda m: IntentMatch(tool_name="brightness",
                            args={"action": "down", "step": 15},
                            reply="Dimming the screen.")),

    # — Media
    (re.compile(
        r"^(?P<verb>pause|resume|next(?:\s+(?:song|track))?|previous(?:\s+(?:song|track))?|"
        r"prev|stop|mute|unmute|volume\s+up|louder|turn\s+it\s+up|volume\s+down|"
        r"quieter|turn\s+it\s+down)$",
        re.IGNORECASE), _intent_media),

    # — Generic "open X" (matches AFTER spotify / url) — last
    (re.compile(r"^(?:please\s+)?(?:open|launch|start|fire\s+up|run)\s+(?P<app>.+)$",
                re.IGNORECASE), _intent_open_app),
]


def route(user_text: str) -> Optional[IntentMatch]:
    """Return a deterministic tool dispatch for ``user_text``, or None.

    When a route fires we treat that as the user's exact intent — no
    LLM round-trip, no narration, just the action. The reply is short
    and confirmation-style so TTS has something brief to say.
    """
    norm = _normalise(user_text)
    if not norm:
        return None
    for pattern, builder in _ROUTES:
        m = pattern.match(norm)
        if m:
            try:
                intent = builder(m)
            except Exception:  # noqa: BLE001
                logger.exception("intent builder raised for %r", norm[:60])
                return None
            if intent.tool_name:
                logger.info(
                    "intent matched: text=%r -> %s args=%s",
                    norm[:80], intent.tool_name, intent.args,
                )
                return intent
    return None
