"""Per-app deep state bridge.

The Rust `ultron-core` window tracker already knows the *focused process*
(`focus_app`), but that's just an executable name. Users care about
*what's happening inside* the app: which file in VS Code, which Discord
channel, which document in Word, which Spotify track.

This bridge polls Windows for the foreground window's title via ctypes
(no pywin32 dependency) and runs a small library of per-app title
parsers to extract structured state. It emits `app_detail` events
whenever the structured state changes.

Per-app providers (all currently title-based — cheap and zero-auth):
  - VS Code:     extracts active file + folder from title
  - Discord:     extracts channel + server from title
  - JetBrains:   IntelliJ/PyCharm/etc. — project + file
  - Chrome/Edge: page title (until the browser extension fills it richer)
  - Word/Excel/PowerPoint: document name
  - Generic: returns the raw title for unknown apps

Future providers (deeper than titles):
  - VS Code via Code's IPC pipe (`\\.\pipe\vscode-ipc-<hash>`)
  - Discord via the official RPC socket (`\\.\pipe\discord-ipc-0`)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from .base import Bridge, BridgePublishFn
from .config import AppDetailConfig

logger = logging.getLogger("ultron.bridges.app_detail")


# --------------------------------------------------------------------------- #
# Win32 plumbing
# --------------------------------------------------------------------------- #


_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None
_psapi = ctypes.windll.psapi if hasattr(ctypes, "windll") else None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ULTRON's own processes — we never want to report these as the user's
# "focus app" because they ARE ULTRON. When the foreground PID matches
# one of these scripts, we skip the publish and the LiveState retains
# the previous *real* application.
_ULTRON_SCRIPT_HINTS = (
    "voice_engine.py",
    "llm_service.py",
    "insight_pulse.py",
    "bridges_service.py",
    "hud.py",
    "repl.py",
    "kg_indexer.py",
    "ultron-core",
    "ultron-insight-pulse",
    "ultron-memory-engine",
    "ultron-ghost",
)


def _process_cmdline(pid: int) -> str:
    """Best-effort read of a process's command line via psutil if available.

    Returns "" if we can't tell. We use this to detect ULTRON's own
    python.exe / powershell.exe windows so we don't claim the user is
    "focusing on" ULTRON itself when they ask what they're working on.
    """
    try:
        import psutil  # type: ignore[import-not-found]
        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return ""


def _foreground_title_and_exe() -> tuple[str, str, int]:
    """Return (window_title, exe_basename, pid) for the foreground window.

    Returns ("", "", 0) on non-Windows or if any API call fails — the
    bridge treats this as "no info" and won't publish.
    """
    if _user32 is None or _kernel32 is None:
        return "", "", 0

    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "", "", 0

    # Title
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""

    # PID
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    # Exe basename via QueryFullProcessImageNameW
    exe = ""
    h_proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h_proc:
        try:
            buf_size = wt.DWORD(260)
            path_buf = ctypes.create_unicode_buffer(buf_size.value)
            if _kernel32.QueryFullProcessImageNameW(
                h_proc, 0, path_buf, ctypes.byref(buf_size)
            ):
                full = path_buf.value or ""
                exe = full.rsplit("\\", 1)[-1] if full else ""
        finally:
            _kernel32.CloseHandle(h_proc)

    return title, exe, int(pid.value)


def _is_ultron_window(title: str, exe: str, pid: int) -> bool:
    """True if this foreground window belongs to ULTRON itself.

    Two heuristics: title contains "ULTRON" (case-insensitive), or the
    process's command line contains one of our script names. The cmdline
    check catches our python.exe / powershell.exe windows even when their
    title is generic (just the exe path).
    """
    low_title = title.lower()
    if "ultron" in low_title:
        return True
    low_exe = exe.lower()
    if low_exe in {"python.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe", "cmd.exe"}:
        cmdline = _process_cmdline(pid).lower()
        if cmdline and any(hint.lower() in cmdline for hint in _ULTRON_SCRIPT_HINTS):
            return True
    # Native Rust sidecar exes — ultron-core.exe etc.
    if any(hint in low_exe for hint in ("ultron-core", "ultron-insight", "ultron-memory", "ultron-ghost")):
        return True
    return False


# --------------------------------------------------------------------------- #
# Per-app parsers
# --------------------------------------------------------------------------- #


@dataclass
class AppDetail:
    app: str                    # short canonical name: "vscode", "discord", ...
    exe: str                    # the executable basename, lowercased
    title: str                  # raw window title (truncated)
    detail: dict[str, Any]      # parsed structured fields, varies by app

    def signature(self) -> str:
        """Stable key for "did anything change" deduping."""
        return f"{self.app}|{self.exe}|{self.title}"


# Each parser returns the `detail` dict for its app, or None to fall
# through. They run in registration order; the first to claim the
# title wins.

_VSCODE_RE = re.compile(r"^(?P<dirty>[●·]\s*)?(?P<file>.+?)\s+-\s+(?P<folder>.+?)\s+-\s+Visual Studio Code$")
_DISCORD_RE = re.compile(r"^(?P<channel>[^|]+)\s*\|\s*(?P<server>[^-]+?)\s*-\s*Discord$")
_DISCORD_DM_RE = re.compile(r"^(?P<friend>[^-]+?)\s*-\s*Discord$")
_JETBRAINS_RE = re.compile(r"^(?P<project>[^\[]+?)\s*\[(?P<path>[^\]]+)\]\s*-\s*(?P<file>.+?)\s*-\s*(?P<ide>IntelliJ IDEA|PyCharm|WebStorm|RustRover|GoLand|CLion|Rider)")
_OFFICE_RE = re.compile(r"^(?P<doc>.+?)\s*-\s*(?P<app>Word|Excel|PowerPoint|OneNote)(\s|$)")
_CHROME_TAIL = re.compile(r"\s*-\s*(Google Chrome|Microsoft.\s?Edge)$")


def _parse_vscode(title: str, exe: str) -> Optional[dict[str, Any]]:
    if "code" not in exe.lower():
        return None
    m = _VSCODE_RE.match(title)
    if not m:
        return None
    return {
        "file": m.group("file"),
        "folder": m.group("folder"),
        "dirty": bool(m.group("dirty")),
    }


def _parse_discord(title: str, exe: str) -> Optional[dict[str, Any]]:
    if "discord" not in exe.lower():
        return None
    m = _DISCORD_RE.match(title)
    if m:
        return {
            "channel": m.group("channel").strip().lstrip("#"),
            "server": m.group("server").strip(),
            "kind": "guild",
        }
    m = _DISCORD_DM_RE.match(title)
    if m:
        return {"friend": m.group("friend").strip(), "kind": "dm"}
    return {"kind": "other"}


def _parse_jetbrains(title: str, exe: str) -> Optional[dict[str, Any]]:
    m = _JETBRAINS_RE.match(title)
    if not m:
        return None
    return {
        "ide": m.group("ide"),
        "project": m.group("project").strip(),
        "path": m.group("path"),
        "file": m.group("file").strip(),
    }


def _parse_office(title: str, exe: str) -> Optional[dict[str, Any]]:
    m = _OFFICE_RE.match(title)
    if not m:
        return None
    return {"document": m.group("doc").strip(), "app": m.group("app")}


def _parse_browser(title: str, exe: str) -> Optional[dict[str, Any]]:
    low = exe.lower()
    if "chrome" not in low and "msedge" not in low and "edge" not in low:
        return None
    page = _CHROME_TAIL.sub("", title).strip()
    return {"page_title": page}


def _parse_spotify(title: str, exe: str) -> Optional[dict[str, Any]]:
    if "spotify" not in exe.lower():
        return None
    # Spotify shows "Track - Artist" in the title when playing, or just
    # "Spotify Free" / "Spotify Premium" otherwise. The Spotify bridge
    # gives richer data; we only emit a hint here.
    if " - " in title and title.lower() != "spotify free":
        track, _, artist = title.partition(" - ")
        return {"track_hint": track.strip(), "artist_hint": artist.strip()}
    return {"idle": True}


# Order matters: more specific first.
_PARSERS = [
    ("vscode", _parse_vscode),
    ("jetbrains", _parse_jetbrains),
    ("discord", _parse_discord),
    ("office", _parse_office),
    ("spotify", _parse_spotify),
    ("browser", _parse_browser),
]


def parse(title: str, exe: str) -> AppDetail:
    """Apply parsers in order; return AppDetail with structured state."""
    for app, parser in _PARSERS:
        result = parser(title, exe)
        if result is not None:
            return AppDetail(app=app, exe=exe.lower(), title=title[:200], detail=result)
    return AppDetail(app="generic", exe=exe.lower(), title=title[:200], detail={})


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #


class AppDetailBridge(Bridge):
    name = "app_detail"

    def __init__(self, publish: BridgePublishFn | None, cfg: AppDetailConfig) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self._last_sig: Optional[str] = None
        # Filter map — disable specific providers if the user opts out.
        self._enabled_app: dict[str, bool] = {
            "vscode": cfg.vscode,
            "jetbrains": cfg.vscode,    # share the toggle with VS Code (it's "code editors")
            "discord": cfg.discord,
            "office": cfg.generic,
            "spotify": cfg.generic,
            "browser": cfg.generic,
            "generic": cfg.generic,
        }

    async def run(self) -> None:
        if _user32 is None:
            self.log.warning("Win32 user32 not available (non-Windows?) — bridge idling")
            await self._stop_event.wait()
            return
        while not self._stop_event.is_set():
            await self._tick()
            if not await self.sleep(self.cfg.poll_secs):
                return

    async def _tick(self) -> None:
        title, exe, pid = _foreground_title_and_exe()
        if not title and not exe:
            return
        # Skip ULTRON's own terminals/sidecars — we don't want to claim the
        # user is "focused on" ULTRON itself. The previously-published
        # app_detail stays in state until they switch to a real app.
        if _is_ultron_window(title, exe, pid):
            self.log.debug("skipping ULTRON window: %s (%s)", title[:60], exe)
            return
        det = parse(title, exe)
        if not self._enabled_app.get(det.app, True):
            return
        sig = det.signature()
        if sig == self._last_sig:
            return
        self._last_sig = sig
        await self.publish(
            "app_detail",
            {
                "app": det.app,
                "exe": det.exe,
                "title": det.title,
                "detail": det.detail,
                "ts_unix_ms": int(time.time() * 1000),
            },
        )
        self.log.info("app_detail: %s — %s", det.app, det.title[:120])


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False
