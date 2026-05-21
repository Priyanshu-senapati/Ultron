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
    # Empty tool_name = "data intent": we know the answer from state,
    # no tool dispatch needed. The reply *is* the answer. Used for
    # "what's the weather", "what time is it", etc.
    tool_name: str
    args: dict[str, Any]
    # What ULTRON says back. Short — TTS-friendly. Empty string = silent.
    reply: str = ""
    # Confidence in the match. Used only for logging; we treat all
    # successful regex matches as confident enough to skip the LLM.
    confidence: float = 1.0

    @property
    def is_data_intent(self) -> bool:
        return not self.tool_name and bool(self.reply)


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
    "play": "play_pause",     # bare "play" = resume; "play X" goes to spotify
    "resume": "play_pause",
    "next": "next",
    "next song": "next",
    "next track": "next",
    "next one": "next",
    "skip": "next",
    "skip song": "next",
    "skip track": "next",
    "skip this": "next",
    "skip ahead": "next",
    "go forward": "next",
    "forward": "next",
    "fast forward": "next",
    "previous": "prev",
    "previous song": "prev",
    "previous track": "prev",
    "previous one": "prev",
    "prev": "prev",
    "go back": "prev",
    "back": "prev",
    "back one": "prev",
    "last song": "prev",
    "last track": "prev",
    "replay": "prev",
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


_GENERIC_PLAY_TARGETS = {
    "music", "some music", "a song", "song", "songs", "something",
    "anything", "tunes", "audio", "the music", "my music",
}


def _intent_spotify_play(m: re.Match[str]) -> IntentMatch:
    query = m.group("query").strip().rstrip(".!?,").lower()
    # "play music" / "play some music" / "play a song" — the user wants
    # *something* to play, not a search for the literal word "music".
    # That's a media-key play/pause, not a spotify search URI.
    if query in _GENERIC_PLAY_TARGETS:
        return IntentMatch(
            tool_name="media_control",
            args={"what": "play_pause"},
            reply="Playing.",
        )
    # Prefer the real Web API path — it actually starts the song
    # instead of just opening Spotify's search page. spotify_control
    # falls back with a clear error if the bridge is unauthorized,
    # which the user will hear and can act on.
    return IntentMatch(
        tool_name="spotify_control",
        args={"action": "play_query", "query": query, "kind": "track"},
        reply=f"Playing {query}.",
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
    """Route a free-text web query.

    Default: ``find_and_open`` — runs the search server-side, picks the
    best result, opens that page directly. The user explicitly asked
    for "the most appropriate page, not a casual search page".

    Carve-outs that still want the search-results PAGE rather than
    auto-jumping to a hit:
      - "google X" / "google for X" — user wants the Google page.
      - "search X on youtube" — YouTube's site search is the destination,
        not whatever result wins.
    Carve-outs use the old ``web_open(query=..., site=...)`` path.
    """
    raw_q = m.group("query").strip().rstrip(".!?,")
    groups = m.groupdict()
    site_kw = (groups.get("site") or "").strip().lower()
    browser_kw = (groups.get("browser") or "").strip().lower()

    # Carve-out 1: site-scoped searches still go to the site's results
    # page. The user said "on youtube" / "on amazon" for a reason — they
    # want to scroll a list.
    if site_kw in _SEARCH_SITES and _SEARCH_SITES[site_kw]:
        args: dict[str, Any] = {"query": raw_q, "site": _SEARCH_SITES[site_kw]}
        if browser_kw and browser_kw in _BROWSER_MENTIONS:
            args["browser"] = _BROWSER_MENTIONS[browser_kw]
        return IntentMatch(
            tool_name="web_open", args=args,
            reply=f"Searching {site_kw} for {raw_q}.",
        )

    # Carve-out 2: literal "google X" — user wants the Google SERP.
    verb = (m.group(0) or "").lower()
    if verb.startswith("google "):
        args = {"query": raw_q}
        if browser_kw and browser_kw in _BROWSER_MENTIONS:
            args["browser"] = _BROWSER_MENTIONS[browser_kw]
        return IntentMatch(
            tool_name="web_open", args=args,
            reply=f"Googling {raw_q}.",
        )

    # Default: jump straight to the best result via find_and_open.
    args = {"query": raw_q}
    if browser_kw and browser_kw in _BROWSER_MENTIONS:
        args["browser"] = _BROWSER_MENTIONS[browser_kw]
    return IntentMatch(
        tool_name="find_and_open", args=args,
        reply=f"Looking up {raw_q}.",
    )


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


_MEDIA_REPLIES = {
    "play_pause": "Done.",       # ambiguous direction — short and neutral
    "next":       "Next.",
    "prev":       "Previous.",
    "stop":       "Stopped.",
    "mute":       "Muted.",
    "volume_up":  "Louder.",
    "volume_down": "Quieter.",
}


def _intent_media(m: re.Match[str]) -> IntentMatch:
    verb = m.group("verb").lower().strip()
    what = _MEDIA_VERBS.get(verb)
    if not what:
        return IntentMatch(tool_name="", args={})
    # One-word confirmations. Long enough for TTS to be audible but
    # short enough that the action arrives faster than the words finish.
    if verb == "pause":
        reply = "Paused."
    elif verb in ("play", "resume"):
        reply = "Playing."
    else:
        reply = _MEDIA_REPLIES.get(what, "Done.")
    return IntentMatch(
        tool_name="media_control",
        args={"what": what},
        reply=reply,
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

    # — Media (anchored on the verb at the *start*; allow trailing
    # filler like "the music" / "the song" / "please"). NOTE: this
    # only includes "play" as a bare verb — "play X" already gets
    # caught by the spotify route above, which redirects generic
    # targets ("music"/"song"/etc.) back to media_control via
    # _GENERIC_PLAY_TARGETS. A bare "play" with optional trailing
    # filler (e.g. "play the music") still routes here.
    (re.compile(
        r"^(?:please\s+)?"
        r"(?P<verb>play|pause|resume|stop|mute|unmute|"
        r"next(?:\s+(?:song|track|one))?|"
        r"skip(?:\s+(?:song|track|this|ahead))?|"
        r"(?:fast\s+)?forward|go\s+forward|"
        r"previous(?:\s+(?:song|track|one))?|prev|"
        r"go\s+back|back(?:\s+one)?|last\s+(?:song|track)|replay|"
        r"volume\s+up|volume\s+down|louder|quieter|"
        r"turn\s+it\s+up|turn\s+it\s+down)"
        r"(?:\s+(?:the\s+)?(?:music|song|track|audio|playback|video))?"
        r"(?:\s+please)?$",
        re.IGNORECASE), _intent_media),

    # — Generic "open X" (matches AFTER spotify / url) — last
    (re.compile(r"^(?:please\s+)?(?:open|launch|start|fire\s+up|run)\s+(?P<app>.+)$",
                re.IGNORECASE), _intent_open_app),
]


_DATA_QUESTION_PATTERNS = {
    "time": re.compile(
        r"^(?:what['’]?s?\s+(?:the\s+)?time|what\s+time\s+is\s+it|"
        r"(?:current\s+)?time\s*(?:please)?|"
        r"tell\s+me\s+the\s+time)$",
        re.IGNORECASE),
    "date": re.compile(
        r"^(?:what['’]?s?\s+(?:today['’]?s?\s+)?date|"
        r"what(?:['’]s)?\s+the\s+date|what\s+day\s+is\s+it|"
        r"today['’]?s?\s+date)$",
        re.IGNORECASE),
    "weather": re.compile(
        r"^(?:how['’]?s?\s+the\s+weather|"
        r"what(?:['’]?s)?\s+(?:the\s+)?weather(?:\s+like)?|"
        r"weather(?:\s+(?:report|update|outside|now|today))?|"
        r"is\s+it\s+(?:hot|cold|raining|sunny)(?:\s+outside)?)$",
        re.IGNORECASE),
    "market": re.compile(
        r"^(?:how['’]?s?\s+the\s+market|"
        r"how(?:\s+is)?\s+sensex(?:\s+doing)?|"
        r"sensex(?:\s+today)?|nifty(?:\s+today)?|"
        r"market\s+(?:update|report|today)|"
        r"(?:what(?:['’]s)?\s+)?(?:the\s+)?(?:stock\s+)?market(?:\s+doing)?)$",
        re.IGNORECASE),
    "news": re.compile(
        r"^(?:what(?:['’]?s)?\s+(?:the\s+)?news|"
        r"any\s+news|news(?:\s+please|\s+update|\s+headlines)?|"
        r"latest\s+(?:news|headlines)|"
        r"top\s+headlines|headlines(?:\s+please)?)$",
        re.IGNORECASE),
    "battery": re.compile(
        r"^(?:battery|battery\s+level|battery\s+status|"
        r"how(?:['’]?s)?\s+(?:the\s+)?battery|"
        r"what(?:['’]?s)?\s+(?:the\s+)?battery|"
        r"how\s+much\s+battery|charge\s+level)$",
        re.IGNORECASE),
    "wifi": re.compile(
        r"^(?:wifi(?:\s+status)?|wi[-\s]?fi(?:\s+status)?|"
        r"am\s+i\s+(?:on\s+)?(?:wifi|online)|what\s+network)$",
        re.IGNORECASE),
    # "what's playing" / "what song is this" / "what am I listening to"
    # — answered directly from state.spotify, no LLM hallucination.
    "now_playing": re.compile(
        r"^(?:what(?:['’]?s|\s+is)?\s+(?:currently\s+)?playing|"
        r"what(?:['’]?s)?\s+(?:this\s+)?song(?:\s+(?:called|name))?|"
        r"what\s+(?:song|track)\s+is\s+(?:this|playing|on)|"
        r"what\s+am\s+i\s+listening\s+to|"
        r"what\s+(?:is|are)\s+(?:we|you)\s+playing|"
        r"now\s+playing|currently\s+playing|"
        r"name\s+of\s+(?:this|the)\s+song|"
        r"who\s+(?:sings|sang|is\s+singing)\s+this|"
        r"who(?:['’]?s|\s+is)?\s+(?:the\s+)?artist)$",
        re.IGNORECASE),
}


def _data_answer(kind: str, state: Any) -> Optional[str]:
    """Format a one-line spoken answer from LiveState. None = no data,
    fall through to LLM."""
    if state is None:
        return None
    if kind == "time":
        si = getattr(state, "sysinfo", None) or {}
        t = (si.get("time") if isinstance(si, dict) else None) or {}
        hhmm = t.get("hh_mm")
        if hhmm:
            return f"It's {hhmm}."
        # Fallback: ask Python for the local clock directly.
        import datetime as _dt
        from zoneinfo import ZoneInfo
        try:
            return "It's " + _dt.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H:%M") + "."
        except Exception:  # noqa: BLE001
            return None
    if kind == "date":
        si = getattr(state, "sysinfo", None) or {}
        t = (si.get("time") if isinstance(si, dict) else None) or {}
        d = t.get("date")
        if d:
            return f"It's {d}."
        import datetime as _dt
        from zoneinfo import ZoneInfo
        try:
            return _dt.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %d %B %Y.")
        except Exception:  # noqa: BLE001
            return None
    if kind == "weather":
        w = getattr(state, "weather", None) or {}
        if not isinstance(w, dict) or not w.get("available"):
            return None
        temp = w.get("temp_c"); label = w.get("label", "")
        city = (w.get("city") or "").strip()
        feels = w.get("feels_c")
        if temp is None:
            return None
        bits = [f"{round(temp)} degrees"]
        if label:
            bits.append(label)
        line = ", ".join(bits)
        if city:
            line += f" in {city}"
        if feels is not None and abs(feels - temp) >= 2:
            line += f", feels like {round(feels)}"
        return line + "."
    if kind == "market":
        s = getattr(state, "stocks", None) or {}
        if not isinstance(s, dict):
            return None
        rows = s.get("rows") or []
        if not rows:
            return None
        # Speak the headline index naturally — the raw insight string
        # ("BSESN 75238 down -0.21% | 1 down") is too geeky for TTS.
        head = rows[0]
        ticker = (head.get("ticker") or "").lstrip("^")
        ticker = {"BSESN": "Sensex", "NSEI": "Nifty"}.get(ticker, ticker)
        close = float(head.get("close") or 0)
        pct = float(head.get("change_pct") or 0.0)
        direction = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
        state_label = "open" if s.get("market_open") else "closed"
        return (
            f"Market {state_label}. {ticker} at {close:,.0f}, "
            f"{direction} {abs(pct):.2f} percent."
        )
    if kind == "news":
        n = getattr(state, "news", None) or {}
        if not isinstance(n, dict):
            return None
        heads = [h.get("title", "") for h in (n.get("headlines") or [])][:3]
        heads = [h for h in heads if h]
        if not heads:
            return None
        # Discrete sentences so TTS pauses between headlines.
        return "Top headlines. " + ". Next, ".join(heads) + "."
    if kind == "battery":
        si = getattr(state, "sysinfo", None) or {}
        b = (si.get("battery") if isinstance(si, dict) else None) or {}
        if not b.get("available"):
            return None
        pct = b.get("percent")
        pct_int = int(round(float(pct))) if pct is not None else 0
        plug = ", plugged in" if b.get("plugged") else ""
        return f"Battery at {pct_int} percent{plug}."
    if kind == "wifi":
        si = getattr(state, "sysinfo", None) or {}
        w = (si.get("wifi") if isinstance(si, dict) else None) or {}
        if not w.get("available"):
            return None
        if not w.get("connected"):
            return "Wifi off."
        return f"Connected to {w.get('ssid', 'wifi')}."
    if kind == "now_playing":
        sp = getattr(state, "spotify", None) or {}
        if not isinstance(sp, dict):
            return None
        # Spotify state is empty if the bridge hasn't received a track
        # yet (token missing, app not playing, bridge disabled). Fall
        # through to LLM rather than claim nothing's playing — silence
        # is wrong if Spotify just hasn't told us yet.
        track = (sp.get("track") or "").strip()
        if not track and not sp.get("is_playing"):
            # Truly nothing — bridge said is_playing=False (status 204).
            if "is_playing" in sp:
                return "Nothing playing right now, sir."
            return None
        artist = (sp.get("artist") or "").strip()
        is_playing = bool(sp.get("is_playing"))
        verb = "Playing" if is_playing else "Paused on"
        if track and artist:
            return f"{verb} {track} by {artist}."
        if track:
            return f"{verb} {track}."
        return None
    return None


def route(user_text: str, state: Any = None) -> Optional[IntentMatch]:
    """Return a deterministic tool dispatch or data answer for
    ``user_text``, or None to fall through to the LLM.

    When a route fires we treat that as the user's exact intent — no
    LLM round-trip, no narration, just the action or fact. The reply
    is short and confirmation-style so TTS has something brief to say.
    """
    norm = _normalise(user_text)
    if not norm:
        return None

    # Data-question intents first — they should beat the "search X"
    # generic route (e.g. "what's the weather" must not become a web
    # search).
    for kind, pat in _DATA_QUESTION_PATTERNS.items():
        if pat.match(norm):
            answer = _data_answer(kind, state)
            if answer:
                logger.info("data intent matched: kind=%s answer=%r", kind, answer[:60])
                return IntentMatch(tool_name="", args={}, reply=answer)
            # Pattern matched but state isn't populated yet — fall
            # through to the LLM so we don't blank out.
            logger.debug("data intent %s matched but state empty, falling through", kind)
            break
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
