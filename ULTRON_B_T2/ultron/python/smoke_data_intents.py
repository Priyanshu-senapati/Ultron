"""Verify data-question intents answer from state with no LLM call.

For each phrase: send a voice_transcript, watch for the llm_response.
Time the round-trip. Anything under ~500 ms means we never hit the
model.
"""
from __future__ import annotations

import asyncio, json, os, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


PHRASES = [
    "what's the time",
    "what time is it",
    "what's today's date",
    "how's the weather",
    "weather",
    "how's the market",
    "sensex today",
    "any news",
    "battery level",
    "wifi status",
]


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-data"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["llm_response"]}))

        for phrase in PHRASES:
            t0 = time.monotonic()
            await ws.send(json.dumps({
                "op":"publish","kind":"voice_transcript",
                "payload":{"text": phrase, "ts_unix_ms": int(time.time()*1000)},
            }))
            deadline = time.monotonic() + 15
            answer = ""
            shard = ""
            while time.monotonic() < deadline:
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(raw_msg)
                if m.get("op") == "event" and m.get("kind") == "llm_response":
                    p = m.get("payload") or {}
                    answer = p.get("text", "")
                    shard = p.get("shard", "")
                    break
            dt = (time.monotonic() - t0) * 1000
            tag = "[intent]" if shard == "intent" else f"[{shard or 'llm'}]"
            ok = "OK" if shard == "intent" else "??"
            print(f"  {ok} {dt:5.0f}ms  {tag:<10} {phrase!r:<28} -> {answer[:80]!r}")
            await asyncio.sleep(0.3)


asyncio.run(main())
