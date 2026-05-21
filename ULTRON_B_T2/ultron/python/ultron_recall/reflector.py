"""LLM-driven session / day reflector.

Reads turns from a period (session boot id, or a calendar day) and asks
the local LLM for a concise ~300-word summary: topics discussed,
decisions made, open threads, mood. The summary is embedded and stored
in the ``reflections`` table.

Subsequent recall queries can surface reflections alongside individual
turns, giving the LLM compressed long-range memory.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from ultron_knowledge.embedder import Embedder
from ultron_llm.client_ollama import OllamaClient

from .store import RecallStore, StoredTurn

logger = logging.getLogger("ultron.recall.reflector")


SYSTEM_PROMPT_SESSION = (
    "You write tight session reflections for a personal AI's long-term "
    "memory. Read the turns and produce a single paragraph (≈250 words "
    "max) covering: what was worked on, what was decided, what's left "
    "open, and the user's general mood / state. Write in third person "
    "('the user...'). Be specific — name files, projects, decisions. "
    "Do NOT include AI's own personality flavor. Output the paragraph "
    "only, no headers, no markdown."
)

SYSTEM_PROMPT_DAY = (
    "You write daily reflections for a personal AI's long-term memory. "
    "Read all turns from one calendar day and produce one tight "
    "paragraph (≈300 words max) covering: themes of the day, decisions "
    "made, mood arc, and unresolved threads. Write in third person, "
    "be specific, name what was worked on. Output the paragraph only."
)


class Reflector:
    def __init__(self, store: RecallStore, *, ollama_url: str,
                 ollama_model: str, embedder: Embedder,
                 max_chars: int = 1200) -> None:
        self._store = store
        self._client = OllamaClient(base_url=ollama_url,
                                    default_model=ollama_model,
                                    request_timeout=120.0)
        self._embedder = embedder
        self._max_chars = max_chars

    def _format_turns(self, turns: list[StoredTurn]) -> str:
        lines: list[str] = []
        for t in turns:
            ts = time.strftime("%H:%M", time.localtime(t.ts))
            content = t.content.strip().replace("\n", " ")
            if len(content) > 400:
                content = content[:397] + "…"
            lines.append(f"[{ts}] {t.role.upper()}: {content}")
        return "\n".join(lines)

    async def _summarise(self, system_prompt: str, turns: list[StoredTurn]) -> str:
        if not turns:
            return ""
        prompt = ("Turns to reflect on:\n\n"
                  + self._format_turns(turns)
                  + "\n\nWrite the reflection.")
        try:
            text = await self._client.chat(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ollama call failed in reflector: %s", exc)
            return ""
        text = text.strip()
        if len(text) > self._max_chars:
            text = text[: self._max_chars].rstrip() + "…"
        return text

    async def reflect_session(self, conv_id: str) -> dict[str, Any]:
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,role,content,conv_id FROM turns "
                "WHERE conv_id = ? ORDER BY ts ASC",
                (conv_id,),
            ).fetchall()
        if len(rows) < 4:
            return {"skipped": "too_few_turns", "turn_count": len(rows)}
        turns = [StoredTurn(id=int(r["id"]), ts=float(r["ts"]),
                            role=str(r["role"]), content=str(r["content"]),
                            conv_id=str(r["conv_id"])) for r in rows]
        summary = await self._summarise(SYSTEM_PROMPT_SESSION, turns)
        if not summary:
            return {"skipped": "empty_summary", "turn_count": len(turns)}
        emb = self._embedder.encode_one(summary)
        rid = self._store.insert_reflection(
            period_start_ts=turns[0].ts,
            period_end_ts=turns[-1].ts,
            period_kind="session",
            summary=summary,
            embedding=emb,
        )
        return {
            "reflection_id": rid,
            "period_kind": "session",
            "conv_id": conv_id,
            "turn_count": len(turns),
            "summary_chars": len(summary),
            "summary_preview": summary[:200],
        }

    async def reflect_day(self, *, day_start_ts: float,
                          day_end_ts: float) -> dict[str, Any]:
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,role,content,conv_id FROM turns "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (day_start_ts, day_end_ts),
            ).fetchall()
        if len(rows) < 6:
            return {"skipped": "too_few_turns", "turn_count": len(rows)}
        turns = [StoredTurn(id=int(r["id"]), ts=float(r["ts"]),
                            role=str(r["role"]), content=str(r["content"]),
                            conv_id=str(r["conv_id"])) for r in rows]
        summary = await self._summarise(SYSTEM_PROMPT_DAY, turns)
        if not summary:
            return {"skipped": "empty_summary", "turn_count": len(turns)}
        emb = self._embedder.encode_one(summary)
        rid = self._store.insert_reflection(
            period_start_ts=day_start_ts,
            period_end_ts=day_end_ts,
            period_kind="day",
            summary=summary,
            embedding=emb,
        )
        return {
            "reflection_id": rid,
            "period_kind": "day",
            "turn_count": len(turns),
            "summary_chars": len(summary),
            "summary_preview": summary[:200],
        }
