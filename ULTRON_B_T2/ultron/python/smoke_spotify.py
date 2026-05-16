"""Verify open_app spotify launches the Microsoft Store Spotify app."""
from __future__ import annotations

import asyncio, json, os, subprocess, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def spotify_running() -> bool:
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "(Get-Process spotify -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    try:
        return int((out.splitlines() or ["0"])[0]) > 0
    except ValueError:
        return False


async def main() -> int:
    if spotify_running():
        print("note: Spotify already running")
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-spotify"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_result"]}))
        rid = f"smoke-{int(time.time()*1000)}"
        await ws.send(json.dumps({
            "op":"publish","kind":"tool_call_request",
            "payload": {"request_id": rid, "name": "open_app",
                        "args": {"name": "spotify"}},
        }))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if (msg.get("op") == "event"
                    and msg.get("kind") == "tool_call_result"
                    and (msg.get("payload") or {}).get("request_id") == rid):
                p = msg["payload"]; inner = p.get("result") or {}
                print(f"bus result: outer={p.get('ok')} inner={inner}")
                break
        else:
            print("FAIL — no tool_call_result")
            return 1
    await asyncio.sleep(4)
    if spotify_running():
        print("PASS — Spotify is running")
        return 0
    print("FAIL — Spotify did not start")
    return 1


sys.exit(asyncio.run(main()))
