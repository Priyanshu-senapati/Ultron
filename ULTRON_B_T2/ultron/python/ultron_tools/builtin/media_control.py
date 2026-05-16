"""media_control tool — Windows media-key actions.

Sends synthetic key events for play/pause, next, prev, stop, mute,
volume up/down. Any media-aware app (Spotify, YouTube, browser tabs
with media, Windows Media Player) responds. No app focus required —
these are global hotkeys at the OS level.

Direction:
- For now this is a *single* tool with a ``what`` arg. If we later need
  app-specific control (only Spotify, not the browser), we'll add a
  Spotify Web API path via the bridges_service.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


# Windows virtual-key codes (winuser.h).
_VK = {
    "play_pause": 0xB3,
    "play": 0xB3,         # toggles
    "pause": 0xB3,        # toggles
    "next": 0xB0,
    "prev": 0xB1,
    "previous": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "vol_up": 0xAF,
    "vol_down": 0xAE,
}
_KEYEVENTF_KEYUP = 0x0002


def _press(vk: int) -> None:
    if sys.platform != "win32":
        raise RuntimeError("media_control only supports Windows")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # Down + up = one tap.
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        what = str(args.get("what", "")).strip().lower().replace("-", "_")
        if not what:
            return {"ok": False, "reason": "what is required"}
        vk = _VK.get(what)
        if vk is None:
            return {
                "ok": False,
                "reason": f"unknown action {what!r}",
                "valid": sorted(set(_VK.keys())),
            }
        repeat = int(args.get("repeat", 1))
        if repeat < 1 or repeat > 20:
            return {"ok": False, "reason": "repeat must be 1..20"}
        try:
            for _ in range(repeat):
                _press(vk)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"key send failed: {exc}"}
        return {"ok": True, "what": what, "repeat": repeat}

    return Tool(
        name="media_control",
        description=(
            "Send a Windows media key. Use for play/pause, next/prev "
            "track, stop, mute, volume up/down. Works with any media-"
            "aware app currently running (Spotify, browser, etc)."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "enum": [
                        "play_pause", "play", "pause", "next",
                        "prev", "previous", "stop", "mute",
                        "volume_up", "volume_down", "vol_up", "vol_down",
                    ],
                },
                "repeat": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["what"],
            "additionalProperties": False,
        },
        handler=handler,
    )
