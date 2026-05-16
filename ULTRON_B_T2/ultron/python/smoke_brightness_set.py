"""Verify brightness SET works end-to-end and the hardware obeys."""
from __future__ import annotations

import asyncio, json, os, subprocess, sys, time
from pathlib import Path
import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def hw_brightness() -> int:
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip().splitlines()
    return int(out[0]) if out else -1


async def set_via_bus(level: int) -> tuple[bool, dict]:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]
    async with websockets.connect(url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"op":"hello","token":token,"role":"smoke-bright-set"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":["tool_call_result"]}))
        rid = f"smoke-{int(time.time()*1000)}"
        await ws.send(json.dumps({
            "op":"publish","kind":"tool_call_request",
            "payload": {"request_id": rid, "name": "brightness",
                        "args": {"action": "set", "level": level}},
        }))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if (msg.get("op") == "event"
                    and msg.get("kind") == "tool_call_result"
                    and (msg.get("payload") or {}).get("request_id") == rid):
                p = msg["payload"]
                return bool(p.get("ok")), (p.get("result") or {})
    return False, {}


async def main() -> int:
    start = hw_brightness()
    print(f"hardware before: {start}")
    target = 25 if start != 25 else 50
    ok, result = await set_via_bus(target)
    print(f"bus result: ok={ok} inner={result}")
    await asyncio.sleep(0.4)
    end = hw_brightness()
    print(f"hardware after: {end}  (target {target})")
    if end == target:
        print("PASS — bus call actually changed hardware brightness")
        # Restore original
        await set_via_bus(start)
        return 0
    print("FAIL — bus reported success but hardware unchanged")
    return 1


sys.exit(asyncio.run(main()))
