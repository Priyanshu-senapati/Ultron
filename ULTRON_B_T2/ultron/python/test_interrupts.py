"""Unit tests for Roadmap #4 — Interrupt Ledger.

Direct against the InterruptStore + InterruptService (no live WS).
We exercise the store's recovery pairing logic by driving the service's
internal methods through asyncio.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from ultron_interrupts import (
    Interrupt,
    InterruptConfig,
    InterruptService,
    InterruptStore,
    get_service,
    init,
)


def _cfg(tmp_path, **overrides) -> InterruptConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        db_path=str(tmp_path / "intr.db"),
        record_wake_word=True,
        record_flow_break=True,
        record_wellness_nudge=True,
        record_reentry=False,
        min_flow_break_duration_secs=60.0,
        recovery_window_secs=1800.0,
        max_pending_interrupts=200,
    )
    defaults.update(overrides)
    return InterruptConfig(**defaults)


# ── Store ──────────────────────────────────────────────────────────────


def test_store_record_and_recent(tmp_path):
    store = InterruptStore(_cfg(tmp_path))
    intr = Interrupt(ts=1000.0, source="flow_break", detail="x", focus_app="vscode")
    iid = store.record(intr)
    assert iid > 0
    rows = store.recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["source"] == "flow_break"
    assert rows[0]["focus_app"] == "vscode"


def test_store_update_recovery(tmp_path):
    store = InterruptStore(_cfg(tmp_path))
    intr = Interrupt(ts=1000.0, source="flow_break", detail="x")
    iid = store.record(intr)
    store.update_recovery(iid, recovery_secs=320.0, recovery_ts=1320.0)
    rows = store.recent(limit=1)
    assert rows[0]["recovery_secs"] == pytest.approx(320.0)
    assert rows[0]["recovery_ts"] == pytest.approx(1320.0)


def test_store_stats_groups_by_source(tmp_path):
    store = InterruptStore(_cfg(tmp_path))
    base_ts = time.time() - 3600.0
    for i in range(3):
        store.record(Interrupt(ts=base_ts + i, source="flow_break"))
    for i in range(2):
        store.record(Interrupt(ts=base_ts + 10 + i, source="wake_word"))
    s = store.stats(since_ts=base_ts - 100)
    assert s["count"] == 5
    sources = {row["source"]: row["count"] for row in s["by_source"]}
    assert sources["flow_break"] == 3
    assert sources["wake_word"] == 2


# ── Service: recovery pairing ──────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def test_recovery_pairs_pending_inside_window(tmp_path, loop):
    cfg = _cfg(tmp_path, recovery_window_secs=600.0)
    svc = InterruptService(cfg)

    async def run():
        # Two interrupts ~1 min apart.
        await svc._record("flow_break", "x", ts=1000.0, focus_app="vscode")
        await svc._record("wake_word", "y", ts=1060.0, focus_app="vscode")
        assert svc.pending_count == 2
        # Recovery 5 min after the first.
        await svc._pair_recoveries(recovery_ts=1300.0)
        # Both fit inside the 600s window — both recovered.
        rows = svc.store.recent(limit=10)
        recs = {r["source"]: r["recovery_secs"] for r in rows}
        assert recs["flow_break"] == pytest.approx(300.0)
        assert recs["wake_word"] == pytest.approx(240.0)
        assert svc.pending_count == 0

    loop.run_until_complete(run())


def test_recovery_drops_stale_pending(tmp_path, loop):
    cfg = _cfg(tmp_path, recovery_window_secs=300.0)
    svc = InterruptService(cfg)

    async def run():
        # Interrupt at ts=1000.
        await svc._record("flow_break", "x", ts=1000.0)
        # Recovery happens way later — beyond the 300s window.
        await svc._pair_recoveries(recovery_ts=1500.0)
        rows = svc.store.recent(limit=1)
        assert rows[0]["recovery_secs"] is None
        # Stale entry was dropped from pending.
        assert svc.pending_count == 0

    loop.run_until_complete(run())


def test_recovery_keeps_already_paired_entries(tmp_path, loop):
    cfg = _cfg(tmp_path, recovery_window_secs=1800.0)
    svc = InterruptService(cfg)

    async def run():
        await svc._record("flow_break", "x", ts=1000.0)
        await svc._pair_recoveries(recovery_ts=1100.0)
        # A second recovery later — the previously-paired interrupt
        # should already be out of pending.
        await svc._record("wake_word", "y", ts=1200.0)
        await svc._pair_recoveries(recovery_ts=1300.0)
        rows = svc.store.recent(limit=10)
        recs = {r["source"]: r["recovery_secs"] for r in rows}
        assert recs["flow_break"] == pytest.approx(100.0)
        assert recs["wake_word"] == pytest.approx(100.0)

    loop.run_until_complete(run())


# ── Service: source filtering ──────────────────────────────────────────


def test_voice_transcript_only_logs_when_present(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = InterruptService(cfg)

    async def run():
        # Default presence state is "present" — should log.
        await svc._on_voice_transcript({"text": "Hey Ultron play music"})
        # Now away — should NOT log.
        svc._presence_state = "away"
        await svc._on_voice_transcript({"text": "Hey Ultron resume"})
        rows = svc.store.recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["source"] == "wake_word"

    loop.run_until_complete(run())


def test_flow_break_below_floor_is_ignored(tmp_path, loop):
    cfg = _cfg(tmp_path, min_flow_break_duration_secs=60.0)
    svc = InterruptService(cfg)

    async def run():
        # 30 seconds of "flow" — too short to count.
        await svc._on_flow_state({"prev_state": "active", "state": "broken",
                                  "duration_seconds": 30.0,
                                  "reason": "app_switch", "ts": 1000.0})
        assert svc.store.recent(limit=1) == []
        # 2 minutes of flow → real interrupt.
        await svc._on_flow_state({"prev_state": "active", "state": "broken",
                                  "duration_seconds": 120.0,
                                  "reason": "app_switch", "ts": 1100.0})
        rows = svc.store.recent(limit=1)
        assert len(rows) == 1
        assert rows[0]["source"] == "flow_break"
        assert "broke flow after 2m" in rows[0]["detail"]

    loop.run_until_complete(run())


def test_wellness_nudge_records(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = InterruptService(cfg)

    async def run():
        await svc._on_wellness_nudge({"kind": "low_sleep"})
        rows = svc.store.recent(limit=1)
        assert rows[0]["source"] == "wellness_nudge"
        assert "low_sleep" in rows[0]["detail"]

    loop.run_until_complete(run())


def test_reentry_opt_in(tmp_path, loop):
    cfg = _cfg(tmp_path, record_reentry=False)
    svc = InterruptService(cfg)

    async def run():
        await svc._on_presence({"state": "returning", "prev_state": "away",
                                "away_duration_seconds": 480.0})
        assert svc.store.recent(limit=1) == []
        # Flip the flag and try again.
        svc._cfg.record_reentry = True
        await svc._on_presence({"state": "returning", "prev_state": "away",
                                "away_duration_seconds": 480.0})
        rows = svc.store.recent(limit=1)
        assert rows[0]["source"] == "reentry"

    loop.run_until_complete(run())


# ── Singleton ──────────────────────────────────────────────────────────


def test_init_returns_same_instance(tmp_path):
    import ultron_interrupts as ui
    ui._service = None
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    assert a is b
    assert get_service() is a
