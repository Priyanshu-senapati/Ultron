"""ULTRON live HUD.

A persistent terminal that subscribes to the bus and shows, in real time:

  - Every voice event (HEARD / WAKE / HOTKEY / PROCESSING / SPEAKING /
    ULTRON / IDLE) as a scrolling log
  - A live mic-level bar while LISTENING, redrawn in place
  - An insights line from the H/D fusion pipeline (cognitive load,
    circadian phase, focus app, visual label)
  - A persistent state indicator pinned to the bottom

Opens automatically with `ultron start`. Closing it (Ctrl+C) doesn't
affect the running stack -- it's pure read-only on the bus.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

# Force ANSI processing on legacy Windows consoles.
if sys.platform == "win32":
    try:
        import ctypes  # type: ignore[import-untyped]
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004; also keep existing flags.
        for handle_id in (-11,):  # STD_OUTPUT_HANDLE
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass

IST = ZoneInfo("Asia/Kolkata")

# ANSI escapes
RESET   = "\x1b[0m"
DIM     = "\x1b[2m"
BOLD    = "\x1b[1m"
RED     = "\x1b[31m"
GREEN   = "\x1b[32m"
YELLOW  = "\x1b[33m"
BLUE    = "\x1b[34m"
PURPLE  = "\x1b[35m"
CYAN    = "\x1b[36m"
WHITE   = "\x1b[97m"
GRAY    = "\x1b[90m"

SAVE_CUR    = "\x1b[s"
RESTORE_CUR = "\x1b[u"
CLEAR_LINE  = "\x1b[2K"
HIDE_CUR    = "\x1b[?25l"
SHOW_CUR    = "\x1b[?25h"


def load_bridge() -> tuple[str, str]:
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    return f"ws://{raw['bridge']['bind']}/ws", raw["bridge"]["token"]


def term_size() -> tuple[int, int]:
    sz = shutil.get_terminal_size(fallback=(100, 28))
    return sz.columns, sz.lines


def fmt_time() -> str:
    return f"{DIM}{datetime.now(IST).strftime('%H:%M:%S')}{RESET}"


class HUD:
    """All HUD rendering state in one place.

    The "log area" is the normal scrolling region. The "status line" is
    pinned to the last row using ANSI scroll-region (DECSTBM). The mic
    meter overwrites whatever is currently on the cursor line in the
    log area using \\r when we're mid-meter; otherwise we just print
    new log lines.
    """

    def __init__(self) -> None:
        # Latest known state (drives the bottom status line).
        self.state: str = "idle"
        self.last_state_at: float = time.monotonic()

        # Latest insight snapshot fields.
        self.cognitive_load: float | None = None
        self.tension: float | None = None
        self.circadian: str = ""
        self.focus_app: str = ""
        self.visual_label: str = ""

        # Latest bridge data — rendered into a second pinned line above
        # the main status bar. Empty string means "skip this slot".
        self.spotify_line: str = ""
        self.tab_line: str = ""
        self.calendar_line: str = ""
        self.mail_line: str = ""
        self.app_detail_line: str = ""

        # Mic meter state: when True, the most recent log line is a meter
        # that should be overwritten in place. False = clean newline next.
        self._meter_open: bool = False
        # Throttle: only redraw the meter at most every ~80ms even if
        # events arrive faster.
        self._last_meter_at: float = 0.0

    # ------------------------------------------------------------------
    # Layout: pin a status line to the last row using a scroll region
    # ------------------------------------------------------------------

    def install_layout(self) -> None:
        cols, rows = term_size()
        # Scroll region = rows 1..(rows-2). Bottom two rows reserved:
        #   rows-1 → bridges bar (Spotify / tab / calendar / mail / app)
        #   rows   → main status bar (state / load / focus / IST time)
        sys.stdout.write(f"\x1b[1;{rows - 2}r")
        sys.stdout.write(f"\x1b[{rows - 2};1H")
        sys.stdout.flush()
        self.draw_status()
        self.draw_bridges()

    def teardown_layout(self) -> None:
        # Reset scroll region to full screen and show cursor.
        cols, rows = term_size()
        sys.stdout.write(f"\x1b[1;{rows}r")
        sys.stdout.write(SHOW_CUR)
        sys.stdout.write("\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Bridges bar (pinned, one row above the main status bar)
    # ------------------------------------------------------------------

    def draw_bridges(self) -> None:
        cols, rows = term_size()
        # Compose slots in priority order; drop trailing slots if we'd
        # exceed the terminal width.
        slots: list[str] = []
        if self.spotify_line:
            slots.append(f"{GREEN}♪{RESET} {self.spotify_line}")
        if self.app_detail_line:
            slots.append(f"{PURPLE}▣{RESET} {self.app_detail_line}")
        if self.tab_line:
            slots.append(f"{BLUE}⌬{RESET} {self.tab_line}")
        if self.calendar_line:
            slots.append(f"{YELLOW}◷{RESET} {self.calendar_line}")
        if self.mail_line:
            slots.append(f"{CYAN}✉{RESET} {self.mail_line}")

        sep = f" {DIM}·{RESET} "
        line = sep.join(slots) if slots else f"{DIM}(no integrations active — edit [bridges.*] in config.toml){RESET}"
        # Truncate to terminal width (visible length, ignoring ANSI).
        # Simple approach: cap raw chars; ANSI codes are short.
        if len(self._strip_ansi(line)) > cols - 2:
            # Trim slot list from the right until it fits.
            while slots and len(self._strip_ansi(sep.join(slots))) > cols - 6:
                slots.pop()
            line = sep.join(slots) + f" {DIM}…{RESET}"

        sys.stdout.write(SAVE_CUR)
        sys.stdout.write(f"\x1b[{rows - 1};1H")
        sys.stdout.write(CLEAR_LINE)
        sys.stdout.write(" " + line)
        sys.stdout.write(RESTORE_CUR)
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Bottom status bar (pinned)
    # ------------------------------------------------------------------

    def draw_status(self) -> None:
        cols, rows = term_size()
        state_color = {
            "idle":       GRAY,
            "listening":  YELLOW,
            "processing": PURPLE,
            "speaking":   CYAN,
            "error":      RED,
        }.get(self.state, WHITE)
        load_s = f"{self.cognitive_load:.2f}" if self.cognitive_load is not None else "--"
        tens_s = f"{self.tension:.2f}" if self.tension is not None else "--"
        circ_s = self.circadian or "--"
        focus_s = (self.focus_app or "--")[:22]
        screen_s = (self.visual_label or "")[: max(0, cols - 80)]
        now_s = datetime.now(IST).strftime("%H:%M:%S IST")
        status = (
            f" {BOLD}{state_color}{self.state.upper():<10}{RESET}"
            f" {DIM}load{RESET} {load_s}"
            f" {DIM}tension{RESET} {tens_s}"
            f" {DIM}phase{RESET} {circ_s}"
            f" {DIM}focus{RESET} {focus_s}"
        )
        if screen_s:
            status += f" {DIM}|{RESET} {screen_s}"
        # Pad with spaces so leftover characters from a longer prior line
        # don't bleed through.
        visible_len = len(self._strip_ansi(status))
        pad = max(0, cols - visible_len - len(now_s) - 1)
        right = f"{DIM}{now_s}{RESET}"

        sys.stdout.write(SAVE_CUR)
        sys.stdout.write(f"\x1b[{rows};1H")
        sys.stdout.write(CLEAR_LINE)
        sys.stdout.write(status + (" " * pad) + right)
        sys.stdout.write(RESTORE_CUR)
        sys.stdout.flush()

    @staticmethod
    def _strip_ansi(s: str) -> str:
        out, i = [], 0
        while i < len(s):
            if s[i] == "\x1b":
                while i < len(s) and s[i] not in "mHKsuABCDEFGJ":
                    i += 1
                i += 1
            else:
                out.append(s[i])
                i += 1
        return "".join(out)

    # ------------------------------------------------------------------
    # Log lines (scrolling area)
    # ------------------------------------------------------------------

    def log(self, tag: str, color: str, text: str) -> None:
        self._end_meter()
        sys.stdout.write(f"{fmt_time()} {color}{BOLD}{tag:<11}{RESET} {text}\n")
        sys.stdout.flush()
        self.draw_status()
        self.draw_bridges()

    def _end_meter(self) -> None:
        if self._meter_open:
            sys.stdout.write("\n")
            self._meter_open = False

    def draw_meter(self, peak: float) -> None:
        # Throttle to 80ms.
        now = time.monotonic()
        if now - self._last_meter_at < 0.08:
            return
        self._last_meter_at = now

        cols, _ = term_size()
        width = max(10, min(40, cols - 30))
        filled = min(width, int(peak * width * 3.0))  # x3 = visual gain
        bar = ("█" * filled).ljust(width, "░")
        # Color by amplitude: <0.05 dim, <0.2 yellow, else green.
        if peak < 0.05:
            color = DIM
        elif peak < 0.20:
            color = YELLOW
        else:
            color = GREEN
        line = (
            f"{fmt_time()} {color}{BOLD}{'MIC':<11}{RESET} "
            f"{color}{bar}{RESET} peak {peak:.3f}"
        )
        # Overwrite the same line in place via \r + clear.
        sys.stdout.write("\r" + CLEAR_LINE + line)
        sys.stdout.flush()
        self._meter_open = True


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


async def run() -> None:
    url, token = load_bridge()
    hud = HUD()

    # Print a banner above the scroll region.
    sys.stdout.write("\x1b[2J\x1b[H")  # clear screen + home
    sys.stdout.write(f"{BOLD}{CYAN}========================================={RESET}\n")
    sys.stdout.write(f"{BOLD}{CYAN}        ULTRON live HUD{RESET}\n")
    sys.stdout.write(f"{BOLD}{CYAN}========================================={RESET}\n")
    sys.stdout.write(f"{DIM}Press Ctrl+C to close. The stack keeps running.{RESET}\n\n")
    sys.stdout.flush()

    hud.install_layout()
    hud.log("CONNECT", CYAN, url)

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "hud"}))
        welcome = json.loads(await ws.recv())
        if welcome.get("op") != "welcome":
            hud.log("ERROR", RED, f"handshake failed: {welcome}")
            return
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": [
                "voice_transcript",
                "voice_state_changed",
                "voice_mic_level",
                "llm_response",
                "wake_listener_heard",
                "insight_snapshot",
                "visual_label",
                # Integrations sidecar
                "spotify_now_playing",
                "browser_tab",
                "gh_activity",
                "calendar_upcoming",
                "gmail_unread",
                "app_detail",
            ],
        }))
        hud.log("READY", GREEN, "subscribed. listening for events...")

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("op") != "event":
                continue
            kind = msg.get("kind", "")
            p = msg.get("payload") or {}

            if kind == "voice_mic_level":
                # Only show the meter while LISTENING -- otherwise it's
                # the wake listener's background tap, which would just
                # be noise on the HUD.
                if hud.state == "listening":
                    hud.draw_meter(float(p.get("peak") or 0.0))

            elif kind == "wake_listener_heard":
                text = p.get("text", "")
                matched = bool(p.get("matched"))
                if matched:
                    hud.log("WAKE", GREEN, f"{WHITE}{text!r}{RESET}")
                else:
                    hud.log("HEARD", DIM, f"{WHITE}{text!r}{RESET}")

            elif kind == "voice_transcript":
                act = p.get("activation", "?")
                text = p.get("text", "")
                if act == "wake_word":
                    hud.log("WAKE", GREEN, f"-> {WHITE}{text!r}{RESET}")
                elif act == "hotkey":
                    hud.log("HOTKEY", YELLOW, f"-> {WHITE}{text!r}{RESET}")
                else:
                    hud.log("INPUT", BLUE, f"{act}: {WHITE}{text!r}{RESET}")

            elif kind == "voice_state_changed":
                frm = p.get("prev_state") or p.get("from", "?")
                to = p.get("state") or p.get("to", "?")
                reason = p.get("activation") or p.get("reason", "")
                hud.state = to
                hud.last_state_at = time.monotonic()
                if to == "processing":
                    hud.log("PROCESSING", PURPLE, f"{DIM}({reason}){RESET}")
                elif to == "speaking":
                    hud.log("SPEAKING", CYAN, f"{DIM}TTS playing...{RESET}")
                elif to == "idle":
                    hud.log("IDLE", DIM, f"{reason}")
                elif to == "listening":
                    hud.log("LISTENING", YELLOW, f"{DIM}({reason}){RESET}")
                elif to == "error":
                    hud.log("ERROR", RED, f"{reason}")
                else:
                    hud.log("STATE", DIM, f"{frm} -> {to}")

            elif kind == "llm_response":
                text = (p.get("text") or "").strip()
                shard = p.get("shard", "?")
                err = p.get("error", False)
                color = RED if err else GREEN
                tag = "ULTRON-ERR" if err else "ULTRON"
                wrapped = text.replace("\n", "\n" + " " * 21)
                hud.log(tag, color, f"{DIM}[{shard}]{RESET} {WHITE}{wrapped}{RESET}")

            elif kind == "insight_snapshot":
                hud.cognitive_load = p.get("cognitive_load")
                hud.tension = p.get("tension")
                hud.circadian = p.get("circadian_phase") or p.get("phase") or ""
                hud.focus_app = p.get("focus_app") or p.get("active_app") or ""
                hud.draw_status()

            elif kind == "visual_label":
                label = (p.get("label") or "").strip()
                if label:
                    hud.visual_label = label
                    hud.log("SCREEN", BLUE, f"{WHITE}{label}{RESET}")

            elif kind == "spotify_now_playing":
                if p.get("is_playing"):
                    track = (p.get("track") or "").strip()
                    artist = (p.get("artist") or "").strip()
                    hud.spotify_line = f"{track} — {artist}" if track else ""
                    if track:
                        hud.log("SPOTIFY", GREEN, f"{WHITE}{track}{RESET} — {DIM}{artist}{RESET}")
                else:
                    hud.spotify_line = ""
                    hud.log("SPOTIFY", DIM, "paused / nothing playing")
                hud.draw_bridges()

            elif kind == "browser_tab":
                title = (p.get("title") or "").strip()
                url = (p.get("url") or "").strip()
                hud.tab_line = title[:50] if title else url[:50]
                if title or url:
                    hud.log("TAB", BLUE, f"{WHITE}{title}{RESET} {DIM}{url}{RESET}")
                hud.draw_bridges()

            elif kind == "gh_activity":
                unread = int(p.get("unread_count", 0) or 0)
                events = p.get("events") or []
                if events:
                    top = events[0]
                    hud.log(
                        "GH",
                        WHITE,
                        f"{top.get('summary','')} {DIM}on {top.get('repo','')}{RESET}",
                    )
                if unread:
                    hud.log("GH-MAIL", YELLOW, f"{unread} unread notification(s)")
                hud.draw_bridges()

            elif kind == "calendar_upcoming":
                events = p.get("events") or []
                if events:
                    ev = events[0]
                    summary = (ev.get("summary") or "(no title)")[:40]
                    start = ev.get("start", "")
                    hud.calendar_line = f"next: {summary} @ {start[-8:]}"
                    hud.log("CAL", YELLOW, f"{WHITE}{summary}{RESET} {DIM}@ {start}{RESET}")
                else:
                    hud.calendar_line = ""
                hud.draw_bridges()

            elif kind == "gmail_unread":
                count = int(p.get("count", 0) or 0)
                msgs = p.get("messages") or []
                if count:
                    hud.mail_line = f"{count} unread"
                    subj = (msgs[0].get("subject") if msgs else "") or ""
                    hud.log("MAIL", CYAN, f"{count} unread {DIM}{subj[:60]}{RESET}")
                else:
                    hud.mail_line = ""
                hud.draw_bridges()

            elif kind == "app_detail":
                app = p.get("app", "")
                detail = p.get("detail") or {}
                summary = ""
                if app == "vscode" and detail.get("file"):
                    summary = f"VSCode: {detail['file']}"
                elif app == "jetbrains" and detail.get("file"):
                    summary = f"{detail.get('ide','IDE')}: {detail['file']}"
                elif app == "discord":
                    if detail.get("kind") == "guild":
                        summary = f"Discord: #{detail.get('channel','')}@{detail.get('server','')}"
                    elif detail.get("kind") == "dm":
                        summary = f"Discord DM: {detail.get('friend','')}"
                elif app == "office" and detail.get("document"):
                    summary = f"{detail.get('app','Office')}: {detail['document']}"
                hud.app_detail_line = summary[:50]
                hud.draw_bridges()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{DIM}HUD closed. ULTRON stack still running.{RESET}\n")
    except Exception as exc:
        sys.stdout.write(f"\n{RED}HUD crashed: {exc}{RESET}\n")
        sys.exit(1)
    finally:
        # Best-effort: restore full-screen scroll region.
        try:
            _, rows = term_size()
            sys.stdout.write(f"\x1b[1;{rows}r{SHOW_CUR}\n")
            sys.stdout.flush()
        except Exception:
            pass
