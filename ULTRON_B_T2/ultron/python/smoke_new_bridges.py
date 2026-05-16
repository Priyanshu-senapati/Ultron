"""Spot-check the new sysinfo + dailydata bridges are publishing."""
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

    want = {"system_info", "weather_update", "stocks_update", "news_update"}
    seen = set()

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-new"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":list(want)}))
        # Trigger every source so we don't have to wait for natural ticks.
        for k in ("system_info_request","weather_request","stocks_request","news_request"):
            await ws.send(json.dumps({"op":"publish","kind":k,"payload":{}}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and seen != want:
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw_msg)
            if m.get("op") == "event" and m.get("kind") in want:
                k = m["kind"]
                if k not in seen:
                    seen.add(k)
                    p = m.get("payload") or {}
                    if k == "system_info":
                        b = p.get("battery") or {}
                        w = p.get("wifi") or {}
                        print(f"[ok] system_info  battery={b.get('percent','?')}% "
                              f"wifi={w.get('ssid','off')}")
                    elif k == "weather_update":
                        print(f"[ok] weather_update  {p.get('temp_c','?')}°C "
                              f"{p.get('label','?')} @ {p.get('city','?')}")
                    elif k == "stocks_update":
                        rows = p.get("rows") or []
                        print(f"[ok] stocks_update  {len(rows)} rows  "
                              f"insight={p.get('insight','?')}")
                    elif k == "news_update":
                        hs = p.get("headlines") or []
                        sample = hs[0]["title"][:60] if hs else "(none)"
                        print(f"[ok] news_update  {len(hs)} headlines  first={sample!r}")
    missing = want - seen
    if missing:
        print(f"FAIL: never saw {missing}")
        return 1
    print("PASS")
    return 0


sys.exit(asyncio.run(main()))
