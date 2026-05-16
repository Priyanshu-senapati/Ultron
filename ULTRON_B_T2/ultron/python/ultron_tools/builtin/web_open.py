"""web_open tool — open a URL or web search in a browser.

Two modes:

- ``url``    open the URL directly (any scheme: https, http, file).
- ``query``  build a Google search URL for the query.

Optional ``site`` arg narrows the search to a single site
(``site=youtube.com`` for "play X on YouTube").

We pick the browser by name when given (``browser="chrome"``), else
the OS default handler for ``https:`` opens it.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.web_open")


_BROWSER_EXE: dict[str, list[str]] = {
    # Each value is a list of candidate exe paths to try.
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
    if name and name in _BROWSER_EXE:
        for p in _BROWSER_EXE[name]:
            if Path(p).exists():
                return p
        # Fall back to PATH lookup
        for stem in (name, f"{name}.exe"):
            found = shutil.which(stem)
            if found:
                return found
    return None


def _build_url(args: dict[str, Any]) -> str | None:
    if args.get("url"):
        return str(args["url"]).strip()
    q = (args.get("query") or "").strip()
    if not q:
        return None
    site = (args.get("site") or "").strip()
    if site:
        q = f"site:{site} {q}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if sys.platform != "win32":
            return {"ok": False, "reason": "web_open only supports Windows"}
        url = _build_url(args)
        if not url:
            return {"ok": False, "reason": "either url or query is required"}
        # Defence in depth: shell metachars are forbidden.
        bad = ("\n", "\r")
        if any(b in url for b in bad):
            return {"ok": False, "reason": "url contains newline"}
        browser_name = (args.get("browser") or "").strip().lower()
        try:
            if browser_name:
                exe = _find_browser(browser_name)
                if exe:
                    cmdline = [exe, url]
                else:
                    return {"ok": False, "reason": f"browser {browser_name!r} not found"}
            else:
                # Default handler: `cmd /c start "" <url>` honours the
                # user's default browser without us having to know it.
                cmdline = ["cmd.exe", "/c", "start", "", url]
            subprocess.Popen(
                cmdline,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                              | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            return {"ok": False, "reason": f"launch failed: {exc}"}
        return {"ok": True, "url": url, "browser": browser_name or "default"}

    return Tool(
        name="web_open",
        description=(
            "Open a URL or a web search in a browser. Pass either url "
            "or query. Optional site narrows the search to one domain "
            "(site=youtube.com to search YouTube). Optional browser "
            "chooses chrome/brave/edge/firefox; default uses the OS "
            "default browser."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "url":     {"type": "string", "maxLength": 2048},
                "query":   {"type": "string", "maxLength": 512},
                "site":    {"type": "string", "maxLength": 128},
                "browser": {"type": "string", "enum": ["chrome", "brave", "edge", "firefox"]},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
