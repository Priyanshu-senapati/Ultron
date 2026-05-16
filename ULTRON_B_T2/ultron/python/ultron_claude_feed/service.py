"""ClaudeFeedService — append ULTRON failures to a markdown feed.

Subscribes:
  - ``tool_call_result``  — capture every result with ok=False
  - ``tool_call_audit``   — same, defensive: catches the audit shape
  - ``llm_error``         — explicit LLM errors (if/when published)
  - ``voice_error``       — voice engine errors (if/when published)
  - ``claude_feed_request`` — explicit "log this to the feed" request
                              with payload {kind, summary, detail}

Publishes:
  - nothing on the wire — purely a sink. Reads its own file via the
    file-system (Claude Code reads it too).

The feed format is one ``## <timestamp> — <kind>`` entry per event,
with an indented code block for raw payload. Easy to scan visually
and easy for Claude to grep.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ultron_bridge import UltronBridge

from .config import ClaudeFeedConfig

logger = logging.getLogger("ultron.claude_feed")

_IST = ZoneInfo("Asia/Kolkata")


class ClaudeFeedService:
    def __init__(self, config: ClaudeFeedConfig) -> None:
        self._cfg = config
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()
        config.feed_dir.mkdir(parents=True, exist_ok=True)
        # Drop a one-time README explaining what this folder is so a
        # human stumbling on it knows what they're looking at.
        readme = config.feed_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# ULTRON → Claude feed\n\n"
                "This folder is an auto-generated stream of ULTRON failures and\n"
                "explicit notes for Claude Code. Each day gets its own file.\n"
                "Telling Claude `look at the ULTRON feed` is the intended use.\n",
                encoding="utf-8",
            )

    # ── Public Python API ──────────────────────────────────────────────

    async def append(self, kind: str, summary: str,
                     detail: dict[str, Any] | None = None) -> None:
        """Append a single entry to today's feed file. Thread/async safe."""
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._write(kind, summary, detail or {}),
            )

    def _write(self, kind: str, summary: str, detail: dict[str, Any]) -> None:
        now = datetime.now(_IST)
        day = now.strftime("%Y-%m-%d")
        ts = now.strftime("%H:%M:%S IST")
        path = self._roll_path(self._cfg.feed_dir / f"{day}.md")
        entry_lines = [
            f"## {ts} — {kind}",
            "",
            summary.strip() or "(no summary)",
            "",
        ]
        if detail:
            try:
                pretty = json.dumps(detail, indent=2, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                pretty = repr(detail)
            entry_lines += ["```json", pretty[:8000], "```", ""]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(entry_lines))

    def _roll_path(self, base: Path) -> Path:
        """If base exists and exceeds the size cap, roll over to .1, .2, …"""
        if not base.exists() or base.stat().st_size < self._cfg.max_file_bytes:
            return base
        # Roll: find the highest numbered companion and bump.
        i = 1
        while True:
            candidate = base.with_suffix(base.suffix + f".{i}")
            if not candidate.exists() or candidate.stat().st_size < self._cfg.max_file_bytes:
                return candidate
            i += 1
            if i > 99:    # extreme safety; should never hit
                return candidate

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start claude_feed")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "tool_call_result",
                "tool_call_audit",
                "llm_error",
                "voice_error",
                "claude_feed_request",
            ],
            role="claude-feed",
        )
        logger.info("ClaudeFeedService starting — feed_dir=%s", self._cfg.feed_dir)
        # Drop a startup marker so the first session boundary is visible.
        await self.append("boot", "claude-feed service started")
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "tool_call_result":
                ok = bool(payload.get("ok"))
                inner = payload.get("result") or {}
                inner_ok = bool(inner.get("ok")) if isinstance(inner, dict) else ok
                if (not ok) or (not inner_ok):
                    name = payload.get("name", "?")
                    reason = (inner.get("reason")
                              if isinstance(inner, dict) and inner.get("reason")
                              else payload.get("error") or "(no reason)")
                    await self.append(
                        kind=f"tool-error: {name}",
                        summary=str(reason),
                        detail=payload,
                    )
                elif self._cfg.log_successes:
                    await self.append(
                        kind=f"tool-ok: {payload.get('name','?')}",
                        summary=str(inner)[:200] if inner else "",
                        detail=payload,
                    )
            elif kind == "tool_call_audit":
                # We already log failures via tool_call_result. The audit
                # event is the same payload with a fuller chain; only
                # capture if explicitly flagged "failed" by E.
                if str(payload.get("kind", "")).lower() in ("failed", "rejected"):
                    await self.append(
                        kind="tool-audit-failure",
                        summary=str(payload.get("reason") or "(audit-flagged)"),
                        detail=payload,
                    )
            elif kind == "llm_error":
                await self.append(
                    kind="llm-error",
                    summary=str(payload.get("message") or payload.get("error") or "(none)"),
                    detail=payload,
                )
            elif kind == "voice_error":
                await self.append(
                    kind="voice-error",
                    summary=str(payload.get("message") or payload.get("error") or "(none)"),
                    detail=payload,
                )
            elif kind == "claude_feed_request":
                await self.append(
                    kind=str(payload.get("kind") or "note"),
                    summary=str(payload.get("summary") or ""),
                    detail=payload.get("detail") or {},
                )
        except Exception:  # noqa: BLE001
            logger.exception("claude_feed handler failed for kind=%s", kind)
