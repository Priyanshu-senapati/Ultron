"""Quick diag: send code_query_request and dump everything we see."""
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


async def main() -> None:
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    url = f"ws://{bridge['bind']}/ws"
    token = bridge["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "diag"}))
        print("welcome:", await ws.recv())
        # Subscribe broadly
        await ws.send(json.dumps({"op": "subscribe",
                                  "kinds": ["code_query_request",
                                            "code_query_result",
                                            "code_index_complete"]}))
        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "code_query_request",
                                  "payload": {"kind": "stats"}}))
        print(f"[{time.strftime('%H:%M:%S')}] sent request, listening 30s...")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                print("non-json:", raw[:200])
                continue
            kind = msg.get("kind", "?")
            print(f"[{time.strftime('%H:%M:%S')}] {msg.get('op','?')} {kind}: "
                  f"{json.dumps(msg.get('payload'))[:200]}")


asyncio.run(main())
