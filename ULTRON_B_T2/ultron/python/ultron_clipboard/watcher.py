"""Clipboard watcher -- polls the system clipboard, classifies content,
publishes clipboard_changed events.

Content types detected:
  url, email, phone, code, file_path, number, json, text
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

from .config import ClipboardConfig

logger = logging.getLogger("ultron.clipboard")

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_PHONE_RE = re.compile(r"^[\+]?[\d\s\-\(\)]{7,15}$")
_FILE_PATH_RE = re.compile(r"^[A-Za-z]:\\[\w\\\-\. ]+$|^/[\w/\-\. ]+$")
_CODE_INDICATORS = (
    "def ", "class ", "function ", "const ", "let ", "var ",
    "import ", "from ", "#include", "public ", "private ",
    "return ", "if (", "for (", "while (", "=> {",
    "#!/", "print(", "console.log",
)


def _classify(text: str) -> str:
    """Classify clipboard text into a content type."""
    stripped = text.strip()
    if not stripped:
        return "empty"
    if _URL_RE.match(stripped):
        return "url"
    if _EMAIL_RE.match(stripped):
        return "email"
    if _PHONE_RE.match(stripped):
        return "phone"
    if _FILE_PATH_RE.match(stripped):
        return "file_path"
    try:
        json.loads(stripped)
        return "json"
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        float(stripped.replace(",", ""))
        return "number"
    except ValueError:
        pass
    if any(indicator in stripped for indicator in _CODE_INDICATORS):
        return "code"
    return "text"


def _get_clipboard() -> Optional[str]:
    """Read the system clipboard. Returns None on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        CF_UNICODETEXT = 13
        if not user32.OpenClipboard(0):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return None


class ClipboardWatcher:
    def __init__(self, cfg: ClipboardConfig, publish) -> None:
        self._cfg = cfg
        self._publish = publish
        self._task: Optional[asyncio.Task] = None
        self._last_content: str = ""
        self._last_ts: float = 0.0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="clipboard-watcher")
        logger.info("clipboard watcher started (poll=%.1fs)", self._cfg.poll_secs)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                content = await loop.run_in_executor(None, _get_clipboard)
                if content and content != self._last_content:
                    self._last_content = content
                    self._last_ts = time.time()
                    content_type = _classify(content)
                    truncated = content[:self._cfg.max_content_chars]
                    await self._publish("clipboard_changed", {
                        "content": truncated,
                        "content_type": content_type,
                        "length": len(content),
                        "ts": self._last_ts,
                    })
                    logger.info(
                        "clipboard: type=%s len=%d preview=%r",
                        content_type, len(content), truncated[:60],
                    )
            except Exception as exc:
                logger.error("clipboard poll failed: %s", exc)
            await asyncio.sleep(self._cfg.poll_secs)
