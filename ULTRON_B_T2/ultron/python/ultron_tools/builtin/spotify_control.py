"""spotify_control tool — true Spotify playback control via Web API.

Goes through the SpotifyBridge (in bridges_service) which holds the
OAuth token and talks to https://api.spotify.com/v1/me/player. Unlike
``spotify_play`` (which only opens a search page in the desktop app),
this actually starts / pauses / skips / seeks playback.

Requires the Spotify bridge to be authorized with the
``user-modify-playback-state`` scope. If the user only auth'd before
this scope existed, control calls return a 403 with a hint to re-run
``python -m ultron_bridges.spotify``.

Actions:
  - ``next`` / ``previous``           — skip to neighbour track
  - ``pause`` / ``resume`` (or ``play``)
  - ``seek`` (args: position_ms)
  - ``volume`` (args: percent 0..100)
  - ``play_uri`` (args: uri)          — start a specific spotify: URI
  - ``play_query`` (args: query, kind=track|album|playlist|artist)
                                       — search + play first match

Fallback: when the bridge is disabled / unauthorized / not running, the
tool degrades gracefully — for play_query it shells out to the
``spotify:search:<query>`` URI handler so the user still gets the
song in the desktop app, just without auto-play. Saves the user from
hitting an error wall before they've set up OAuth.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import urllib.parse
from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.spotify_control")


def _launch_spotify_uri(uri: str) -> bool:
    """Launch a ``spotify:`` URI via the Windows shell. Used only as
    fallback when the Web API bridge can't reach the user's account."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", uri],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except OSError as exc:
        logger.warning("spotify URI fallback launch failed: %s", exc)
        return False


VALID_ACTIONS = (
    "next", "previous", "prev",
    "pause", "resume", "play",
    "seek", "volume",
    "play_uri", "play_query",
)


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        if action not in VALID_ACTIONS:
            return {"ok": False,
                    "reason": f"unknown action {action!r}",
                    "valid": list(VALID_ACTIONS)}
        payload: dict[str, Any] = {"action": action}
        # Pass through action-specific args.
        sub: dict[str, Any] = {}
        if action == "seek":
            if "position_ms" not in args:
                return {"ok": False, "reason": "seek requires position_ms"}
            sub["position_ms"] = int(args["position_ms"])
        elif action == "volume":
            if "percent" not in args:
                return {"ok": False, "reason": "volume requires percent"}
            sub["percent"] = int(args["percent"])
        elif action == "play_uri":
            uri = str(args.get("uri") or "").strip()
            if not uri:
                return {"ok": False, "reason": "play_uri requires uri"}
            sub["uri"] = uri
        elif action == "play_query":
            q = str(args.get("query") or "").strip()
            if not q:
                return {"ok": False, "reason": "play_query requires query"}
            sub["query"] = q
            sub["kind"] = str(args.get("kind") or "track")
        if sub:
            payload["args"] = sub
        result = await bridge_rpc.request_response(
            "spotify_control_request", payload,
            "spotify_control_result", timeout=8.0,
        )
        # Fallback: bridge disabled / down / unauthorized AND the action
        # is play_query → launch the spotify: search URI so the user
        # still gets the song in the desktop app. Other actions
        # (next/pause/seek/volume) have no equivalent fallback, so we
        # return the error and let the LLM speak it back.
        if (result is None or not result.get("ok")) and action == "play_query":
            q = str(args.get("query") or "").strip()
            if q and _launch_spotify_uri("spotify:search:"
                                          + urllib.parse.quote(q, safe="")):
                return {
                    "ok": True,
                    "action": action,
                    "fallback": "uri_handler",
                    "reason": ("bridge unavailable — opened Spotify "
                               "search instead. For real playback control"
                               " configure [bridges.spotify] and run "
                               "`python -m ultron_bridges.spotify`."),
                    "query": q,
                }
        if result is None:
            return {"ok": False,
                    "reason": "spotify bridge did not respond — bridge "
                              "disabled, unauthorized, or down"}
        return result

    return Tool(
        name="spotify_control",
        description=(
            "Control Spotify playback via the Spotify Web API (true "
            "control, not just opening the app). Actions: next, "
            "previous, pause, resume, seek (position_ms), volume "
            "(percent 0-100), play_uri (uri), play_query (query + "
            "optional kind: track|album|playlist|artist). Requires "
            "the Spotify bridge to be authorized with the "
            "user-modify-playback-state scope; on missing scope the "
            "tool returns a re-auth hint."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(VALID_ACTIONS)},
                "position_ms": {"type": "integer", "minimum": 0,
                                "maximum": 86_400_000},
                "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                "uri": {"type": "string", "maxLength": 256},
                "query": {"type": "string", "maxLength": 256},
                "kind": {"type": "string",
                         "enum": ["track", "album", "playlist", "artist"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )
