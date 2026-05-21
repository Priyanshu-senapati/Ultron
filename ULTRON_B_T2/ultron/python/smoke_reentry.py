"""Live smoke for the Re-entry Protocol — exercises the WS contract.

We can't actually wait 5 minutes in a smoke. Instead we use the same
trick as the flow smoke: lower the thresholds via the published config
defaults DON'T apply here (the service already loaded its config) so we
drive the detector indirectly by:

  1. Publishing input_metrics_updated with a huge idle_secs to force AWAY.
  2. Publishing a visual_label + an llm_response while AWAY so the
     context buffer has something to brief about.
  3. Publishing input_metrics_updated with idle_secs=0 to trigger
     RETURNING and (if the live config's min_away_minutes_for_brief
     allows) the brief.

The brief threshold in the default config is 5 minutes — but the
detector measures (now - last_idle_secs) for the away-start. By
publishing idle_secs=400 first, the service computes away_started_ts
~= now-400s, so when we then push idle_secs=0 right after, the duration
will be ~400s — comfortably above the 300s threshold.
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


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "smoke-reentry"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["presence_state_changed", "reentry_brief", "reentry_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_presence: list[dict] = []
        seen_brief: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "presence_state_changed":
                    seen_presence.append(p)
                    print(f"  presence: {p.get('prev_state')}->{p.get('state')}"
                          + (f"  away={p.get('away_duration_seconds')}s"
                             if p.get("away_duration_seconds") else ""))
                elif k == "reentry_brief":
                    seen_brief.append(p)
                    print(f"  brief ({p.get('away_minutes')} min):")
                    print(f"    {p.get('text')}")

        rt = asyncio.create_task(reader())

        # Prime the context buffer with focus + label + LLM reply BEFORE
        # the user goes away, since the brief reads the most recent
        # values regardless of when they were published.
        await ws.send(json.dumps({"op": "publish", "kind": "insight_snapshot",
                                  "payload": {"focus_app": "vscode",
                                              "focus_category": "editor",
                                              "cognitive_load": 0.6,
                                              "tension": 0.3}}))
        await ws.send(json.dumps({"op": "publish", "kind": "visual_label",
                                  "payload": {"label": "writing reentry tests"}}))
        await ws.send(json.dumps({"op": "publish", "kind": "llm_response",
                                  "payload": {"text": "Sounds good, sir. We are ready to ship Roadmap #2.",
                                              "shard": "default"}}))
        await asyncio.sleep(0.5)

        # Force AWAY by reporting a giant idle_secs.
        await ws.send(json.dumps({"op": "publish", "kind": "input_metrics_updated",
                                  "payload": {"idle_secs": 400.0,
                                              "app_switch_per_min": 0.0,
                                              "backspace_rate_per_min": 0.0}}))
        await asyncio.sleep(0.6)

        # Simulate the first keystroke back: idle drops to 0.
        await ws.send(json.dumps({"op": "publish", "kind": "input_metrics_updated",
                                  "payload": {"idle_secs": 0.0,
                                              "app_switch_per_min": 0.0,
                                              "backspace_rate_per_min": 0.0}}))
        await asyncio.sleep(1.5)

        # Query the service for its view of state.
        await ws.send(json.dumps({"op": "publish", "kind": "reentry_query_request",
                                  "payload": {"kind": "last_brief"}}))
        await asyncio.sleep(1.5)
        rt.cancel()

    states = [(p.get("prev_state"), p.get("state")) for p in seen_presence]
    ok_transitions = (("present", "away") in states and
                      any(s[1] == "returning" for s in states))
    ok_brief = bool(seen_brief and seen_brief[0].get("text"))
    if ok_transitions and ok_brief:
        print(f"PASS  transitions={states}  brief_chars={len(seen_brief[0]['text'])}")
        return 0
    print(f"FAIL  transitions={states}  briefs={len(seen_brief)}")
    return 1


sys.exit(asyncio.run(main()))
