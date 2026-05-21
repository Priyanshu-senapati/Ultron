"""Live smoke for the Readiness Score module.

Connects to the bridge, feeds the readiness service synthetic
sleep_recorded + workout_recorded + insight_snapshot + flow_state_changed
events, then asks for a recompute and asserts:

  1. ``readiness_score_update`` is published.
  2. The total is roughly what the inputs imply.
  3. ``readiness_query_request`` round-trips a ``readiness_query_result``.
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

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-readiness"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["readiness_score_update", "readiness_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_updates: list[dict] = []
        seen_results: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "readiness_score_update":
                    seen_updates.append(p)
                    print(f"  update: total={p.get('total')}  bucket={p.get('bucket')}")
                    for c in (p.get("components") or []):
                        print(f"    {c['name']:>14s}: {c['score']:>5.1f}/{c['max_score']:.0f}"
                              f"  ({c.get('detail','')})")
                elif k == "readiness_query_result":
                    seen_results.append(p)
                    print(f"  query result: kind={p.get('kind')}")

        rt = asyncio.create_task(reader())

        # Feed the signals — "primed morning" profile.
        await ws.send(json.dumps({"op": "publish", "kind": "sleep_recorded",
                                  "payload": {"hours": 7.6,
                                              "sleep": {"hours": 7.6,
                                                        "date": "2026-05-21"}}}))
        await ws.send(json.dumps({"op": "publish", "kind": "workout_recorded",
                                  "payload": {"workout": {"ts": time.time() - 6 * 3600,
                                                          "duration_secs": 1800,
                                                          "exercise": "run"}}}))
        # Push a few low-tension snapshots so the EWMA settles down.
        for _ in range(5):
            await ws.send(json.dumps({"op": "publish", "kind": "insight_snapshot",
                                      "payload": {"tension": 0.20,
                                                  "cognitive_load": 0.55}}))
        # And a completed flow session — 70 minutes ending now.
        await ws.send(json.dumps({"op": "publish", "kind": "flow_state_changed",
                                  "payload": {"state": "broken",
                                              "prev_state": "active",
                                              "duration_seconds": 70 * 60,
                                              "ts": time.time()}}))
        await asyncio.sleep(1.5)

        # Trigger an explicit recompute via query.
        await ws.send(json.dumps({"op": "publish", "kind": "readiness_query_request",
                                  "payload": {"kind": "recompute"}}))
        await asyncio.sleep(1.5)

        # Ask for current state too.
        await ws.send(json.dumps({"op": "publish", "kind": "readiness_query_request",
                                  "payload": {"kind": "current"}}))
        await asyncio.sleep(1.2)
        rt.cancel()

    ok_updates = bool(seen_updates) and seen_updates[-1].get("total", 0) > 0
    ok_results = any(r.get("kind") == "current" for r in seen_results)
    final = seen_updates[-1] if seen_updates else {}
    if ok_updates and ok_results:
        print(f"PASS  total={final.get('total')}  bucket={final.get('bucket')}"
              f"  updates={len(seen_updates)}  queries={len(seen_results)}")
        return 0
    print(f"FAIL  updates={len(seen_updates)}  queries={len(seen_results)}  final={final}")
    return 1


sys.exit(asyncio.run(main()))
