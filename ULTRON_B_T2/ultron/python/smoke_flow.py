"""Live smoke for the Flow State Protector.

Injects synthetic insight_snapshot + input_metrics_updated events that
satisfy all eligibility criteria, then watches for flow_state_changed
events. Verifies the round-trip: IDLE → ENTERING → ACTIVE → BROKEN.
"""
from __future__ import annotations

import asyncio, json, os, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


GOOD_INPUT = {
    "app_switch_per_min": 1.0,
    "backspace_rate_per_min": 2.0,
    "idle_secs": 4.0,
}
GOOD_SNAPSHOT = {
    "cognitive_load": 0.55,
    "tension": 0.30,
    "cadence_band": "steady",
    "focus_category": "editor",
    "focus_app": "vscode",
}


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    seen: list[dict] = []

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-flow"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["flow_state_changed"]}))
        await asyncio.sleep(0.3)

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") == "event" and m.get("kind") == "flow_state_changed":
                    p = m.get("payload") or {}
                    seen.append(p)
                    print(f"  flow_state_changed: {p.get('prev_state')} -> {p.get('state')}"
                          + (f"  reason={p.get('reason')}" if p.get('reason') else "")
                          + (f"  dur={p.get('duration_minutes')}min" if p.get('duration_minutes') else ""))

        reader_task = asyncio.create_task(reader())

        # Send 4 flow-eligible ticks. samples_to_activate=3 by default,
        # so by tick 3 we should be ACTIVE.
        for i in range(4):
            await ws.send(json.dumps({"op":"publish","kind":"input_metrics_updated",
                                      "payload": GOOD_INPUT}))
            await ws.send(json.dumps({"op":"publish","kind":"insight_snapshot",
                                      "payload": GOOD_SNAPSHOT}))
            await asyncio.sleep(0.4)

        # Now break it: app-switch storm for 2 ticks.
        bad_input = {**GOOD_INPUT, "app_switch_per_min": 9.0}
        for _ in range(2):
            await ws.send(json.dumps({"op":"publish","kind":"input_metrics_updated",
                                      "payload": bad_input}))
            await ws.send(json.dumps({"op":"publish","kind":"insight_snapshot",
                                      "payload": GOOD_SNAPSHOT}))
            await asyncio.sleep(0.4)

        # Give the detector a moment to finish processing.
        await asyncio.sleep(1.0)
        reader_task.cancel()

    states = [(p.get("prev_state"), p.get("state")) for p in seen]
    print()
    print("transitions:", states)

    ok = (
        ("idle", "entering") in states
        and ("entering", "active") in states
        and any(t[1] == "broken" for t in states)
    )
    if ok:
        broken = next(p for p in seen if p.get("state") == "broken")
        print(f"PASS — entered, activated, broke (reason={broken.get('reason')}, "
              f"dur={broken.get('duration_minutes')}min, app={broken.get('last_focus_app')})")
        return 0
    print("FAIL — incomplete transition chain")
    return 1


sys.exit(asyncio.run(main()))
