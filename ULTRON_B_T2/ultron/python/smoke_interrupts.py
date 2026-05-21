"""Live smoke for the Interrupt Ledger.

Connects to the bridge, fires synthetic flow-break + recovery and a
wake-word transcript, then queries today's rollup. Assertions:
  1. ``interrupt_logged`` events fire for each recordable input.
  2. ``interrupt_recovered`` fires when a flow ACTIVE transition
     follows a flow_break inside the recovery window.
  3. ``interrupt_query_result`` round-trips today's stats with the
     expected sources represented.
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
                                  "role": "smoke-interrupts"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["interrupt_logged", "interrupt_recovered", "interrupt_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_log: list[dict] = []
        seen_rec: list[dict] = []
        seen_q: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "interrupt_logged":
                    seen_log.append(p)
                    print(f"  logged: id={p.get('id')} source={p.get('source')}"
                          f" detail='{p.get('detail')}'")
                elif k == "interrupt_recovered":
                    seen_rec.append(p)
                    print(f"  recovered: id={p.get('id')}"
                          f" source={p.get('source')}"
                          f" rec={p.get('recovery_secs')}s")
                elif k == "interrupt_query_result":
                    seen_q.append(p)
                    s = p.get("stats") or {}
                    print(f"  query: kind={p.get('kind')} count={s.get('count')}"
                          f" by_source={s.get('by_source')}")

        rt = asyncio.create_task(reader())

        # Set the focus app for attribution.
        await ws.send(json.dumps({"op": "publish", "kind": "insight_snapshot",
                                  "payload": {"focus_app": "vscode",
                                              "focus_category": "editor",
                                              "tension": 0.3,
                                              "cognitive_load": 0.6}}))

        # 1) Flow break (5 min session) — should log as interrupt.
        now = time.time()
        await ws.send(json.dumps({"op": "publish", "kind": "flow_state_changed",
                                  "payload": {"prev_state": "active",
                                              "state": "broken",
                                              "duration_seconds": 300.0,
                                              "reason": "app_switch",
                                              "last_focus_app": "vscode",
                                              "ts": now}}))
        await asyncio.sleep(0.6)

        # 2) Wake word during PRESENT — self-interrupt.
        await ws.send(json.dumps({"op": "publish", "kind": "voice_transcript",
                                  "payload": {"text": "hey ultron what's my score"}}))
        await asyncio.sleep(0.6)

        # 3) Wellness nudge.
        await ws.send(json.dumps({"op": "publish", "kind": "wellness_nudge",
                                  "payload": {"kind": "low_sleep"}}))
        await asyncio.sleep(0.6)

        # 4) Flow recovers — ACTIVE entry pairs the pending interrupts.
        await ws.send(json.dumps({"op": "publish", "kind": "flow_state_changed",
                                  "payload": {"prev_state": "entering",
                                              "state": "active",
                                              "ts": now + 200.0}}))
        await asyncio.sleep(1.0)

        # 5) Query today's stats.
        await ws.send(json.dumps({"op": "publish", "kind": "interrupt_query_request",
                                  "payload": {"kind": "today"}}))
        await asyncio.sleep(1.2)
        rt.cancel()

    sources_logged = {l.get("source") for l in seen_log}
    ok_log = sources_logged >= {"flow_break", "wake_word", "wellness_nudge"}
    ok_rec = len(seen_rec) >= 3   # all three pending should recover
    ok_q = any(q.get("kind") == "today" and (q.get("stats") or {}).get("count", 0) >= 3
               for q in seen_q)
    if ok_log and ok_rec and ok_q:
        print(f"PASS  logged={len(seen_log)} recovered={len(seen_rec)}"
              f" queries={len(seen_q)} sources={sources_logged}")
        return 0
    print(f"FAIL  logged={len(seen_log)} recovered={len(seen_rec)}"
          f" queries={len(seen_q)} sources={sources_logged}")
    return 1


sys.exit(asyncio.run(main()))
