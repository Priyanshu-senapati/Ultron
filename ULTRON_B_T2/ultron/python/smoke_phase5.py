"""Phase 5 integration smoke — connects to the live WS bridge and
exercises one cross-module flow per new module.

Run while the stack is up:
    python smoke_phase5.py

Exit code 0 = pass.
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
else:  # pragma: no cover
    import tomli as tomllib


def _read_token() -> tuple[str, str]:
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    return f"ws://{bridge['bind']}/ws", bridge["token"]


async def _subscribe(ws, kinds: list[str]) -> None:
    await ws.send(json.dumps({"op": "subscribe", "kinds": kinds}))


async def _publish(ws, kind: str, payload: dict) -> None:
    await ws.send(json.dumps({"op": "publish", "kind": kind, "payload": payload}))


async def _await_event(ws, kind: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get("op") == "event" and msg.get("kind") == kind:
            return msg
    raise AssertionError(f"timeout waiting for event {kind!r}")


async def _smoke() -> None:
    url, token = _read_token()
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "phase5-smoke"}))
        welcome_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        welcome = json.loads(welcome_raw)
        assert welcome.get("op") == "welcome", f"auth failed: {welcome}"
        await _subscribe(ws, [
            "tool_call_result", "money_recorded", "money_query_result",
            "kg_entity_added", "kg_query_result", "hud_status_tick",
            "wellness_query_result", "plan_query_result",
            "dopamine_query_result", "code_query_result",
        ])

        # 1. Tool registry — call each new query tool via Module E and
        #    verify each one returns a non-error result. This is strictly
        #    stronger than reading the catalog: it proves the handler
        #    runs and the downstream service responds.
        new_tools = (
            ("code_query",     {"kind": "stats"}),
            ("money_query",    {"kind": "monthly_summary"}),
            ("wellness_query", {"kind": "all_streaks"}),
            ("plan_query",     {"kind": "today_summary"}),
            ("kg_query",       {"kind": "stats"}),
            ("dopamine_query", {"kind": "current_score"}),
        )
        for tool_name, args in new_tools:
            request_id = f"smoke-{tool_name}-{int(time.time()*1000)}"
            print(f"   [..] invoking {tool_name} (request_id={request_id})")
            await _publish(ws, "tool_call_request", {
                "request_id": request_id, "name": tool_name, "args": args,
            })
            # code_query has a long internal timeout; others are quick.
            this_timeout = 45.0 if tool_name == "code_query" else 15.0
            deadline = time.monotonic() + this_timeout
            while True:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"timeout waiting for tool_call_result for {tool_name}")
                raw = await asyncio.wait_for(ws.recv(),
                                             timeout=max(0.1, deadline - time.monotonic()))
                msg = json.loads(raw)
                if (msg.get("op") == "event"
                        and msg.get("kind") == "tool_call_result"
                        and (msg.get("payload") or {}).get("request_id") == request_id):
                    pl = msg["payload"]
                    assert pl.get("ok") is True, f"{tool_name} returned not-ok: {pl.get('error')}"
                    # The tool framework's outer ok=True means the handler
                    # didn't raise. The inner result is what we actually
                    # need — verify it's not a "service did not respond"
                    # sentinel.
                    inner = pl.get("result") or {}
                    if isinstance(inner, dict) and inner.get("ok") is False:
                        raise AssertionError(
                            f"{tool_name} inner result not-ok: {inner.get('reason')}"
                        )
                    break
        print(f"[ok] tool registry: invoked {len(new_tools)} new query tools end-to-end")

        # 2. Money — record a tiny test tx and read it back.
        marker = f"smoke-{int(time.time())}"
        await _publish(ws, "money_record_request", {
            "amount": 1.0, "category": "other", "account": "smoke",
            "kind": "expense", "merchant": marker, "note": "phase5",
        })
        await _await_event(ws, "money_recorded", timeout=5.0)
        await _publish(ws, "money_query_request", {
            "kind": "list_transactions", "limit": 5,
        })
        ev = await _await_event(ws, "money_query_result", timeout=5.0)
        rows = ev["payload"].get("rows") or []
        assert any(r.get("merchant") == marker for r in rows), \
            "smoke transaction did not appear in list_transactions"
        print("[ok] money: round-tripped test transaction")

        # 3. KG — add an entity, read stats.
        await _publish(ws, "kg_entity_add_request", {
            "kind": "concept", "name": f"phase5-smoke-{int(time.time())}",
            "attrs": {"source": "smoke"},
        })
        await _await_event(ws, "kg_entity_added", timeout=5.0)
        await _publish(ws, "kg_query_request", {"kind": "stats"})
        ev = await _await_event(ws, "kg_query_result", timeout=5.0)
        stats = ev["payload"].get("stats") or {}
        assert int(stats.get("nodes", 0)) >= 1
        print(f"[ok] kg: stats reports {stats['nodes']} node(s)")

        # 4. Wellness / Plan / Code / Dopamine — round-trip a read.
        # The code service may still be rebuilding its index over C:\dev
        # on first boot, so we give it a generous window.
        for req, resp, payload, timeout in (
            ("wellness_query_request", "wellness_query_result", {"kind": "all_streaks"}, 10.0),
            ("plan_query_request",     "plan_query_result",     {"kind": "today_summary"}, 10.0),
            ("code_query_request",     "code_query_result",     {"kind": "stats"}, 60.0),
            ("dopamine_query_request", "dopamine_query_result", {"kind": "current_score"}, 10.0),
        ):
            await _publish(ws, req, payload)
            await _await_event(ws, resp, timeout=timeout)
        print("[ok] wellness / plan / code / dopamine all responded")

        # 5. HUD — ask for a status tick and verify the new sections.
        await _publish(ws, "hud_status_request", {})
        ev = await _await_event(ws, "hud_status_tick", timeout=10.0)
        snap = ev["payload"]
        for section in ("dopamine", "wellness", "money", "planner", "code", "kg"):
            assert section in snap, f"hud tick missing section {section!r}"
        live = sum(1 for v in snap.values()
                   if isinstance(v, dict) and v.get("available"))
        print(f"[ok] hud tick reports {live} live sections")


def main() -> int:
    try:
        asyncio.run(_smoke())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc!r}")
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
