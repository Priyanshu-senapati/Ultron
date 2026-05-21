"""LLM-driven fact extractor.

Reads recent un-extracted turns from the ``turns`` table and asks the
local LLM to surface durable (subject, predicate, object) triples about
the user and their context. Results are written to the ``facts`` table
(dedup'd by uniqueness constraint).

Why not go through Module C? The C-only-LLM rule exists to keep
user-facing conversation behind a single privacy gate. The extractor
operates on already-stored turns — there's no fresh user input, no
personality, no privacy decision to make. Going to Ollama directly
keeps the code path simple and avoids re-publishing through the bus
for an internal job.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from ultron_llm.client_ollama import OllamaClient

from .store import RecallStore, StoredTurn

logger = logging.getLogger("ultron.recall.extractor")


SYSTEM_PROMPT = (
    "You read conversation turns between a user and an AI assistant. "
    "Your only job is to extract DURABLE FACTS the user has shared about "
    "themselves or their immediate context — things that will still be "
    "true in a week. Examples: name, family members, pets, profession, "
    "current projects, strong preferences, location, allergies, hobbies. "
    "Do NOT extract: weather, transient mood, one-off requests, hypothetical "
    "statements, AI's own claims about itself.\n\n"
    "Output strict JSON: an array of objects with keys 'subject', 'predicate', "
    "'object'. Subjects are short noun phrases ('user', 'user's dog', "
    "'user's project ULTRON'). Predicates are short verbs/relations "
    "('is named', 'works on', 'prefers', 'lives in', 'has'). Objects are "
    "the value. Empty array if no durable facts.\n\n"
    "Return ONLY the JSON array. No prose, no markdown fences."
)


_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", re.DOTALL)


@dataclass
class ExtractedFact:
    subject: str
    predicate: str
    object: str
    source_turn_id: Optional[int] = None


def _parse_facts(text: str) -> list[dict[str, str]]:
    """Best-effort JSON-array extraction from the LLM output."""
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Try direct parse first.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        s = str(item.get("subject", "")).strip()
        p = str(item.get("predicate", "")).strip()
        o = str(item.get("object", "")).strip()
        if s and p and o and len(s) < 80 and len(p) < 80 and len(o) < 200:
            out.append({"subject": s, "predicate": p, "object": o})
    return out


class FactExtractor:
    def __init__(self, store: RecallStore, *, ollama_url: str,
                 ollama_model: str, max_turns_per_pass: int = 20) -> None:
        self._store = store
        self._client = OllamaClient(base_url=ollama_url,
                                    default_model=ollama_model,
                                    request_timeout=90.0)
        self._max_turns = max_turns_per_pass
        # Highest turn id we've already extracted from. Persisted in
        # the facts table via the source_turn_id column — on boot we
        # scan for it.
        self._last_extracted_turn_id: int = 0
        self._init_high_water()

    def _init_high_water(self) -> None:
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(source_turn_id) AS m FROM facts"
                ).fetchone()
                if row and row["m"] is not None:
                    self._last_extracted_turn_id = int(row["m"])
        except Exception:  # noqa: BLE001
            logger.exception("could not read fact high-water mark; starting at 0")

    @property
    def last_extracted_turn_id(self) -> int:
        return self._last_extracted_turn_id

    def _format_turns(self, turns: list[StoredTurn]) -> str:
        lines: list[str] = []
        for t in turns:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.ts))
            lines.append(f"[{ts}] {t.role.upper()}: {t.content}")
        return "\n".join(lines)

    async def extract_pass(self, *, min_new_turns: int = 4) -> dict[str, Any]:
        """Run one extraction pass over new turns.

        Returns a dict suitable for publishing as ``facts_extracted``.
        """
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,role,content,conv_id FROM turns "
                "WHERE id > ? ORDER BY id ASC LIMIT ?",
                (self._last_extracted_turn_id, self._max_turns),
            ).fetchall()
        if len(rows) < min_new_turns:
            return {"skipped": "not_enough_new_turns", "new_turn_count": len(rows)}
        new_turns = [StoredTurn(id=int(r["id"]), ts=float(r["ts"]),
                                role=str(r["role"]), content=str(r["content"]),
                                conv_id=str(r["conv_id"])) for r in rows]
        prompt = (
            "Conversation turns to analyse:\n\n"
            + self._format_turns(new_turns)
            + "\n\nReturn the JSON array of durable facts (or [] if none)."
        )
        try:
            raw = await self._client.chat(
                system_prompt=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ollama call failed in extractor: %s", exc)
            return {"error": str(exc), "new_turn_count": len(new_turns)}
        parsed = _parse_facts(raw)
        # Attribute each fact to the *most recent* turn in the window.
        attribution_id = new_turns[-1].id
        inserted = 0
        skipped_dupes = 0
        for f in parsed:
            fid = self._store.insert_fact(
                subject=f["subject"], predicate=f["predicate"],
                object_=f["object"], source_turn_id=attribution_id,
                confidence=0.85,
            )
            if fid is None:
                skipped_dupes += 1
            else:
                inserted += 1
        # Advance the high-water mark regardless of insertion outcome
        # (a window that produced no facts shouldn't be re-processed).
        self._last_extracted_turn_id = attribution_id
        return {
            "window_turn_count": len(new_turns),
            "facts_parsed": len(parsed),
            "facts_inserted": inserted,
            "facts_duplicate": skipped_dupes,
            "high_water_turn_id": attribution_id,
        }
