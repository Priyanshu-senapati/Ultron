"""Live smoke for the toast bridge.

Publishes one synthetic wellness_nudge event over the WS bus. If the
toast service is running, a real Windows notification should appear in
the action centre within ~1s. We can't programmatically observe the
notification banner from here, so the smoke is "did the bus round-trip
go through without errors, and is the service connected".

Visual confirmation is on you — see the action centre after running.
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

    # Pre-flight: directly invoke the notifier to confirm PowerShell +
    # WinRT actually pop one. This is the most useful proof.
    from ultron_toast.notifier import show
    print("  direct notifier test...")
    ok = show(
        title="ULTRON smoke test",
        body="If you see this, toast bridge is working.",
        footer="ULTRON · smoke",
    )
    if not ok:
        print("FAIL  direct notifier returned False")
        return 1
    print("  direct toast launched.")

    # Now exercise the bus path.
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-toast"}))
        await ws.recv()
        await asyncio.sleep(0.4)
        print("  publishing synthetic wellness_nudge...")
        await ws.send(json.dumps({
            "op": "publish", "kind": "wellness_nudge",
            "payload": {"kind": "low_sleep",
                         "hours": 4.5, "target": 7.5,
                         "date": "2026-05-23"},
        }))
        await asyncio.sleep(2.0)
        print("  publishing synthetic flow_state_changed (broken, 23min)...")
        await ws.send(json.dumps({
            "op": "publish", "kind": "flow_state_changed",
            "payload": {"prev_state": "active", "state": "broken",
                         "duration_minutes": 23.0,
                         "reason": "app_switch",
                         "last_focus_app": "vscode"},
        }))
        await asyncio.sleep(2.0)

    print("PASS  smoke ran cleanly. Check Action Centre for 3 toasts:")
    print("   1. \"ULTRON smoke test\" (direct)")
    print("   2. \"Low sleep last night\" (wellness_nudge)")
    print("   3. \"Flow ended\" (flow break)")
    return 0


sys.exit(asyncio.run(main()))
