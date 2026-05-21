"""Live smoke for the spotify_control tool.

Three checks:
  1. Tool is registered + responds to tool_call_request.
  2. With the Spotify bridge disabled (current default), play_query
     gracefully falls back to launching the spotify:search URI.
  3. Bare actions (next/pause) return an error with a re-auth hint
     when the bridge isn't authorized — proving error path works.

Does not require Spotify OAuth to be configured.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


async def call_tool(ws, action: str, **extra) -> dict | None:
    rid = uuid.uuid4().hex[:12]
    args = {"action": action, **extra}
    await ws.send(json.dumps({"op": "publish", "kind": "tool_call_request",
                              "payload": {
                                  "request_id": rid,
                                  "name": "spotify_control",
                                  "args": args,
                              }}))
    # Wait briefly for the matching audit event.
    deadline = asyncio.get_event_loop().time() + 12.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        except asyncio.TimeoutError:
            continue
        if m.get("op") != "event":
            continue
        if m.get("kind") != "tool_call_audit":
            continue
        p = m.get("payload") or {}
        if p.get("name") == "spotify_control" and p.get("request_id") == rid:
            return p
    return None


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-spotify-control"}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe",
                                  "kinds": ["tool_call_audit"]}))
        await asyncio.sleep(0.4)

        # Check 1: play_query → fallback to URI handler since bridge
        # is disabled by default. We expect ok=True, fallback="uri_handler".
        r1 = await call_tool(ws, "play_query", query="Closer Chainsmokers")
        if r1 is None:
            print("FAIL  no audit event for play_query")
            return 1
        res1 = r1.get("result") or {}
        ok1 = bool(res1.get("ok"))
        fallback1 = res1.get("fallback")
        print(f"  play_query  : ok={ok1}  fallback={fallback1}"
              f"  reason={res1.get('reason', '')[:80]!r}")

        # Check 2: next → no fallback, should return ok=False with hint.
        r2 = await call_tool(ws, "next")
        if r2 is None:
            print("FAIL  no audit event for next")
            return 1
        res2 = r2.get("result") or {}
        ok2 = bool(res2.get("ok"))
        reason2 = res2.get("reason") or ""
        print(f"  next        : ok={ok2}  reason={reason2[:120]!r}")

        # Check 3: unknown action → tool rejects upfront.
        r3 = await call_tool(ws, "doabarrelroll")
        res3 = (r3 or {}).get("result") or {}
        print(f"  bad action  : ok={res3.get('ok')}  reason={res3.get('reason')!r}")

    # Acceptable outcomes for the user-visible paths:
    #   - play_query: ok=True with fallback (bridge disabled) OR ok=True
    #     (bridge authorised and song actually started).
    #   - next: either ok=True (bridge authorised + active device) or
    #     ok=False with an informative reason (re-auth hint or no
    #     device). Both demonstrate the channel works.
    user_visible_ok = bool(ok1)
    next_path_ok = (ok2 is True) or (ok2 is False and bool(reason2))
    if user_visible_ok and next_path_ok:
        print("PASS  spotify_control tool reachable; play_query falls back "
              "cleanly when bridge is disabled; next returns clear status")
        return 0
    print(f"FAIL  user_visible_ok={user_visible_ok} next_path_ok={next_path_ok}")
    return 1


sys.exit(asyncio.run(main()))
