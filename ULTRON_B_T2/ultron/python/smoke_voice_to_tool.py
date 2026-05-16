"""Simulate the full voice → LLM → tool path.

Publishes a voice_transcript "open notepad" and watches for the
downstream tool_call_request the LLM should emit, then the
tool_call_result. Tells us exactly where the chain breaks.
"""
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

    saw_request = False
    saw_result = False
    seen_response = ""

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-voice"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":[
            "tool_call_request", "tool_call_result", "llm_response",
        ]}))
        await asyncio.sleep(0.3)

        await ws.send(json.dumps({
            "op":"publish","kind":"voice_transcript",
            "payload": {"text": "open notepad", "ts_unix_ms": int(time.time()*1000)},
        }))
        print("sent voice_transcript: 'open notepad'  (waiting 25s)")

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw_msg)
            if m.get("op") != "event":
                continue
            k = m.get("kind"); p = m.get("payload") or {}
            if k == "llm_response":
                seen_response = (p.get("text") or "")
                print(f"  llm_response: {seen_response[:150]!r}")
            elif k == "tool_call_request":
                print(f"  tool_call_request: {p.get('name')} {p.get('args')}")
                if p.get("name") == "open_app":
                    saw_request = True
            elif k == "tool_call_result":
                inner = p.get("result") or {}
                print(f"  tool_call_result: outer={p.get('ok')} inner={inner}")
                if p.get("ok") and isinstance(inner, dict) and inner.get("ok"):
                    saw_result = True

        print()
        print(f"  saw_request={saw_request}  saw_result={saw_result}")
        if saw_request and saw_result:
            print("PASS — LLM path works end-to-end")
            return 0
        if saw_request and not saw_result:
            print("FAIL — LLM emitted tool_call_request but no successful result. "
                  "Module E either didn't receive it or the handler failed.")
            return 1
        print("FAIL — LLM did NOT emit a tool_call_request. The model "
              "either skipped the tool block or _post_process didn't "
              "parse/publish it.")
        return 1


sys.exit(asyncio.run(main()))
