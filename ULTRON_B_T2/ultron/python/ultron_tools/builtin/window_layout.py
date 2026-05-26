"""window_layout tool -- save and restore window arrangements by name.

Uses Win32 API (ctypes) to enumerate top-level windows, capture their
positions/sizes, and restore them. Layouts are saved as JSON files in
%APPDATA%/ULTRON/layouts/.

Tools:
  save_layout(name)    -- snapshot current window positions
  restore_layout(name) -- move windows back to saved positions
  list_layouts()       -- show available saved layouts
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.window_layout")


def _layouts_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = Path(appdata) / "ULTRON" / "layouts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _enum_windows() -> list[dict[str, Any]]:
    """Enumerate visible top-level windows with their positions."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    windows: list[dict[str, Any]] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not title or title in ("Program Manager", ""):
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        # Skip minimized windows (rect is all -32000)
        if rect.left <= -30000:
            return True

        # Get process name
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = ""
        try:
            import subprocess
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-Process -Id {pid.value} -ErrorAction SilentlyContinue).ProcessName"],
                capture_output=True, text=True, timeout=3,
            )
            proc_name = (r.stdout or "").strip()
        except Exception:
            pass

        windows.append({
            "hwnd": hwnd,
            "title": title[:120],
            "process": proc_name,
            "x": rect.left,
            "y": rect.top,
            "w": rect.right - rect.left,
            "h": rect.bottom - rect.top,
        })
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def _restore_windows(layout: list[dict[str, Any]]) -> tuple[int, int]:
    """Restore windows to saved positions. Returns (matched, total)."""
    if sys.platform != "win32":
        return 0, 0
    user32 = ctypes.windll.user32
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    current = _enum_windows()
    matched = 0
    for saved in layout:
        sproc = (saved.get("process") or "").lower()
        stitle = (saved.get("title") or "").lower()
        for win in current:
            wproc = (win.get("process") or "").lower()
            wtitle = (win.get("title") or "").lower()
            if sproc and wproc and sproc == wproc:
                user32.SetWindowPos(
                    win["hwnd"], 0,
                    saved["x"], saved["y"],
                    saved["w"], saved["h"],
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
                matched += 1
                break
            elif stitle and stitle in wtitle:
                user32.SetWindowPos(
                    win["hwnd"], 0,
                    saved["x"], saved["y"],
                    saved["w"], saved["h"],
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
                matched += 1
                break

    return matched, len(layout)


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "")).strip().lower()

        if action == "save":
            name = str(args.get("name", "")).strip().lower().replace(" ", "_")
            if not name:
                return {"ok": False, "reason": "name is required"}
            windows = _enum_windows()
            serializable = [
                {k: v for k, v in w.items() if k != "hwnd"}
                for w in windows
            ]
            path = _layouts_dir() / f"{name}.json"
            path.write_text(json.dumps(serializable, indent=2))
            return {
                "ok": True,
                "action": "save",
                "name": name,
                "window_count": len(serializable),
                "path": str(path),
            }

        elif action == "restore":
            name = str(args.get("name", "")).strip().lower().replace(" ", "_")
            if not name:
                return {"ok": False, "reason": "name is required"}
            path = _layouts_dir() / f"{name}.json"
            if not path.exists():
                available = [f.stem for f in _layouts_dir().glob("*.json")]
                return {
                    "ok": False,
                    "reason": f"layout '{name}' not found",
                    "available": available,
                }
            layout = json.loads(path.read_text())
            matched, total = _restore_windows(layout)
            return {
                "ok": matched > 0,
                "action": "restore",
                "name": name,
                "matched": matched,
                "total": total,
            }

        elif action == "list":
            available = [f.stem for f in _layouts_dir().glob("*.json")]
            return {
                "ok": True,
                "action": "list",
                "layouts": available,
                "count": len(available),
            }

        return {"ok": False, "reason": f"unknown action '{action}'. Use save, restore, or list."}

    return Tool(
        name="window_layout",
        description=(
            "Save and restore window arrangements. "
            "action=save + name to snapshot current layout. "
            "action=restore + name to move windows back. "
            "action=list to show saved layouts."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["save", "restore", "list"]},
                "name": {"type": "string", "maxLength": 64},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )
