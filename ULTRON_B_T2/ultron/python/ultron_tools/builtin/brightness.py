"""brightness tool — get / set display brightness on Windows.

Uses the WMI WmiMonitorBrightnessMethods class via PowerShell. Works on
laptops with integrated displays; many external monitors don't expose
DDC/CI via Windows and will return an error.

Three actions:
- ``get``  → returns the current brightness level (0-100)
- ``set``  → sets brightness to ``level`` (0-100)
- ``up`` / ``down`` → relative adjustment by ``step`` (default 10)
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def _ps(cmd: str, *, timeout: float = 5.0) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _get_brightness() -> int | None:
    code, out, _ = _ps(
        "(Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightness -ErrorAction Stop).CurrentBrightness"
    )
    if code != 0 or not out:
        return None
    try:
        # Multi-monitor: PowerShell prints one int per line; take the first.
        return int(out.splitlines()[0].strip())
    except (ValueError, IndexError):
        return None


def _set_brightness(level: int) -> bool:
    """Set monitor brightness via WMI.

    Get-CimInstance returns CimInstance objects that don't expose
    methods directly — you have to use Invoke-CimMethod. The older
    Get-WmiObject path supports direct method calls but is deprecated
    in PowerShell 7+. We use Invoke-CimMethod for forward-compat.
    """
    level = max(0, min(100, int(level)))
    code, _, err = _ps(
        "$ErrorActionPreference='Stop'; "
        "Invoke-CimMethod -InputObject (Get-CimInstance -Namespace root/WMI "
        "-ClassName WmiMonitorBrightnessMethods) "
        f"-MethodName WmiSetBrightness -Arguments @{{Brightness=[byte]{level};Timeout=[uint32]1}} | Out-Null"
    )
    return code == 0 and not err


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if sys.platform != "win32":
            return {"ok": False, "reason": "brightness only supported on Windows"}
        action = str(args.get("action", "")).strip().lower()
        if action == "get":
            level = _get_brightness()
            if level is None:
                return {"ok": False, "reason": "could not read brightness "
                                               "(external monitor without DDC/CI?)"}
            return {"ok": True, "level": level}
        if action == "set":
            if "level" not in args:
                return {"ok": False, "reason": "level is required for action=set"}
            level = int(args["level"])
            if not 0 <= level <= 100:
                return {"ok": False, "reason": "level must be 0..100"}
            if not _set_brightness(level):
                return {"ok": False, "reason": "WmiSetBrightness failed"}
            return {"ok": True, "level": level}
        if action in ("up", "down"):
            step = int(args.get("step", 10))
            if not 1 <= step <= 50:
                return {"ok": False, "reason": "step must be 1..50"}
            current = _get_brightness()
            if current is None:
                return {"ok": False, "reason": "could not read current brightness"}
            target = current + (step if action == "up" else -step)
            target = max(0, min(100, target))
            if not _set_brightness(target):
                return {"ok": False, "reason": "WmiSetBrightness failed"}
            return {"ok": True, "level": target, "from": current}
        return {"ok": False, "reason": f"unknown action {action!r}"}

    return Tool(
        name="brightness",
        description=(
            "Get or change display brightness (0-100). actions: "
            "get, set (with level), up (with optional step), down "
            "(with optional step). Works on laptop screens; some "
            "external monitors don't expose this via WMI."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "up", "down"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "step":  {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )
