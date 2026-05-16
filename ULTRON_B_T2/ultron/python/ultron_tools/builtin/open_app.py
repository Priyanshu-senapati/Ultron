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
    embedded operators outright. AppsFolder paths are allowed even
    though they contain backslashes / exclamation marks."""
    bad = ("&", "|", ";", "$(", "`", "\n", "\r")
    for b in bad:
        if b in target:
            return False, f"target contains forbidden character {b!r}"
    return True, ""


# Cache for Get-StartApps results. Populated on first launch attempt
# (Get-StartApps takes 1-2s; we don't want to pay that per call).
_start_apps_cache: dict[str, str] | None = None


def _refresh_start_apps_cache() -> dict[str, str]:
    """Map lowercase app *display name* → AUMID via Get-StartApps.

    Get-StartApps enumerates every launchable entry on the Start menu
    — Microsoft Store apps, classic apps, even some web shortcuts. Its
    AppIDs work as `explorer.exe shell:AppsFolder\\<AppID>` for *any*
    of them. That single trick unifies launching across app kinds.
    """
    global _start_apps_cache
    out: dict[str, str] = {}
    if sys.platform != "win32":
        _start_apps_cache = out
        return out
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command",
             "Get-StartApps | ForEach-Object { \"$($_.Name)|$($_.AppID)\" }"],
            capture_output=True, text=True, timeout=8,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            name, _, app_id = line.partition("|")
            name = name.strip().lower()
            app_id = app_id.strip()
            if name and app_id and name not in out:
                out[name] = app_id
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Get-StartApps failed: %s", exc)
    _start_apps_cache = out
    return out


def _appsfolder_for(name: str) -> str | None:
    """Return ``shell:AppsFolder\\<AppID>`` for ``name`` if installed."""
    cache = _start_apps_cache if _start_apps_cache is not None else _refresh_start_apps_cache()
    key = name.strip().lower()
    # Exact match wins.
    if key in cache:
        return f"shell:AppsFolder\\{cache[key]}"
    # Partial match — pick the shortest display name that contains the
    # query (so "spotify" matches "Spotify" not "Spotify Web Player").
    candidates = [n for n in cache if key in n]
    if candidates:
        best = min(candidates, key=len)
        return f"shell:AppsFolder\\{cache[best]}"
    return None


def build(config: ToolsConfig) -> Tool:
    apps_section = _load_apps_section()
    aliases: dict[str, str] = dict(DEFAULT_ALIASES)
    raw_user_aliases = apps_section.get("aliases") or {}
    user_aliases: dict[str, str] = {}
    if isinstance(raw_user_aliases, dict):
        user_aliases = {str(k).lower(): str(v) for k, v in raw_user_aliases.items()}
        # Merge into the combined alias map too, so descriptor/output
        # still mentions everything the user configured.
        aliases.update(user_aliases)
    auto_launch_raw = apps_section.get("auto_launch") or []
    auto_launch = {str(s).lower() for s in auto_launch_raw} if isinstance(auto_launch_raw, list) else set()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "reason": "name is required"}

        # Resolution priority:
        # 1. User-defined alias (config.toml [apps].aliases) — explicit
        #    user overrides always win.
        # 2. Start-menu AppsFolder match — handles Microsoft Store apps
        #    like Spotify AND classic apps that register themselves on
        #    the Start menu. This is the most reliable single path.
        # 3. Built-in default alias (URI schemes for known apps).
        # 4. Literal name → `cmd /c start`.
        key = name.strip().lower()
        user_target = user_aliases.get(key) if isinstance(user_aliases, dict) else None
        if user_target:
            target = str(user_target)
        else:
            appsfolder = _appsfolder_for(name)
            if appsfolder is not None:
                target = appsfolder
            else:
                target = _resolve_target(name, aliases)

        ok, reason = _is_safe_target(target)
        if not ok:
            return {"ok": False, "reason": reason}

        try:
            if target.startswith("shell:AppsFolder\\"):
                # AppsFolder paths only launch via explorer.exe.
                cmdline = ["explorer.exe", target]
            else:
                # Classic path: cmd /c start handles URI schemes,
                # registered apps, and absolute paths. The "" arg is
                # the window title placeholder so the actual target
                # isn't interpreted as one.
                cmdline = ["cmd.exe", "/c", "start", "", target]
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
        return {
            "ok": True,
            "name": name,
            "target": target,
            "via": "appsfolder" if target.startswith("shell:") else "start",
        }

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
