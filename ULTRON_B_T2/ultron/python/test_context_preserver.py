"""Unit tests for Roadmap #5 — Context Preserver."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ultron_context_preserver import (
    ContextPreserverConfig,
    ContextPreserverService,
    ContextSnapshot,
    get_service,
    init,
    render_packet,
)


def _cfg(tmp_path: Path, **overrides) -> ContextPreserverConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        user_name="Priyanshu",
        packet_md_path=tmp_path / "context_packet.md",
        packet_json_path=tmp_path / "context_packet.json",
        archive_dir=tmp_path / "archive",
        archive_keep=3,
        heartbeat_interval_secs=60.0,
        boot_delay_secs=0.0,
        max_llm_quote_chars=400,
        max_commits=10,
        max_claude_snippet_chars=800,
    )
    defaults.update(overrides)
    return ContextPreserverConfig(**defaults)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# ── Markdown rendering ─────────────────────────────────────────────────


def test_render_packet_includes_all_sections(tmp_path):
    cfg = _cfg(tmp_path)
    snap = ContextSnapshot(
        saved_ts=time.time(),
        reason="shutdown",
        user_name="Priyanshu",
        focus_app="vscode",
        focus_category="editor",
        focus_app_ts=time.time() - 10,
        visual_label="writing context preserver",
        visual_label_ts=time.time() - 12,
        last_user_transcript="hey ultron save the session",
        last_user_ts=time.time() - 20,
        last_llm_response="Saving the session now, sir.",
        last_llm_shard="default",
        last_llm_ts=time.time() - 18,
        flow_state="broken",
        last_flow_break_minutes=42.0,
        last_flow_break_reason="app_switch",
        last_flow_break_app="vscode",
        last_flow_break_ts=time.time() - 600,
        readiness_total=78.0,
        readiness_bucket="ready",
        readiness_components=[
            {"name": "sleep", "score": 40.0, "max_score": 40.0, "detail": "7.5h"},
            {"name": "flow_yesterday", "score": 22.5, "max_score": 30.0, "detail": "80m"},
        ],
        readiness_ts=time.time() - 30,
        interrupts_today_count=4,
        interrupts_top_source="wake_word",
        interrupts_avg_recovery_secs=240.0,
        interrupts_ts=time.time() - 25,
        recent_commits=[
            {"sha": "a" * 40, "subject": "Roadmap #5 — Context Preserver", "ts": time.time() - 90},
        ],
        claude_session_snippet="Working on the context preserver tests.",
        claude_session_ts=time.time() - 15,
    )
    md = render_packet(snap, cfg)
    for header in ("# ULTRON Context Packet",
                   "## Session",
                   "## Last focus",
                   "## Last conversation turn",
                   "## Flow",
                   "## Readiness",
                   "## Interrupts today",
                   "## Git (recent commits)",
                   "## Claude Code session"):
        assert header in md
    # Substance checks.
    assert "vscode" in md
    assert "writing context preserver" in md
    assert "Saving the session now, sir." in md
    assert "42.0 min" in md
    assert "78/100" in md
    assert "primed" in md or "ready" in md
    assert "wake_word" in md
    assert "Roadmap #5 — Context Preserver" in md


def test_render_packet_handles_empty_snapshot(tmp_path):
    cfg = _cfg(tmp_path)
    snap = ContextSnapshot(saved_ts=time.time(), reason="heartbeat",
                           user_name="Priyanshu")
    md = render_packet(snap, cfg)
    # Sections still present, but with empty placeholders.
    assert "## Last focus" in md
    assert "No focus data" in md
    assert "No recent turn" in md
    assert "No interrupts logged today" in md
    assert "No recent commit activity" in md


def test_truncate_quote_clips_at_sentence(tmp_path):
    cfg = _cfg(tmp_path, max_llm_quote_chars=60)
    long_reply = ("Sure. The pipeline ships with prompt caching enabled. "
                  "We can also flip on extended thinking later if needed.")
    snap = ContextSnapshot(saved_ts=time.time(), reason="manual",
                           user_name="Priyanshu",
                           last_llm_response=long_reply,
                           last_llm_ts=time.time() - 5)
    md = render_packet(snap, cfg)
    # Either ends on a sentence boundary or has an ellipsis.
    quoted_line = next(line for line in md.splitlines()
                       if "ULTRON" in line and "Sure" in line)
    assert quoted_line.endswith(".") or "…" in quoted_line


# ── Service writing ────────────────────────────────────────────────────


def test_write_now_creates_packet_files(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = ContextPreserverService(cfg)
    # Seed some data via the sink methods directly.
    svc._on_insight_snapshot({"focus_app": "vscode", "focus_category": "editor"},
                             now=time.time())
    svc._on_llm_response({"text": "Hello sir.", "shard": "default"}, now=time.time())

    async def run():
        info = await svc.write_now("manual")
        assert info["reason"] == "manual"
        assert Path(info["md_path"]).exists()
        assert Path(info["json_path"]).exists()
        # JSON is valid + contains the seeded values.
        data = json.loads(Path(info["json_path"]).read_text(encoding="utf-8"))
        assert data["focus"]["app"] == "vscode"
        assert data["last_turn"]["llm_response"] == "Hello sir."
        # Markdown is non-trivial.
        md = Path(info["md_path"]).read_text(encoding="utf-8")
        assert "vscode" in md and "Hello sir." in md

    loop.run_until_complete(run())


def test_archive_keeps_last_N(tmp_path, loop):
    cfg = _cfg(tmp_path, archive_keep=2)
    svc = ContextPreserverService(cfg)

    async def run():
        for i in range(4):
            svc._on_llm_response({"text": f"reply {i}", "shard": "default"},
                                 now=time.time())
            await svc.write_now("heartbeat")
            # Stagger filesystem mtimes so prune sorts correctly.
            time.sleep(1.1)
        archives = sorted((tmp_path / "archive").glob("context_packet_*.md"))
        # archive_keep=2 means we keep the 2 most recent; first write
        # has nothing to archive (no prior packet exists yet) so total
        # archived = 3 → after prune = 2.
        assert len(archives) <= 2

    loop.run_until_complete(run())


def test_git_dedup_by_sha(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = ContextPreserverService(cfg)

    async def run():
        now = time.time()
        svc._on_git_activity({"commits": [{"sha": "a" * 40, "subject": "first"},
                                          {"sha": "b" * 40, "subject": "second"}],
                              "head": "b" * 40}, now=now)
        # Replay the same payload — should NOT duplicate.
        svc._on_git_activity({"commits": [{"sha": "a" * 40, "subject": "first"}],
                              "head": "a" * 40}, now=now + 60)
        info = await svc.write_now("manual")
        data = json.loads(Path(info["json_path"]).read_text(encoding="utf-8"))
        commits = data["recent_commits"]
        shas = [c["sha"] for c in commits]
        assert sorted(shas) == sorted(set(shas))   # no dupes
        assert "a" * 40 in shas and "b" * 40 in shas

    loop.run_until_complete(run())


def test_handle_voice_shutdown_writes_packet(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = ContextPreserverService(cfg)

    async def run():
        await svc._handle_event({"kind": "voice_shutdown_initiated", "payload": {}})
        await asyncio.sleep(0.1)  # write_now is awaited inline by the handler
        assert cfg.packet_md_path.exists()
        data = json.loads(cfg.packet_json_path.read_text(encoding="utf-8"))
        assert data["reason"] == "shutdown"

    loop.run_until_complete(run())


def test_interrupt_result_only_consumed_for_today_kind(tmp_path, loop):
    cfg = _cfg(tmp_path)
    svc = ContextPreserverService(cfg)
    # Wrong kind — should be ignored.
    svc._on_interrupt_result({"kind": "recent", "rows": []}, now=time.time())
    assert svc.snapshot.interrupts_today_count == 0
    # Right kind — should populate.
    svc._on_interrupt_result({
        "kind": "today",
        "stats": {"count": 5,
                  "by_source": [{"source": "flow_break", "count": 3},
                                {"source": "wake_word", "count": 2}],
                  "avg_recovery_secs": 180.0},
    }, now=time.time())
    assert svc.snapshot.interrupts_today_count == 5
    assert svc.snapshot.interrupts_top_source == "flow_break"
    assert svc.snapshot.interrupts_avg_recovery_secs == 180.0


# ── Singleton ──────────────────────────────────────────────────────────


def test_init_returns_same_instance(tmp_path):
    import ultron_context_preserver as ucp
    ucp._service = None
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    assert a is b
    assert get_service() is a
