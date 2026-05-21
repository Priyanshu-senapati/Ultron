"""Verify the recall tool is registered and reachable end-to-end."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
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

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-recall-tool"}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe",
                                  "kinds": ["tool_call_audit"]}))
        await asyncio.sleep(0.3)

        results: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                if m.get("kind") == "tool_call_audit":
                    p = m.get("payload") or {}
                    if p.get("name") == "recall":
                        results.append(p)
                        ok = p.get("ok")
                        res = p.get("result") or {}
                        b = res.get("bundle") or {}
                        c = b.get("counts") or {}
                        print(f"  audit: ok={ok}"
                              f"  hits={c}"
                              f"  block_chars={len(str(res.get('prompt_block') or ''))}")

        rt = asyncio.create_task(reader())

        rid = uuid.uuid4().hex[:12]
        await ws.send(json.dumps({"op": "publish", "kind": "tool_call_request",
                                  "payload": {
                                      "request_id": rid,
                                      "name": "recall",
                                      "args": {"kind": "search",
                                               "query": "what is the name of my pet",
                                               "top_k": 5},
                                  }}))
        await asyncio.sleep(8.0)
        rt.cancel()

    if results and results[0].get("ok"):
        print(f"PASS  recall tool reachable via tool_call_request")
        return 0
    print(f"FAIL  recall tool audits seen: {len(results)}")
    if results:
        print(f"  payload: {results[0]}")
    return 1


sys.exit(asyncio.run(main()))
