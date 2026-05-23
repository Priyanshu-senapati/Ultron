"""SelfTunerService — runs the daily reflection + observers.

Subscribes:
  - ``tool_call_audit``        → update tool usage stats
  - ``emotion_state_changed``  → update mood histogram
  - ``self_reflect_request``   → reflect now (manual / smoke trigger)

Publishes:
  - ``self_reflection_written`` → after each successful reflection
                                  with the markdown path + summary.
  - ``tuning_suggestion``       → fires per suggestion so HUD / voice
                                  could surface them in real time.

Persists:
  - ``self_reflections/YYYY-MM-DD.md`` — the dated reflection.
  - ``self_reflections/latest.md``     — symlink-ish copy for quick read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import SelfTunerConfig
from .observer import EmotionObserver, ToolUsageObserver
from .reflector import gather_facts, render_markdown
from .tuner import suggest

logger = logging.getLogger("ultron.selftuner.service")


class SelfTunerService:
    def __init__(self, config: SelfTunerConfig) -> None:
        self._cfg = config
        self._tool_obs = ToolUsageObserver(config.tool_usage_window_secs)
        self._emotion_obs = EmotionObserver(config.tool_usage_window_secs)
        self._bridge: Optional[UltronBridge] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_reflection_ts: float = 0.0
        self._last_reflection_info: dict[str, Any] = {}

    @property
    def tool_observer(self) -> ToolUsageObserver:
        return self._tool_obs

    @property
    def emotion_observer(self) -> EmotionObserver:
        return self._emotion_obs

    # ── Reflection ────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self._cfg.reflection_dir.mkdir(parents=True, exist_ok=True)

    def _write_reflection(self, now: float) -> dict[str, Any]:
        self._ensure_dirs()
        facts = gather_facts(self._cfg, self._tool_obs, self._emotion_obs,
                             now=now)
        suggestions = suggest(facts, self._cfg)
        md = render_markdown(facts, suggestions)
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        dated_path = self._cfg.reflection_dir / f"{date_str}.md"
        dated_path.write_text(md, encoding="utf-8")
        self._cfg.latest_md_path.write_text(md, encoding="utf-8")
        json_path = self._cfg.reflection_dir / f"{date_str}.json"
        json_path.write_text(
            json.dumps({"facts": facts, "suggestions": suggestions},
                       indent=2),
            encoding="utf-8",
        )
        return {
            "ts": now,
            "date": date_str,
            "md_path": str(dated_path),
            "json_path": str(json_path),
            "latest_md_path": str(self._cfg.latest_md_path),
            "suggestion_count": len(suggestions),
            "md_chars": len(md),
        }

    async def reflect_now(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        now = time.time()
        info = await loop.run_in_executor(None, lambda: self._write_reflection(now))
        self._last_reflection_ts = now
        self._last_reflection_info = info
        if self._bridge is not None:
            try:
                await self._bridge.publish("self_reflection_written", info)
            except Exception:  # noqa: BLE001
                logger.exception("self_reflection_written publish failed")
            # Surface suggestions individually so any consumer can act.
            try:
                json_path = Path(info["json_path"])
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                for s in (payload.get("suggestions") or []):
                    await self._bridge.publish("tuning_suggestion", s)
            except Exception:  # noqa: BLE001
                logger.exception("tuning_suggestion publish loop failed")
        logger.info("self-reflection written: %s (%d chars, %d suggestions)",
                    info["md_path"], info["md_chars"], info["suggestion_count"])
        return info

    # ── Bus handler ───────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "tool_call_audit":
                name = str(payload.get("name") or "")
                if not name:
                    return
                # ``ok`` may be at top level or inside result.
                ok = bool(payload.get("ok"))
                if not payload.get("ok") and isinstance(payload.get("result"),
                                                        dict):
                    ok = bool(payload["result"].get("ok"))
                reason = ""
                if not ok:
                    res = payload.get("result") or {}
                    reason = str(res.get("reason") or
                                 payload.get("reason") or "")
                self._tool_obs.record(name, ok=ok, error_reason=reason)
            elif kind == "emotion_state_changed":
                self._emotion_obs.record(payload)
            elif kind == "self_reflect_request":
                asyncio.create_task(self.reflect_now())
        except Exception:  # noqa: BLE001
            logger.exception("selftuner handler failed for kind=%s", kind)

    # ── Background heartbeat ──────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        await asyncio.sleep(self._cfg.boot_delay_secs)
        while True:
            try:
                await self.reflect_now()
                await asyncio.sleep(self._cfg.reflection_interval_secs)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("selftuner heartbeat failed")
                await asyncio.sleep(self._cfg.reflection_interval_secs)

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start selftuner")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "tool_call_audit",
                "emotion_state_changed",
                "self_reflect_request",
            ],
            role="selftuner",
        )
        logger.info(
            "SelfTunerService starting — reflections=%s interval=%.0fs",
            self._cfg.reflection_dir, self._cfg.reflection_interval_secs,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._bridge.run_forever()
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
