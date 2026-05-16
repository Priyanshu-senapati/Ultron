"""spotify_play tool — play a song / artist / playlist on Spotify.

Two paths:

- ``query``  do a Spotify search via the ``spotify:search:`` URI scheme.
             Opens the search results inside the app; the user (or
             the app's auto-play) starts playback.
- ``uri``    if the user (or a future Spotify-Web-API bridge) hands
             us an actual track URI like ``spotify:track:xxxx``, play
             that directly via the same URI handler.

The Microsoft Store version of Spotify *does* register the
``spotify:`` URI handler at the app level even though it doesn't
register it as a system-wide HKCR scheme. The Windows shell finds it
via the App's protocol declarations when you ``start "" spotify:…``.

If neither query nor uri is given, falls back to opening the app.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import urllib.parse
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.spotify_play")


def _launch_uri(uri: str) -> bool:
    """Open a spotify: URI via the Windows shell. Returns False on launch error."""
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", uri],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except OSError as exc:
        logger.warning("spotify URI launch failed: %s", exc)
        return False


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if sys.platform != "win32":
            return {"ok": False, "reason": "spotify_play only supports Windows"}
        uri = (args.get("uri") or "").strip()
        query = (args.get("query") or "").strip()

        if uri:
            if not uri.startswith("spotify:"):
                return {"ok": False, "reason": "uri must start with spotify:"}
            if not _launch_uri(uri):
                return {"ok": False, "reason": "URI launch failed"}
            return {"ok": True, "uri": uri}

        if query:
            search_uri = "spotify:search:" + urllib.parse.quote(query, safe="")
            if not _launch_uri(search_uri):
                return {"ok": False, "reason": "search launch failed"}
            return {"ok": True, "query": query, "uri": search_uri}

        # Bare spotify_play with no args → just open Spotify.
        if not _launch_uri("spotify:"):
            return {"ok": False, "reason": "bare spotify: URI failed"}
        return {"ok": True, "query": "", "uri": "spotify:"}

    return Tool(
        name="spotify_play",
        description=(
            "Play music on Spotify. Pass query for a name/lyric search "
            "('play Closer Chainsmokers'), or uri for a specific "
            "spotify:track:/spotify:album:/spotify:playlist: link. "
            "Opens results in the Spotify app; the user picks the "
            "track or auto-play starts it."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 512},
                "uri":   {"type": "string", "maxLength": 256},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
