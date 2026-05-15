"""Tests for Module L (HUD aggregator)."""
from __future__ import annotations

from typing import Any

import pytest

from ultron_hud.aggregator import HudAggregator
from ultron_hud.config import HudConfig
from ultron_hud.service import HudService


def _cfg() -> HudConfig:
    return HudConfig(ws_url="ws://x", ws_token="t",
                     tick_seconds=5, enable_tray=False)


@pytest.mark.asyncio
async def test_aggregator_unwired_returns_unavailable() -> None:
    """No request_response set → every section reports unavailable."""
    snap = await HudAggregator().snapshot()
    for key in ("dopamine", "wellness", "money", "planner", "code", "kg"):
        assert key in snap
        assert snap[key]["available"] is False


@pytest.mark.asyncio
async def test_aggregator_with_fake_rr_picks_up_data() -> None:
    async def fake_rr(req: str, payload: dict, resp: str, timeout: float) -> dict:
        # Return a plausible result per response kind.
        if resp == "dopamine_query_result":
            return {"score": 1.5}
        if resp == "wellness_query_result":
            if payload.get("kind") == "all_streaks":
                return {"streaks": [{"kind": "workout", "current": 3}]}
            return {"metrics": {"weight_kg": 70}}
        if resp == "money_query_result":
            if payload.get("kind") == "monthly_summary":
                return {"summary": {"net": 1000}}
            return {"rows": [{"status": "over", "category": "food"}]}
        if resp == "plan_query_result":
            return {"summary": {"blocks": [{"title": "focus"}], "events": []}}
        if resp == "code_query_result":
            return {"stats": {"nodes": 100}}
        if resp == "kg_query_result":
            return {"stats": {"nodes": 5}}
        return {}

    ag = HudAggregator(request_response=fake_rr)
    snap = await ag.snapshot()
    assert snap["dopamine"] == {"available": True, "score": 1.5}
    assert snap["wellness"]["available"] is True
    assert snap["wellness"]["streaks"][0]["kind"] == "workout"
    assert snap["money"]["available"] is True
    assert len(snap["money"]["budget_alerts"]) == 1
    assert snap["planner"]["available"] is True
    assert snap["planner"]["next_blocks"][0]["title"] == "focus"
    assert snap["code"]["stats"]["nodes"] == 100
    assert snap["kg"]["stats"]["nodes"] == 5


@pytest.mark.asyncio
async def test_aggregator_timeout_marks_unavailable() -> None:
    async def slow_rr(req: str, payload: dict, resp: str, timeout: float) -> Any:
        return None  # caller treats None as timeout

    ag = HudAggregator(request_response=slow_rr)
    snap = await ag.snapshot()
    for key in ("dopamine", "wellness", "money", "planner", "code", "kg"):
        assert snap[key]["available"] is False


@pytest.mark.asyncio
async def test_aggregator_partial_failure_isolated() -> None:
    """If one section's first call returns None, that section is
    unavailable but the rest still come through."""
    async def rr(req: str, payload: dict, resp: str, timeout: float) -> Any:
        if resp == "money_query_result":
            return None
        return {"score": 0.0, "streaks": [], "metrics": {},
                "summary": {}, "rows": [], "stats": {}}

    ag = HudAggregator(request_response=rr)
    snap = await ag.snapshot()
    assert snap["money"]["available"] is False
    assert snap["dopamine"]["available"] is True
    assert snap["wellness"]["available"] is True
    assert snap["code"]["available"] is True


@pytest.mark.asyncio
async def test_hud_snapshot_carries_timestamp() -> None:
    svc = HudService(_cfg())
    snap = await svc.snapshot()
    assert "ts" in snap
    assert snap["ts"] > 0


def test_hud_service_singleton() -> None:
    from ultron_hud import get_service, init
    import ultron_hud
    ultron_hud._service = None  # noqa: SLF001
    a = init(_cfg())
    b = init(_cfg())
    c = get_service()
    assert a is b is c
    ultron_hud._service = None  # noqa: SLF001


def test_tray_no_pystray_is_no_op() -> None:
    from ultron_hud.tray import TrayIcon
    t = TrayIcon()
    t.start()
    t.set_title("X")
    t.stop()
