"""Verify brightness tool works end-to-end (get only — non-destructive)."""
from __future__ import annotations

import asyncio, json, os, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-bright"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_result"]}))
        rid = f"smoke-{int(time.time()*1000)}"
        await ws.send(json.dumps({
            "op":"publish","kind":"tool_call_request",
            "payload": {"request_id": rid, "name": "brightness",
                        "args": {"action": "get"}},
        }))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw_msg)
            if (m.get("op") == "event"
                    and m.get("kind") == "tool_call_result"
                    and (m.get("payload") or {}).get("request_id") == rid):
                p = m["payload"]; inner = p.get("result") or {}
                if p.get("ok") and isinstance(inner, dict) and inner.get("ok"):
                    print(f"PASS  current brightness = {inner.get('level')}")
                    return 0
                print(f"FAIL  outer ok={p.get('ok')} inner={inner}")
                return 1
        print("FAIL  timeout")
        return 2


sys.exit(asyncio.run(main()))
