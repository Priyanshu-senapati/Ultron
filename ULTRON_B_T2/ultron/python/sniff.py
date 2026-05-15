"""Quick event sniffer — listens for 20s and prints all interesting events."""
import asyncio, json, os, sys, tomllib
from pathlib import Path
import websockets

APPDATA = Path(os.environ.get("APPDATA", Path.home()))
with open(APPDATA / "ULTRON" / "config.toml", "rb") as f:
    cfg = tomllib.load(f)
url = f"ws://{cfg['bridge']['bind']}/ws"
token = cfg["bridge"]["token"]

async def main():
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "sniff"}))
        welcome = json.loads(await ws.recv())
        print(f"connected: {welcome.get('session_id')}")
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["screenshot_captured", "visual_label", "insight_snapshot", "window_changed"]
        }))
        print("listening for 25s...")
        deadline = asyncio.get_event_loop().time() + 25
        counts = {}
        last_visual = None
        last_focus = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - asyncio.get_event_loop().time())
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("op") != "event":
                continue
            kind = msg.get("kind", "?")
            counts[kind] = counts.get(kind, 0) + 1
            p = msg.get("payload", {})
            if kind == "visual_label":
                last_visual = p.get("label", "")
                print(f"  visual_label: {last_visual!r}")
            elif kind == "screenshot_captured":
                print(f"  screenshot: {p.get('path','')[-60:]}")
            elif kind == "insight_snapshot":
                last_focus = (p.get("focus_app"), p.get("focus_category"), p.get("visual_label"))
        print(f"\ncounts: {counts}")
        print(f"last visual_label: {last_visual!r}")
        print(f"last insight focus: {last_focus}")

asyncio.run(main())
