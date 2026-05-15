"""
ULTRON v5.1 — Python WebSocket bridge test client.

Phase 1 Module H additions:
- Pretty-prints `input_metrics_updated` with WPM, backspace storms, mouse stats.
- Pretty-prints `window_changed` with the foreground app/title.
- Pretty-prints `screenshot_captured` with size + reason.
- Sums event kinds at the end (Ctrl-C) so you can verify volumes look sane.

Usage:
    python -m pip install websockets tomli
    python python/bridge_test.py
    python python/bridge_test.py --filter input_metrics_updated,window_changed
    python python/bridge_test.py --request-screenshot   # fires one capture, exits
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Need: pip install websockets", file=sys.stderr)
    sys.exit(2)

try:
    import tomllib  # 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        print("Need: pip install tomli  (or use Python 3.11+)", file=sys.stderr)
        sys.exit(2)


def config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ULTRON" / "config.toml"
    return Path.home() / ".local" / "share" / "ULTRON" / "config.toml"


def load_bridge() -> tuple[str, str]:
    p = config_path()
    if not p.exists():
        sys.exit(f"config not found at {p}\nRun ultron-core once to bootstrap.")
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    return cfg["bridge"]["bind"], cfg["bridge"]["token"]


# -- Pretty-printers per event kind ----------------------------------------


def _ts_short(iso: str) -> str:
    """Trim the ISO timestamp to HH:MM:SS for terminal density."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso[:19]


def fmt_heartbeat(p: dict) -> str:
    return f"heartbeat  tension={p.get('tension', 0):.3f} uptime={p.get('uptime_secs', 0)}s"


def fmt_input_activity(p: dict) -> str:
    kind = p.get("kind", "?")
    if kind == "key_event":
        cat = p.get("category", "?")
        dn = "down" if p.get("is_down") else "up"
        return f"input.key      {cat:11} {dn}"
    if kind == "mouse_move":
        return f"input.move     dx={p.get('dx', 0):+4} dy={p.get('dy', 0):+4}"
    if kind == "mouse_button":
        dn = "down" if p.get("is_down") else "up"
        return f"input.button   {p.get('button','?'):6} {dn}"
    if kind == "mouse_scroll":
        return f"input.scroll   {p.get('delta', 0):+}"
    if kind == "idle":
        return f"input.idle     {p.get('secs', 0)}s"
    return f"input          {p}"


def fmt_input_metrics(p: dict) -> str:
    storm = "  STORM" if p.get("backspace_storm") else ""
    return (
        f"metrics    "
        f"wpm={p.get('wpm', 0):5.1f} "
        f"bs/min={p.get('backspace_rate_per_min', 0):4.1f}{storm} "
        f"clicks/min={p.get('click_rate_per_min', 0):4.1f} "
        f"app_sw/min={p.get('app_switch_per_min', 0):4.1f} "
        f"mouse_v={p.get('mouse_velocity_px_per_sec', 0):6.1f}px/s "
        f"hes={p.get('mouse_hesitation_score', 0):.2f} "
        f"rhythm={p.get('typing_rhythm_variance', 0):.2f} "
        f"idle={p.get('idle_secs', 0):.0f}s"
    )


def fmt_window_changed(p: dict) -> str:
    title = (p.get("title") or "").strip() or "(no title)"
    if len(title) > 60:
        title = title[:57] + "..."
    cat = p.get("app_category") or "-"
    return (
        f"window     {p.get('process_name','?'):20} "
        f"[{cat:13}] pid={p.get('pid',0):<6} {title}"
    )


def fmt_screenshot(p: dict) -> str:
    return (
        f"screenshot {p.get('width',0)}x{p.get('height',0)} "
        f"reason={p.get('reason','?')} "
        f"path={p.get('path','?')}"
    )


def fmt_tension_changed(p: dict) -> str:
    prev = p.get("previous", 0)
    curr = p.get("current", 0)
    arrow = "up  " if curr > prev else "down"
    return f"tension    {prev:.3f} {arrow} {curr:.3f}"


def fmt_service_state(p: dict) -> str:
    return f"service    {p.get('state','?')}"


FORMATTERS = {
    "heartbeat": fmt_heartbeat,
    "input_activity": fmt_input_activity,
    "input_metrics_updated": fmt_input_metrics,
    "window_changed": fmt_window_changed,
    "screenshot_captured": fmt_screenshot,
    "tension_changed": fmt_tension_changed,
    "service_state": fmt_service_state,
}


def pretty(kind: str, payload: dict) -> str:
    f = FORMATTERS.get(kind)
    if f is None:
        return f"{kind}: {payload}"
    return f(payload)


# -- Main loop -------------------------------------------------------------


async def main(args: argparse.Namespace) -> None:
    bind, token = load_bridge()
    url = f"ws://{bind}/ws"
    print(f"connecting to {url} ...")
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(
            json.dumps({"op": "hello", "token": token, "role": "python-bridge"})
        )
        welcome = json.loads(await ws.recv())
        print("welcome:", welcome)
        if welcome.get("op") != "welcome":
            sys.exit(f"auth failed: {welcome}")

        kinds = [k.strip() for k in args.filter.split(",")] if args.filter else []
        await ws.send(json.dumps({"op": "subscribe", "kinds": kinds}))

        if args.request_screenshot:
            # Trip a one-shot capture by publishing a custom event the user's
            # tooling can pick up. The core doesn't yet have a "do this thing"
            # RPC; that's Phase 2. For now we just prove the channel works.
            await ws.send(
                json.dumps(
                    {
                        "op": "publish",
                        "kind": "screenshot_request",
                        "payload": {"reason": "on_demand"},
                    }
                )
            )
            print(
                "request published — actual capture wiring lands when the agent",
                "router (Phase 2) routes 'screenshot_request' to the core.",
            )

        print("listening for events. Ctrl-C to quit.\n" + "-" * 78)
        seen: Counter[str] = Counter()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print("non-json:", raw)
                    continue
                if msg.get("op") != "event":
                    print(msg)
                    continue
                kind = msg.get("kind", "?")
                ts = _ts_short(msg.get("ts", ""))
                payload = msg.get("payload", {}) or {}
                seen[kind] += 1
                print(f"[{ts}] {pretty(kind, payload)}")
        except KeyboardInterrupt:
            pass
        finally:
            print("\n" + "-" * 78)
            print("event counts:")
            for k, n in seen.most_common():
                print(f"  {n:>6}  {k}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filter",
        default="",
        help="Comma-separated event kinds to subscribe to (default: all).",
    )
    parser.add_argument(
        "--request-screenshot",
        action="store_true",
        help="Publish a screenshot_request and continue listening.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nbye.")
