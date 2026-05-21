"""Tiny health check: round-trip a query against each new roadmap service.

Confirms post-restart that:
  - flow service answers flow_query_request (current)
  - reentry service answers reentry_query_request (current)
  - readiness service answers readiness_query_request (current)
  - interrupt service answers interrupt_query_request (today)
  - context preserver writes a packet on context_packet_request
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


PROBES = [
    ("flow",     "flow_query_request",     "flow_query_result",     {"kind": "current"}),
    ("reentry",  "reentry_query_request",  "reentry_query_result",  {"kind": "current"}),
    ("readiness","readiness_query_request","readiness_query_result",{"kind": "current"}),
    ("interrupt","interrupt_query_request","interrupt_query_result",{"kind": "today"}),
    ("ctx-pres", "context_packet_request", "context_packet_written",{"reason": "health-check"}),
    ("recall",   "recall_query_request",   "recall_query_result",   {"kind": "counts"}),
]


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-roadmap-health"}))
        await ws.recv()
        result_kinds = sorted({result_kind for _, _, result_kind, _ in PROBES})
        await ws.send(json.dumps({"op": "subscribe", "kinds": result_kinds}))
        await asyncio.sleep(0.4)

        results: dict[str, dict] = {}
        all_seen = asyncio.Event()

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k in result_kinds:
                    # First-arrival wins per probe.
                    for name, _, result_kind, _ in PROBES:
                        if result_kind == k and name not in results:
                            results[name] = p
                            break
                if len(results) >= len(PROBES):
                    all_seen.set()
                    return

        rt = asyncio.create_task(reader())

        for _, req_kind, _, payload in PROBES:
            await ws.send(json.dumps({"op": "publish", "kind": req_kind,
                                      "payload": payload}))
            await asyncio.sleep(0.15)

        try:
            await asyncio.wait_for(all_seen.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            pass
        rt.cancel()

    width = max(len(n) for n, *_ in PROBES)
    fails = []
    for name, _, _, _ in PROBES:
        r = results.get(name)
        if r is None:
            fails.append(name)
            print(f"  {name:<{width}} : NO RESPONSE")
            continue
        if name == "flow":
            print(f"  {name:<{width}} : OK  state={r.get('state')}")
        elif name == "reentry":
            print(f"  {name:<{width}} : OK  state={r.get('state')}")
        elif name == "readiness":
            s = r.get("score") or {}
            print(f"  {name:<{width}} : OK  total={s.get('total')} bucket={s.get('bucket')}")
        elif name == "interrupt":
            s = r.get("stats") or {}
            print(f"  {name:<{width}} : OK  today_count={s.get('count')}")
        elif name == "ctx-pres":
            print(f"  {name:<{width}} : OK  md_chars={r.get('md_chars')} reason={r.get('reason')}")
        elif name == "recall":
            c = r.get("counts") or {}
            print(f"  {name:<{width}} : OK  turns={c.get('turns')} "
                  f"reflections={c.get('reflections')} facts={c.get('facts')}"
                  f" conv={r.get('conv_id')}")

    if fails:
        print(f"FAIL  no response from: {fails}")
        return 1
    print(f"PASS  all {len(PROBES)} roadmap services responsive")
    return 0


sys.exit(asyncio.run(main()))
