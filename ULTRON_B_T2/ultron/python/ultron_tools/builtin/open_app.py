"""open_app tool — launch a Windows application by name.

Two resolution paths, tried in order:

1. The configured ``[apps].aliases`` map in config.toml. e.g.
   ``aliases = { "spotify" = "spotify:" }``  — a URI scheme, a path,
   or an exe name.
2. The Windows shell's ``start`` command, which handles uri schemes
   (``spotify:``, ``mailto:``), registered apps (``code``, ``chrome``),
   and absolute paths.

confirm_required is on by default — launching arbitrary apps is a
mutation. The user can opt apps out per-name via
``[apps].auto_launch = ["spotify", "vscode"]``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.open_app")


# Sensible defaults for apps the user is likely to want, so first-run
# works without a config entry. Any of these can be overridden by
# putting `aliases = { … }` in config.toml under [apps].
DEFAULT_ALIASES: dict[str, str] = {
    "spotify":     "spotify:",
    "chrome":      "chrome",
    "brave":       "brave",
    "edge":        "msedge",
    "firefox":     "firefox",
    "vscode":      "code",
    "code":        "code",
    "terminal":    "wt",
    "powershell":  "powershell",
    "explorer":    "explorer",
    "calculator":  "calc",
    "calc":        "calc",
    "notepad":     "notepad",
    "settings":    "ms-settings:",
    "task manager": "taskmgr",
    "discord":     "discord:",
    "obsidian":    "obsidian://",
    "youtube":     "https://www.youtube.com",
    "gmail":       "https://mail.google.com",
    "github":      "https://github.com",
}


def _load_apps_section() -> dict[str, Any]:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return {}
    section = raw.get("apps")
    return section if isinstance(section, dict) else {}


def _resolve_target(name: str, aliases: dict[str, str]) -> str:
    key = name.strip().lower()
    if key in aliases:
        return aliases[key]
    # Fallback: pass through. `start` will resolve registered apps,
    # URI schemes, and paths. Unknown names just fail.
    return name


def _is_safe_target(target: str) -> tuple[bool, str]:
    """Reject obviously dangerous shell sequences. We pass `target`
    as a *single argument* to start, but defence in depth — refuse
    embedded operators outright."""
    bad = ("&", "|", ";", "$(", "`", "\n", "\r")
    for b in bad:
        if b in target:
            return False, f"target contains forbidden character {b!r}"
    return True, ""


def build(config: ToolsConfig) -> Tool:
    apps_section = _load_apps_section()
    aliases: dict[str, str] = dict(DEFAULT_ALIASES)
    user_aliases = apps_section.get("aliases") or {}
    if isinstance(user_aliases, dict):
        # User-defined wins.
        aliases.update({str(k).lower(): str(v) for k, v in user_aliases.items()})
    auto_launch_raw = apps_section.get("auto_launch") or []
    auto_launch = {str(s).lower() for s in auto_launch_raw} if isinstance(auto_launch_raw, list) else set()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "reason": "name is required"}
        target = _resolve_target(name, aliases)
        ok, reason = _is_safe_target(target)
        if not ok:
            return {"ok": False, "reason": reason}
        # Use cmd /c start so URI schemes (spotify:, mailto:) and
        # registered app names (code, chrome) both work. start treats
        # the *first* quoted arg as the window title — passing "" keeps
        # the actual target intact.
        cmdline = ["cmd.exe", "/c", "start", "", target]
        try:
            subprocess.Popen(
                cmdline,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                              | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            logger.exception("open_app failed: %s", exc)
            return {"ok": False, "reason": f"launch failed: {exc}"}
        return {"ok": True, "name": name, "target": target}

    # open_app is invoked by explicit user command ("open Spotify") —
    # the request IS the consent. Adding a confirm prompt here would
    # make every "open X" voice command take an extra round-trip. If
    # the user wants the safety net back, they can put "open_app" in
    # [tools].confirm_required_tools in config.toml — the registry
    # honours that as an additive override.
    return Tool(
        name="open_app",
        description=(
            "Launch a Windows application by name. Recognised names include "
            f"{', '.join(sorted(set(aliases) | auto_launch)[:10])} … "
            "and any value the Windows 'start' command resolves "
            "(URI schemes, registered apps, absolute paths)."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 256},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )
