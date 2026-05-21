"""Live smoke for the Context Preserver.

Feeds the running service synthetic events, then fires a
context_packet_request, then opens the produced ``context_packet.md``
and asserts the expected substance landed on disk.
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
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    md_path = Path(os.environ["APPDATA"]) / "ULTRON" / "context_packet.md"
    json_path = Path(os.environ["APPDATA"]) / "ULTRON" / "context_packet.json"

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-context-preserver"}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe",
                                  "kinds": ["context_packet_written",
                                            "context_packet_loaded"]}))
        await asyncio.sleep(0.4)

        seen_written: list[dict] = []
        seen_loaded: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "context_packet_written":
                    seen_written.append(p)
                    print(f"  written: reason={p.get('reason')}"
                          f" md_chars={p.get('md_chars')}")
                elif k == "context_packet_loaded":
                    seen_loaded.append(p)
                    print(f"  loaded prior packet: reason="
                          f"{(p.get('previous') or {}).get('reason')}")

        rt = asyncio.create_task(reader())

        # Feed a rich set of context events.
        await ws.send(json.dumps({"op": "publish", "kind": "insight_snapshot",
                                  "payload": {"focus_app": "vscode",
                                              "focus_category": "editor",
                                              "tension": 0.25,
                                              "cognitive_load": 0.6}}))
        await ws.send(json.dumps({"op": "publish", "kind": "visual_label",
                                  "payload": {"label": "writing context preserver"}}))
        await ws.send(json.dumps({"op": "publish", "kind": "voice_transcript",
                                  "payload": {"text": "hey ultron save the session"}}))
        await ws.send(json.dumps({"op": "publish", "kind": "llm_response",
                                  "payload": {"text": ("Saving the session now, sir. "
                                                       "All five roadmap items are done."),
                                              "shard": "default"}}))
        await ws.send(json.dumps({"op": "publish", "kind": "flow_state_changed",
                                  "payload": {"prev_state": "active",
                                              "state": "broken",
                                              "duration_seconds": 25 * 60,
                                              "reason": "app_switch",
                                              "last_focus_app": "vscode",
                                              "ts": time.time()}}))
        await ws.send(json.dumps({"op": "publish", "kind": "readiness_score_update",
                                  "payload": {"total": 84.0, "bucket": "primed",
                                              "components": [
                                                  {"name": "sleep", "score": 40.0,
                                                   "max_score": 40.0,
                                                   "detail": "7.6h vs 7.5h"},
                                                  {"name": "flow_yesterday",
                                                   "score": 22.5, "max_score": 30.0,
                                                   "detail": "85 min"},
                                                  {"name": "calm", "score": 12.0,
                                                   "max_score": 15.0,
                                                   "detail": "tension 0.32"},
                                                  {"name": "activity",
                                                   "score": 9.5, "max_score": 15.0,
                                                   "detail": "20h ago"},
                                              ]}}))
        await ws.send(json.dumps({"op": "publish", "kind": "git_activity",
                                  "payload": {"commits": [
                                      {"sha": "f" * 40,
                                       "subject": "Roadmap #5 — Context Preserver",
                                       "ts": time.time() - 60},
                                  ], "head": "f" * 40}}))
        await ws.send(json.dumps({"op": "publish", "kind": "claude_session_update",
                                  "payload": {"snippet": ("Built the Context "
                                                          "Preserver and ran the "
                                                          "smoke.")}}))
        await asyncio.sleep(0.6)

        # Trigger a write.
        await ws.send(json.dumps({"op": "publish", "kind": "context_packet_request",
                                  "payload": {"reason": "smoke"}}))
        await asyncio.sleep(1.5)
        rt.cancel()

    if not md_path.exists() or not json_path.exists():
        print(f"FAIL  packet files missing: md={md_path.exists()}"
              f" json={json_path.exists()}")
        return 1
    md = md_path.read_text(encoding="utf-8")
    js = json.loads(json_path.read_text(encoding="utf-8"))

    checks = {
        "header": "# ULTRON Context Packet" in md,
        "focus": "vscode" in md and js["focus"]["app"] == "vscode",
        "vision": "writing context preserver" in md,
        "user_turn": "save the session" in md,
        "llm_turn": "Saving the session now" in md,
        "flow_break": "25.0 min" in md and "app_switch" in md,
        "readiness": "84/100" in md and "primed" in md,
        "git": "Roadmap #5" in md,
        "claude": "ran the smoke" in md or "Context Preserver" in md,
        "written_event": bool(seen_written),
    }
    fails = [k for k, v in checks.items() if not v]
    if not fails:
        print(f"PASS  md_chars={len(md)} writes={len(seen_written)}"
              f" loaded_prior={len(seen_loaded)}")
        return 0
    print(f"FAIL  missing={fails}")
    print("--- packet head ---")
    print(md[:1200])
    return 1


sys.exit(asyncio.run(main()))
