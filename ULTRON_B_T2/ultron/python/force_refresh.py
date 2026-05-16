"""Force every data bridge to publish a fresh tick. Used after a service
restart so the LLM service's LiveState catches up without waiting for
the next natural poll."""
from __future__ import annotations

import asyncio, json, os, sys
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


async def main() -> None:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "force-refresh"}))
        await ws.recv()
        for k in ("weather_request", "stocks_request", "news_request", "system_info_request"):
            await ws.send(json.dumps({"op": "publish", "kind": k, "payload": {}}))
            print(f"  sent {k}")
        await asyncio.sleep(0.8)


asyncio.run(main())
