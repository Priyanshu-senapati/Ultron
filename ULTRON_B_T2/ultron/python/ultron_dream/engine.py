"""Dream engine -- reviews the day's activity during idle periods and
generates insights the user might have missed.

Triggers when the system has been idle for 30+ minutes (user likely
away/sleeping). Collects: git commits, conversation topics, flow
sessions, emotion peaks, app usage patterns, time spent per task.
Feeds a summary to the LLM and asks for non-obvious connections,
missed patterns, and suggestions. Publishes the result as a
``dream_insight`` event which the reentry service can speak as a
morning brief.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import DreamConfig

logger = logging.getLogger("ultron.dream")
IST = ZoneInfo("Asia/Kolkata")


class DreamEngine:
    def __init__(self, cfg: DreamConfig, publish, state=None) -> None:
        self._cfg = cfg
        self._publish = publish
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._last_dream_ts: float = 0.0
        self._last_activity_ts: float = time.monotonic()
        self._dream_delivered: bool = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="dream-engine")
        logger.info("dream engine started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def on_activity(self) -> None:
        self._last_activity_ts = time.monotonic()
        if self._dream_delivered:
            self._dream_delivered = False

    async def _loop(self) -> None:
        while True:
            try:
                idle_mins = (time.monotonic() - self._last_activity_ts) / 60.0
                if (self._cfg.enabled
                        and idle_mins >= self._cfg.idle_threshold_minutes
                        and not self._dream_delivered
                        and time.time() - self._last_dream_ts > 3600):
                    await self._generate_dream()
                    self._dream_delivered = True
                    self._last_dream_ts = time.time()
            except Exception as exc:
                logger.error("dream tick failed: %s", exc)
            await asyncio.sleep(60)

    def _collect_day_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"date": datetime.now(IST).strftime("%Y-%m-%d")}

        try:
            r = subprocess.run(
                ["git", "log", "--since=midnight", "--oneline",
                 "--format=%h %s"],
                capture_output=True, text=True, timeout=5,
                cwd=r"C:\dev",
            )
            commits = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
            summary["git_commits"] = commits[:20]
        except Exception:
            summary["git_commits"] = []

        if self._state:
            sh = getattr(self._state, "syshealth", None) or {}
            summary["system"] = {
                "cpu_percent": sh.get("cpu_percent"),
                "ram_percent": sh.get("ram_percent"),
                "gpu": sh.get("gpu", {}).get("name"),
            }
            emotion = getattr(self._state, "emotion", None) or {}
            summary["last_emotion"] = {
                "valence": emotion.get("valence"),
                "arousal": emotion.get("arousal"),
                "frustration": emotion.get("frustration"),
            }

        now = datetime.now(IST)
        summary["current_time"] = now.strftime("%H:%M")
        summary["idle_since"] = (
            now - timedelta(minutes=self._cfg.idle_threshold_minutes)
        ).strftime("%H:%M")

        return summary

    async def _generate_dream(self) -> None:
        logger.info("dream mode: generating overnight insights...")
        summary = self._collect_day_summary()

        prompt = f"""You are ULTRON's Dream Engine. The user has been idle for {self._cfg.idle_threshold_minutes:.0f}+ minutes (likely sleeping or away).

Review today's activity and generate 3-5 insights the user might have missed. Be specific, actionable, and non-obvious.

TODAY'S DATA:
{json.dumps(summary, indent=2, default=str)}

Rules:
- Each insight should be 1-2 sentences max
- Focus on connections the user might not see: patterns across commits, emotional correlations with productivity, time-of-day effects
- If git commits show repeated work on the same file, suggest the root issue
- If frustration was high, suggest what might have caused it
- If the user worked late, note it gently
- Format as a numbered list
- Speak as ULTRON addressing "sir"
- No preamble, just the insights"""

        try:
            r = subprocess.run(
                ["ollama", "run", self._cfg.ollama_model, prompt],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0 or not r.stdout.strip():
                logger.warning("dream LLM failed: %s", r.stderr[:200] if r.stderr else "empty")
                return

            insights = r.stdout.strip()
            logger.info("dream insights generated (%d chars)", len(insights))

            await self._publish("dream_insight", {
                "insights": insights,
                "summary": summary,
                "ts": time.time(),
            })

            appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
            dream_dir = Path(appdata) / "ULTRON" / "dreams"
            dream_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(IST).strftime("%Y-%m-%d_%H%M")
            (dream_dir / f"dream_{date_str}.md").write_text(
                f"# ULTRON Dream — {date_str}\n\n{insights}\n\n"
                f"## Raw Data\n```json\n{json.dumps(summary, indent=2, default=str)}\n```\n",
                encoding="utf-8",
            )

        except subprocess.TimeoutExpired:
            logger.warning("dream LLM timed out")
        except Exception as exc:
            logger.error("dream generation failed: %s", exc)
