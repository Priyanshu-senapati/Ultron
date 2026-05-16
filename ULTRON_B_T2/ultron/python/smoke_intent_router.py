"""Verify the intent router intercepts common verbs before the LLM."""
from __future__ import annotations

import asyncio, json, os, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CASES = [
    # (voice text, expected tool, expected partial args)
    # Specific song/artist queries reach Spotify search:
    ("play ocean eyes on spotify",   "spotify_play", {"query": "ocean eyes"}),
    ("play pink floyd",              "spotify_play", {"query": "pink floyd"}),
    # Generic "play music/song/etc." should TOGGLE play, not search:
    ("play music",                   "media_control",{"what": "play_pause"}),
    ("play some music",              "media_control",{"what": "play_pause"}),
    ("play the music",               "media_control",{"what": "play_pause"}),
    # Verbs with trailing filler ("the music", "please", "song"):
    ("pause",                        "media_control",{"what": "play_pause"}),
    ("pause the music",              "media_control",{"what": "play_pause"}),
    ("stop the music",               "media_control",{"what": "stop"}),
    ("next song please",             "media_control",{"what": "next"}),
    ("turn it down",                 "media_control",{"what": "volume_down"}),
    # App / search / brightness / etc. (regression baseline):
    ("open chrome",                  "open_app",     {"name": "chrome"}),
    ("open spotify",                 "open_app",     {"name": "spotify"}),
    ("search rain in chennai on chrome", "web_open", {"query": "rain in chennai", "browser": "chrome"}),
    ("search rust async on youtube", "web_open",     {"query": "rust async", "site": "youtube.com"}),
    ("set brightness to 40",         "brightness",   {"action": "set", "level": 40}),
    ("dim the screen",               "brightness",   {"action": "down"}),
    ("next song",                    "media_control",{"what": "next"}),
    ("volume up",                    "media_control",{"what": "volume_up"}),
]


async def run_one(ws, text: str, expect_tool: str, expect_args: dict) -> bool:
    await ws.send(json.dumps({
        "op":"publish","kind":"voice_transcript",
        "payload":{"text": text, "ts_unix_ms": int(time.time()*1000)},
    }))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
        except asyncio.TimeoutError:
            continue
        m = json.loads(raw)
        if m.get("op") == "event" and m.get("kind") == "tool_call_request":
            p = m.get("payload") or {}
            if p.get("name") == expect_tool:
                ok = all(p.get("args", {}).get(k) == v for k, v in expect_args.items())
                print(f"  {text!r:<45} -> {expect_tool} args_ok={ok} actual={p.get('args')}")
                return ok
    print(f"  {text!r:<45} -> TIMEOUT (expected {expect_tool})")
    return False


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]
    passed = 0
    failed: list[str] = []
    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-intent"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_request"]}))
        for text, tool, args in CASES:
            if await run_one(ws, text, tool, args):
                passed += 1
            else:
                failed.append(text)
            # Pace requests so handlers settle.
            await asyncio.sleep(0.2)
    print()
    print(f"{passed}/{len(CASES)} routes intercepted by intent router")
    if failed:
        print("missed:")
        for f in failed:
            print(f"  - {f!r}")
        return 1
    return 0


sys.exit(asyncio.run(main()))
