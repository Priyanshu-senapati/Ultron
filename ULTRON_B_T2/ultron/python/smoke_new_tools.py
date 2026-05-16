"""Verify web_open, spotify_play, and the claude_feed sink all work."""
from __future__ import annotations

import asyncio, json, os, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


async def call_tool(ws, name: str, args: dict) -> dict:
    rid = f"smoke-{name}-{int(time.time()*1000)}"
    await ws.send(json.dumps({
        "op":"publish","kind":"tool_call_request",
        "payload":{"request_id":rid,"name":name,"args":args},
    }))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        except asyncio.TimeoutError:
            continue
        if (msg.get("op") == "event"
                and msg.get("kind") == "tool_call_result"
                and (msg.get("payload") or {}).get("request_id") == rid):
            return msg["payload"]
    raise AssertionError(f"timeout for {name}")


async def main() -> int:
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-newtools"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_result"]}))

        # web_open: just resolves and launches — don't actually open a tab.
        # Use a query that returns instantly. We verify the bus result.
        wo = await call_tool(ws, "web_open", {"query": "ultron cognitive twin"})
        print(f"web_open  -> outer={wo.get('ok')} inner={wo.get('result')}")
        assert wo.get("ok") is True

        # spotify_play search URI — just builds spotify:search:... URI.
        sp = await call_tool(ws, "spotify_play", {"query": "Pink Floyd Time"})
        print(f"spotify_play -> outer={sp.get('ok')} inner={sp.get('result')}")
        assert sp.get("ok") is True

        # Trigger a tool error so claude_feed gets something to log.
        # Asking open_app with empty name -> handler returns ok:False reason
        err = await call_tool(ws, "open_app", {"name": ""})
        print(f"forced-error open_app -> outer={err.get('ok')} inner={err.get('result')}")

    # Now check the claude-feed file got written
    feed_path = Path("C:/dev/.ultron-feed") / (time.strftime("%Y-%m-%d") + ".md")
    if not feed_path.exists():
        print(f"FAIL — claude feed file missing: {feed_path}")
        return 1
    text = feed_path.read_text(encoding="utf-8")
    if "tool-error" not in text:
        print(f"FAIL — feed file present but no tool-error entry yet.\n"
              f"  contents start: {text[:200]!r}")
        return 1
    print(f"claude_feed wrote {len(text)} bytes to {feed_path}")
    print("PASS — web_open, spotify_play, claude_feed all working")
    return 0


sys.exit(asyncio.run(main()))
