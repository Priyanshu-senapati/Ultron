"""Live smoke for the find_and_open tool — hits real DDG.

The browser launch step is unavoidable. We pass an unusual query and
verify (a) the tool responds via tool_call_audit, (b) it returns a real
URL with sensible ranking. The browser will pop a tab; close it after.
"""
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
                                  "role": "smoke-find-and-open"}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe",
                                  "kinds": ["tool_call_audit"]}))
        await asyncio.sleep(0.4)

        # Use a query with a strong site hint so the ranker has something
        # to do beyond DDG's natural order.
        rid = uuid.uuid4().hex[:12]
        await ws.send(json.dumps({"op": "publish", "kind": "tool_call_request",
                                  "payload": {
                                      "request_id": rid,
                                      "name": "find_and_open",
                                      "args": {
                                          "query": "wikipedia ultron marvel",
                                      },
                                  }}))
        deadline = asyncio.get_event_loop().time() + 25.0
        result: dict | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                continue
            if m.get("op") != "event":
                continue
            if m.get("kind") != "tool_call_audit":
                continue
            p = m.get("payload") or {}
            if p.get("name") == "find_and_open" and p.get("request_id") == rid:
                result = p
                break

    if result is None:
        print("FAIL  no audit event received for find_and_open")
        return 1
    res = result.get("result") or {}
    ok = bool(res.get("ok"))
    print(f"  ok       : {ok}")
    print(f"  query    : {res.get('query')}")
    print(f"  url      : {res.get('url')}")
    print(f"  title    : {res.get('title')}")
    print(f"  host     : {res.get('host')}")
    print(f"  hint     : {res.get('site_hint')}")
    print(f"  considered: {res.get('considered')}")
    print(f"  rank_won : {res.get('rank_of_winner_in_ddg')}")
    print(f"  alts     :")
    for a in (res.get("alternates") or []):
        print(f"    - {a.get('title')}  <{a.get('url')}>")
    if ok and res.get("url"):
        # Bonus: prefer that the winning host is wikipedia given the hint.
        host = (res.get("host") or "")
        hint_landed = host.endswith("wikipedia.org")
        print(f"PASS  ok=True host={host!r} hint_landed={hint_landed}")
        return 0
    print("FAIL")
    return 1


sys.exit(asyncio.run(main()))
