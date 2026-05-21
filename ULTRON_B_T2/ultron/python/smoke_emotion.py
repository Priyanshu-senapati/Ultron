"""Live smoke for the Emotional Intelligence layer.

Drives a sequence of synthetic voice_transcript events through the
running emotion service and verifies:
  1. ``emotion_state_changed`` fires for a frustration burst.
  2. A subsequent positive turn pulls valence up.
  3. ``emotion_query_request`` round-trips a snapshot.
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


SEQUENCE = [
    ("user", "ugh this is broken again, doesn't work"),
    ("user", "I'm stuck"),
    ("user", "ok finally got it"),
    ("user", "perfect, that's amazing"),
]


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-emotion"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["emotion_state_changed", "emotion_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_changes: list[dict] = []
        seen_queries: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(),
                                                          timeout=20))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "emotion_state_changed":
                    seen_changes.append(p)
                    print(f"  emotion: mood={p.get('mood_label'):>11}"
                          f"  v={p.get('valence'):+.2f}"
                          f"  a={p.get('arousal'):.2f}"
                          f"  f={p.get('frustration'):.2f}"
                          f"  conf={p.get('confidence'):.2f}"
                          f"  matched={p.get('last_matched')}")
                elif k == "emotion_query_result":
                    seen_queries.append(p)

        rt = asyncio.create_task(reader())

        # Feed turns. min_publish_interval is 2s by default; sleep
        # enough between to let each publish go out.
        for role, text in SEQUENCE:
            await ws.send(json.dumps({
                "op": "publish", "kind": "voice_transcript",
                "payload": {"text": text,
                            "ts_unix_ms": int(time.time() * 1000)},
            }))
            await asyncio.sleep(2.4)

        # Query for current state.
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "emotion_query_request",
                                  "payload": {"kind": "current"}}))
        await asyncio.sleep(2.0)
        rt.cancel()

    if not seen_changes:
        print("FAIL  no emotion_state_changed events received")
        return 1
    # Find a frustrated reading and a later positive one.
    saw_frustration = any(c.get("mood_label") == "frustrated"
                          for c in seen_changes)
    later_positive = any(c.get("valence", 0.0) > 0.2
                         for c in seen_changes[2:])
    query_ok = bool(seen_queries
                    and (seen_queries[-1].get("state") or {}).get("mood_label"))
    if saw_frustration and later_positive and query_ok:
        print(f"PASS  {len(seen_changes)} state changes; "
              "frustration -> positive arc observed; query returned snapshot")
        return 0
    print(f"FAIL  saw_frustration={saw_frustration} "
          f"later_positive={later_positive} query_ok={query_ok}")
    return 1


sys.exit(asyncio.run(main()))
