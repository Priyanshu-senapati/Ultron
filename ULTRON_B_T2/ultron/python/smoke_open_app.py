"""Direct test that open_app actually launches an app end-to-end.

Bypasses the LLM — publishes a tool_call_request straight to Module E
and verifies the result. Uses 'calculator' so the test is non-disruptive.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
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
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "smoke-openapp"}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe", "kinds": ["tool_call_result"]}))

        rid = f"smoke-{int(time.time()*1000)}"
        await ws.send(json.dumps({
            "op": "publish", "kind": "tool_call_request",
            "payload": {"request_id": rid, "name": "open_app",
                        "args": {"name": "calculator"}},
        }))

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw_msg)
            if (m.get("op") == "event"
                    and m.get("kind") == "tool_call_result"
                    and (m.get("payload") or {}).get("request_id") == rid):
                p = m["payload"]
                outer_ok = p.get("ok") is True
                inner = p.get("result") or {}
                inner_ok = inner.get("ok") is True if isinstance(inner, dict) else False
                if outer_ok and inner_ok:
                    print(f"PASS  outer ok=True, inner ok=True, target={inner.get('target')}")
                    return 0
                print(f"FAIL  outer ok={outer_ok}, inner={inner}")
                return 1
        print("FAIL  timeout waiting for tool_call_result")
        return 2


sys.exit(asyncio.run(main()))
