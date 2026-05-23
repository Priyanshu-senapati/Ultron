"""close_app tool -- close/kill a running Windows application by name.

Resolution:
  1. Check a built-in name-to-process mapping (e.g. "spotify" -> "Spotify.exe").
  2. Fall back to substring match against running process names.
  3. Use taskkill to close -- graceful first, force on request.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.close_app")

_NAME_TO_PROCESS: dict[str, list[str]] = {
    "spotify":       ["Spotify.exe"],
    "chrome":        ["chrome.exe"],
    "brave":         ["brave.exe"],
    "edge":          ["msedge.exe"],
    "firefox":       ["firefox.exe"],
    "vscode":        ["Code.exe"],
    "code":          ["Code.exe"],
    "visual studio code": ["Code.exe"],
    "discord":       ["Discord.exe"],
    "notepad":       ["notepad.exe"],
    "calculator":    ["CalculatorApp.exe"],
    "calc":          ["CalculatorApp.exe"],
    "terminal":      ["WindowsTerminal.exe"],
    "powershell":    ["powershell.exe"],
    "task manager":  ["Taskmgr.exe"],
    "obs":           ["obs64.exe", "obs32.exe"],
    "vlc":           ["vlc.exe"],
    "steam":         ["steam.exe"],
    "word":          ["WINWORD.EXE"],
    "excel":         ["EXCEL.EXE"],
    "powerpoint":    ["POWERPNT.EXE"],
    "teams":         ["ms-teams.exe", "Teams.exe"],
    "outlook":       ["OUTLOOK.EXE"],
    "whatsapp":      ["WhatsApp.exe"],
    "telegram":      ["Telegram.exe"],
    "slack":         ["slack.exe"],
    "paint":         ["mspaint.exe"],
    "snipping tool": ["SnippingTool.exe"],
    "clock":         ["Time.exe"],
    "photos":        ["Microsoft.Photos.exe"],
    "file explorer":  ["explorer.exe"],
    "files":         ["explorer.exe"],
    "zoom":          ["Zoom.exe"],
}

_PROTECTED = {"explorer.exe", "csrss.exe", "svchost.exe",
              "lsass.exe", "winlogon.exe", "System"}


def _find_running(name: str) -> list[dict[str, Any]]:
    """Find running processes matching the given name."""
    if sys.platform != "win32":
        return []

    key = name.strip().lower()
    targets: list[str] = []

    if key in _NAME_TO_PROCESS:
        targets = _NAME_TO_PROCESS[key]
    else:
        targets = [f"{key}.exe"]

    found: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        for line in (result.stdout or "").splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) < 2:
                continue
            proc_name = parts[0].strip('"')
            pid = parts[1].strip('"')
            proc_lower = proc_name.lower()
            for t in targets:
                if proc_lower == t.lower():
                    found.append({"name": proc_name, "pid": int(pid)})
                    break
            else:
                if key in proc_lower and proc_name not in _PROTECTED:
                    found.append({"name": proc_name, "pid": int(pid)})
    except Exception as exc:
        logger.warning("tasklist failed: %s", exc)

    return found


def _kill_processes(proc_name: str, force: bool) -> tuple[bool, str]:
    """Kill all processes with the given image name."""
    args = ["taskkill", "/IM", proc_name]
    if force:
        args.append("/F")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode == 0:
            return True, output or f"closed {proc_name}"
        return False, err or output or f"taskkill returned {result.returncode}"
    except Exception as exc:
        return False, f"taskkill failed: {exc}"


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "reason": "name is required"}

        force = bool(args.get("force", False))

        running = _find_running(name)
        if not running:
            return {
                "ok": False,
                "reason": f"no running process found matching '{name}'",
            }

        proc_names_seen: set[str] = set()
        killed: list[str] = []
        failed: list[str] = []

        for proc in running:
            pname = proc["name"]
            if pname.lower() in {p.lower() for p in _PROTECTED}:
                failed.append(f"{pname}: protected system process")
                continue
            if pname in proc_names_seen:
                continue
            proc_names_seen.add(pname)
            ok, msg = _kill_processes(pname, force)
            if ok:
                killed.append(pname)
            else:
                failed.append(f"{pname}: {msg}")

        if killed:
            return {
                "ok": True,
                "closed": killed,
                "failed": failed or None,
                "force": force,
                "matched_processes": len(running),
            }
        return {
            "ok": False,
            "reason": "; ".join(failed) if failed else f"could not close '{name}'",
            "matched_processes": len(running),
        }

    return Tool(
        name="close_app",
        description=(
            "Close a running Windows application by name. "
            "Sends a graceful close by default; pass force=true to force-kill. "
            "Works with common names like 'spotify', 'chrome', 'discord', "
            "'vscode', 'notepad', etc."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 256},
                "force": {"type": "boolean"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )
