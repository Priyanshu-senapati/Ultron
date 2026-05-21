"""ContextPreserverService — listens to context-bearing bus events and
persists a Markdown + JSON packet on shutdown / heartbeat / request.

Subscribes:
  - ``insight_snapshot``           — focus_app, focus_category.
  - ``visual_label``               — LLaVA vision labels.
  - ``voice_transcript``           — last user turn.
  - ``llm_response``               — last ULTRON turn.
  - ``flow_state_changed``         — flow state + last completed session.
  - ``readiness_score_update``     — score + components.
  - ``interrupt_query_result``     — periodic today's-stats pulls.
  - ``git_activity``               — recent commits.
  - ``claude_session_update``      — most recent Claude Code snippet.
  - ``voice_shutdown_initiated``   — triggers a "shutdown" packet write.
  - ``context_packet_request``     — manual write request.

Publishes:
  - ``context_packet_written``     — fires after each successful write
                                     with the destination paths + reason.
  - ``context_packet_loaded``      — fires once at startup if an
                                     existing packet was found on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import ContextPreserverConfig
from .markdown import render_packet
from .snapshot import ContextSnapshot

logger = logging.getLogger("ultron.context_preserver.service")


class ContextPreserverService:
    def __init__(self, config: ContextPreserverConfig) -> None:
        self._cfg = config
        self._bridge: Optional[UltronBridge] = None
        self._snap = ContextSnapshot(user_name=config.user_name)
        # Rolling commits buffer (most recent first, dedup by sha).
        self._commits: deque[dict[str, Any]] = deque(maxlen=max(10, config.max_commits))
        self._seen_shas: set[str] = set()
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None

    @property
    def snapshot(self) -> ContextSnapshot:
        return self._snap

    # ── Write paths ───────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self._cfg.packet_md_path.parent.mkdir(parents=True, exist_ok=True)
        self._cfg.packet_json_path.parent.mkdir(parents=True, exist_ok=True)
        self._cfg.archive_dir.mkdir(parents=True, exist_ok=True)

    def _archive_existing(self, now: float) -> None:
        if not self._cfg.packet_md_path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        dest = self._cfg.archive_dir / f"context_packet_{stamp}.md"
        try:
            shutil.copy2(self._cfg.packet_md_path, dest)
        except Exception:  # noqa: BLE001
            logger.exception("archive copy failed for %s", dest)
        # Prune by mtime.
        files = sorted(self._cfg.archive_dir.glob("context_packet_*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[self._cfg.archive_keep:]:
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                logger.exception("archive prune failed for %s", old)

    def _write_packet(self, reason: str, now: Optional[float] = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        self._snap.reason = reason
        self._snap.saved_ts = now
        self._snap.recent_commits = list(self._commits)
        md = render_packet(self._snap, self._cfg, now=now)
        js = json.dumps(self._snap.as_dict(), indent=2)
        self._ensure_dirs()
        self._archive_existing(now)
        self._cfg.packet_md_path.write_text(md, encoding="utf-8")
        self._cfg.packet_json_path.write_text(js, encoding="utf-8")
        logger.info("context packet written (reason=%s, %d md chars)",
                    reason, len(md))
        return {
            "reason": reason,
            "saved_ts": now,
            "md_path": str(self._cfg.packet_md_path),
            "json_path": str(self._cfg.packet_json_path),
            "md_chars": len(md),
        }

    async def write_now(self, reason: str) -> dict[str, Any]:
        async with self._lock:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: self._write_packet(reason))
        if self._bridge is not None:
            try:
                await self._bridge.publish("context_packet_written", info)
            except Exception:  # noqa: BLE001
                logger.exception("context_packet_written publish failed")
        return info

    # ── Inbound event sinks ───────────────────────────────────────────

    def _on_insight_snapshot(self, p: dict[str, Any], now: float) -> None:
        app = str(p.get("focus_app") or "").strip()
        cat = str(p.get("focus_category") or "").strip()
        if app:
            self._snap.focus_app = app
            self._snap.focus_category = cat
            self._snap.focus_app_ts = now

    def _on_visual_label(self, p: dict[str, Any], now: float) -> None:
        label = str(p.get("label") or "").strip()
        if label:
            self._snap.visual_label = label
            self._snap.visual_label_ts = now

    def _on_voice_transcript(self, p: dict[str, Any], now: float) -> None:
        text = str(p.get("text") or "").strip()
        if text:
            self._snap.last_user_transcript = text
            self._snap.last_user_ts = now

    def _on_llm_response(self, p: dict[str, Any], now: float) -> None:
        text = str(p.get("text") or "").strip()
        shard = str(p.get("shard") or "").strip()
        if text:
            self._snap.last_llm_response = text
            self._snap.last_llm_shard = shard
            self._snap.last_llm_ts = now

    def _on_flow_state(self, p: dict[str, Any]) -> None:
        state = str(p.get("state") or "")
        prev = str(p.get("prev_state") or "")
        if state:
            self._snap.flow_state = state
        if state == "active":
            self._snap.flow_session_start_ts = float(p.get("ts") or time.time())
        if prev == "active" and state == "broken":
            dur = float(p.get("duration_seconds") or 0.0)
            self._snap.last_flow_break_minutes = round(dur / 60.0, 1)
            self._snap.last_flow_break_reason = str(p.get("reason") or "")
            self._snap.last_flow_break_app = str(p.get("last_focus_app") or "")
            self._snap.last_flow_break_ts = float(p.get("ts") or time.time())

    def _on_readiness(self, p: dict[str, Any], now: float) -> None:
        if "total" in p:
            self._snap.readiness_total = float(p.get("total") or 0.0)
            self._snap.readiness_bucket = str(p.get("bucket") or "")
            self._snap.readiness_components = list(p.get("components") or [])
            self._snap.readiness_ts = now

    def _on_interrupt_result(self, p: dict[str, Any], now: float) -> None:
        if str(p.get("kind") or "") != "today":
            return
        stats = p.get("stats") or {}
        self._snap.interrupts_today_count = int(stats.get("count") or 0)
        by_src = stats.get("by_source") or []
        self._snap.interrupts_top_source = (
            str(by_src[0].get("source")) if by_src else ""
        )
        rec = stats.get("avg_recovery_secs")
        self._snap.interrupts_avg_recovery_secs = (
            float(rec) if rec is not None else None
        )
        self._snap.interrupts_ts = now

    def _on_git_activity(self, p: dict[str, Any], now: float) -> None:
        commits = p.get("commits") or []
        for c in commits:
            if not isinstance(c, dict):
                continue
            sha = str(c.get("sha") or "")
            if not sha or sha in self._seen_shas:
                continue
            entry = {
                "sha": sha,
                "subject": str(c.get("subject") or c.get("message") or ""),
                "ts": c.get("ts") or now,
            }
            self._commits.appendleft(entry)
            self._seen_shas.add(sha)
        # Cap seen set to avoid unbounded growth across long sessions.
        if len(self._seen_shas) > 1000:
            self._seen_shas = set(c["sha"] for c in self._commits)

    def _on_claude_session(self, p: dict[str, Any], now: float) -> None:
        snippet = str(p.get("snippet") or p.get("text") or "").strip()
        if snippet:
            self._snap.claude_session_snippet = snippet
            self._snap.claude_session_ts = now

    # ── Dispatcher ────────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        now = time.time()
        try:
            if kind == "insight_snapshot":
                self._on_insight_snapshot(payload, now)
            elif kind == "visual_label":
                self._on_visual_label(payload, now)
            elif kind == "voice_transcript":
                self._on_voice_transcript(payload, now)
            elif kind == "llm_response":
                self._on_llm_response(payload, now)
            elif kind == "flow_state_changed":
                self._on_flow_state(payload)
            elif kind == "readiness_score_update":
                self._on_readiness(payload, now)
            elif kind == "interrupt_query_result":
                self._on_interrupt_result(payload, now)
            elif kind == "git_activity":
                self._on_git_activity(payload, now)
            elif kind == "claude_session_update":
                self._on_claude_session(payload, now)
            elif kind == "voice_shutdown_initiated":
                await self.write_now("shutdown")
            elif kind == "context_packet_request":
                reason = str(payload.get("reason") or "manual")
                asyncio.create_task(self.write_now(reason))
        except Exception:  # noqa: BLE001
            logger.exception("context preserver handler failed for kind=%s", kind)

    # ── Background loops ──────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(self._cfg.boot_delay_secs)
        while True:
            try:
                await self.write_now("heartbeat")
                await asyncio.sleep(self._cfg.heartbeat_interval_secs)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("heartbeat tick failed")
                await asyncio.sleep(self._cfg.heartbeat_interval_secs)

    async def _interrupts_pull_loop(self) -> None:
        """Periodically nudge the interrupt service for today's stats.

        The interrupt service only publishes its rollup on explicit
        request — without this poll we'd never get a count into the
        packet. Run at roughly half the heartbeat cadence so the data
        is fresh by the time the packet is written.
        """
        await asyncio.sleep(self._cfg.boot_delay_secs / 2)
        interval = max(60.0, self._cfg.heartbeat_interval_secs / 2)
        while True:
            try:
                if self._bridge is not None:
                    await self._bridge.publish("interrupt_query_request",
                                               {"kind": "today"})
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("interrupts pull failed")
                await asyncio.sleep(interval)

    async def _publish_packet_loaded_if_any(self) -> None:
        path = self._cfg.packet_json_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("could not read existing packet at %s", path)
            return
        if self._bridge is None:
            return
        try:
            await self._bridge.publish("context_packet_loaded", {
                "md_path": str(self._cfg.packet_md_path),
                "json_path": str(self._cfg.packet_json_path),
                "previous": data,
            })
            logger.info("context_packet_loaded broadcast (prior reason=%s)",
                        data.get("reason"))
        except Exception:  # noqa: BLE001
            logger.exception("context_packet_loaded publish failed")

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start context preserver")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "insight_snapshot",
                "visual_label",
                "voice_transcript",
                "llm_response",
                "flow_state_changed",
                "readiness_score_update",
                "interrupt_query_result",
                "git_activity",
                "claude_session_update",
                "voice_shutdown_initiated",
                "context_packet_request",
            ],
            role="context-preserver",
        )
        logger.info("ContextPreserverService starting — md=%s",
                    self._cfg.packet_md_path)
        # On startup: announce the prior packet so other services can
        # surface "where we left off".
        asyncio.create_task(self._publish_packet_loaded_if_any())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._stats_task = asyncio.create_task(self._interrupts_pull_loop())
        try:
            await self._bridge.run_forever()
        finally:
            for t in (self._heartbeat_task, self._stats_task):
                if t is not None:
                    t.cancel()
