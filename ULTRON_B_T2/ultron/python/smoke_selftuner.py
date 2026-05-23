"""Live smoke for the Self-Tuner.

Feeds a handful of tool_call_audit + emotion_state_changed events,
then triggers a self_reflect_request and verifies:
  1. ``self_reflection_written`` fires with a real md_path.
  2. The dated markdown file exists on disk and contains the expected
     sections + the synthetic tool name.
  3. Any tuning_suggestion events that fire are well-formed.
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
                                  "role": "smoke-selftuner"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["self_reflection_written", "tuning_suggestion"],
        }))
        await asyncio.sleep(0.4)

        written: list[dict] = []
        suggestions: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "self_reflection_written":
                    written.append(p)
                    print(f"  written: date={p.get('date')}"
                          f"  suggestions={p.get('suggestion_count')}"
                          f"  md_chars={p.get('md_chars')}")
                elif k == "tuning_suggestion":
                    suggestions.append(p)
                    print(f"  suggestion: {p.get('title')}")

        rt = asyncio.create_task(reader())

        # Synthesise an outlier: a tool that has been failing a lot. The
        # selftuner observer aggregates from live tool_call_audit events.
        now = time.time()
        for i in range(8):
            await ws.send(json.dumps({"op": "publish", "kind": "tool_call_audit",
                                      "payload": {"name": "smoke_flaky_tool",
                                                  "ok": False,
                                                  "result": {"ok": False,
                                                             "reason": "synthetic failure"},
                                                  "ts": now - 100 + i}}))
        for i in range(2):
            await ws.send(json.dumps({"op": "publish", "kind": "tool_call_audit",
                                      "payload": {"name": "smoke_flaky_tool",
                                                  "ok": True,
                                                  "result": {"ok": True},
                                                  "ts": now - 50 + i}}))

        # And a couple of frustration events for variety.
        for f in (0.7, 0.6, 0.5):
            await ws.send(json.dumps({
                "op": "publish", "kind": "emotion_state_changed",
                "payload": {"mood_label": "frustrated",
                             "valence": -0.5, "arousal": 0.4,
                             "frustration": f,
                             "confidence": 0.9, "source": "lexicon",
                             "last_matched": ["ugh"],
                             "ts": now - 30},
            }))
        await asyncio.sleep(1.0)

        # Trigger the reflection now (don't wait for the 24h heartbeat).
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "self_reflect_request",
                                  "payload": {}}))
        # Reflection involves SQLite reads + file write; give it time.
        await asyncio.sleep(4.0)
        rt.cancel()

    if not written:
        print("FAIL  no self_reflection_written event received")
        return 1
    info = written[-1]
    md_path = Path(info["md_path"])
    if not md_path.exists():
        print(f"FAIL  md file missing at {md_path}")
        return 1
    md = md_path.read_text(encoding="utf-8")
    checks = {
        "header": "# ULTRON Self-Reflection" in md,
        "tools_section": "## Tools" in md,
        "emotion_section": "## Emotion" in md,
        "suggestions_section": "## Tuning suggestions" in md,
        "flaky_tool_mentioned": "smoke_flaky_tool" in md,
    }
    fails = [k for k, v in checks.items() if not v]
    flaky_in_suggestions = any("smoke_flaky_tool" in (s.get("title") or "")
                               for s in suggestions)
    if not fails and flaky_in_suggestions:
        print(f"PASS  md={md_path.name}  suggestions={len(suggestions)}  "
              "flaky tool surfaced")
        return 0
    print(f"FAIL  missing_sections={fails}  flaky_in_suggestions={flaky_in_suggestions}")
    print("--- md head ---")
    print(md[:1500])
    return 1


sys.exit(asyncio.run(main()))
