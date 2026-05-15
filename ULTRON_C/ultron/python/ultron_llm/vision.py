"""vision.py — on-demand screen reading for Module C.

When the user asks something visual, screenshot the foreground display
and pass it to LLaVA via Ollama with the user's exact question. Grounded
answer goes back as the response.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from typing import Optional

logger = logging.getLogger("ultron.llm.vision")


_VISUAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:my |the )?screen\b",
        r"\bon (?:my |the )?display\b",
        r"\bwhat('?s| is| does)\s+(this|that)\b",
        r"\b(read|summari[sz]e|describe|explain|translate|analyze|analyse) (?:this|that|it)\b",
        r"\blook at (?:this|that|here|my (?:screen|display)|the (?:screen|display))\b",
        r"\bsee (?:this|that|what|my (?:screen|display))\b",
        r"\b(?:do|can) you see\b",
        r"\bwhat am i (?:doing|looking at|seeing|reading|listening to|watching|playing)\b",
        r"\bwhat(?:'?s| is) (?:happening|going on|playing|on)\b",
        r"\bwhat (?:song|track|music|tune|artist|album|video|movie|show|episode|series|game|app)\b",
        r"\bname (?:this|the) (?:song|track|music|tune|artist|album|video|movie|show|page)\b",
        r"\bwhich (?:song|track|music|video|movie|show|episode|page|tab|window|app|file)\b",
        r"\b(?:this|that|the) (?:song|track|video|movie|show|episode|page|tab|window|error|message|file|document|article)\b",
        r"\bwhat does (?:this|that|the (?:error|page|message|screen|window|button))\s+(say|do|mean)\b",
        r"\bhelp me (?:with|read|understand) (?:this|that|the)\b",
        r"\bcheck (?:my |the )?(?:screen|display|tab|page|browser|window)\b",
        r"\btell me (?:about|what) (?:this|that|i'?m (?:doing|looking|seeing|reading|watching|listening))\b",
    )
)


def looks_visual(query: str) -> bool:
    if not query:
        return False
    return any(p.search(query) for p in _VISUAL_PATTERNS)


def capture_screen_b64(max_dimension: int = 2400, jpeg_quality: int = 92) -> Optional[str]:
    try:
        from PIL import ImageGrab  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("Pillow not installed — vision disabled.")
        return None
    try:
        img = ImageGrab.grab()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ImageGrab failed: %s", exc)
        return None

    w, h = img.size
    longest = max(w, h)
    if longest > max_dimension:
        scale = max_dimension / longest
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


VISION_SYSTEM_PROMPT = (
    "You are ULTRON. An image of the user's current screen is attached. "
    "Your job is to answer their question grounded in what you actually see — "
    "NOT to apologise about lacking access to apps, accounts, or files. You "
    "DO have access: the image is right there.\n\n"
    "REQUIRED BEHAVIOUR:\n"
    "1. READ TEXT VERBATIM. Quote song titles, artist names, page titles, "
    "URLs, error messages, file names, button labels, dialog text — whatever "
    "the user is asking about. Be SPECIFIC.\n"
    "2. IDENTIFY APPS by their UI: Spotify, YouTube, YouTube Music, Apple "
    "Music, VS Code, Chrome, Discord, Slack, Notion, etc.\n"
    "3. If the relevant text is small or partially hidden, describe what you "
    "CAN see rather than refusing.\n"
    "4. If the screen genuinely doesn't contain the answer, say so plainly "
    "with one sentence describing what IS visible instead.\n"
    "5. NEVER say 'I don't have access to your <X> account' or 'check the app "
    "yourself' — you can see the app right now.\n\n"
    "Address the user as 'sir' or 'commander' naturally. Lead with the answer."
)
