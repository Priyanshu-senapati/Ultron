"""conversation.py — Conversation history with disk persistence.

One history per ULTRON instance (single-user twin — no per-user split).
Keeps the last N turns (user+assistant pairs) in a deque AND mirrors
them to a JSON file so restarts don't wipe context.

The user repeatedly complained about ULTRON "forgetting our previous
conversations" — the old in-memory ring buffer was the cause. Now every
add_user / add_assistant writes through to disk atomically.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ultron.llm.conversation")


@dataclass
class Turn:
    role: str    # "user" | "assistant"
    content: str


class ConversationHistory:
    def __init__(self, max_turns: int = 20, persist_path: Optional[Path] = None) -> None:
        # Store individual messages; max_turns pairs = max_turns*2 messages.
        self._max = max_turns * 2
        self._messages: deque[Turn] = deque(maxlen=self._max)
        self._persist_path = persist_path
        if persist_path is not None:
            self._load()

    # ── Mutators ─────────────────────────────────────────────────────────

    def add_user(self, text: str) -> None:
        self._messages.append(Turn(role="user", content=text))
        self._save()

    def add_assistant(self, text: str) -> None:
        self._messages.append(Turn(role="assistant", content=text))
        self._save()

    def clear(self) -> None:
        self._messages.clear()
        self._save()

    # ── Serialisers ──────────────────────────────────────────────────────

    def to_ollama_messages(self) -> list[dict]:
        return [{"role": t.role, "content": t.content} for t in self._messages]

    def to_claude_messages(self) -> list[dict]:
        return [{"role": t.role, "content": t.content} for t in self._messages]

    def __len__(self) -> int:
        return len(self._messages)

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load conversation history: %s", exc)
            return
        if not isinstance(data, list):
            return
        for entry in data[-self._max:]:
            if isinstance(entry, dict) and "role" in entry and "content" in entry:
                self._messages.append(
                    Turn(role=str(entry["role"]), content=str(entry["content"]))
                )
        logger.info("loaded %d history messages from %s", len(self._messages), self._persist_path)

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [asdict(t) for t in self._messages]
            # Atomic write: tmp + rename. Survives crashes mid-save.
            fd, tmp = tempfile.mkstemp(
                prefix=".conv-", suffix=".json", dir=str(self._persist_path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self._persist_path)
            except Exception:
                # Best-effort cleanup of the temp file on failure.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("could not persist conversation history: %s", exc)
