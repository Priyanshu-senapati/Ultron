"""Verify flow_query tool round-trips through Module E."""
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
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-flowq"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_result"]}))
        for kind in ("current", "recent", "stats"):
            rid = f"smoke-{kind}-{int(time.time()*1000)}"
            await ws.send(json.dumps({
                "op":"publish","kind":"tool_call_request",
                "payload":{"request_id":rid,"name":"flow_query","args":{"kind":kind}},
            }))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                except asyncio.TimeoutError:
                    continue
                if (m.get("op") == "event"
                        and m.get("kind") == "tool_call_result"
                        and (m.get("payload") or {}).get("request_id") == rid):
                    p = m["payload"]; inner = p.get("result") or {}
                    ok = p.get("ok") and (not isinstance(inner, dict) or inner.get("ok") is not False)
                    print(f"  flow_query {kind:<8} -> ok={ok} inner_keys={list(inner.keys())[:6] if isinstance(inner, dict) else type(inner)}")
                    if not ok:
                        return 1
                    break
            else:
                print(f"  flow_query {kind}: TIMEOUT")
                return 1
    print("PASS")
    return 0


sys.exit(asyncio.run(main()))
