"""claude_session.py — Tail the active Claude Code conversation log.

The user is having this very conversation in Claude Code, which writes
JSONL logs to:

    C:/Users/<user>/.claude/projects/C--dev/<session>.jsonl

This bridge picks the most-recently-modified jsonl in that directory and
tails new lines. Each line is one JSON-serialised conversation entry
(user message, assistant message, tool call, tool result, etc.).

We extract a lightweight summary — kind + content snippet — and publish
`claude_session_update` events so ULTRON can answer "what is claude
working on right now" or "what did claude just say".

Failure mode: if the .claude dir isn't where we expect, or no jsonl
exists, the bridge idles cleanly without crashing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .base import Bridge, BridgePublishFn

logger = logging.getLogger("ultron.bridges.claude_session")


@dataclass
class ClaudeSessionConfig:
    enabled: bool = True
    # Walks up from %USERPROFILE%; "C--dev" matches Claude Code's path-to-slug
    # convention for sessions started in C:\dev.
    sessions_dir: str = ""  # resolved at startup if empty
    poll_secs: float = 4.0
    # Max chars to include in each event's `content` snippet.
    snippet_chars: int = 400


def _default_sessions_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    return home / ".claude" / "projects" / "C--dev"


class ClaudeSessionBridge(Bridge):
    name = "claude_session"

    def __init__(self, publish: BridgePublishFn | None, cfg: ClaudeSessionConfig) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self.dir = Path(cfg.sessions_dir) if cfg.sessions_dir else _default_sessions_dir()
        self._current_file: Optional[Path] = None
        self._offset: int = 0
        # De-dupe key per emitted entry so re-tailing the same line doesn't
        # re-publish (line offsets only protect within a single file).
        self._last_emitted_key: str = ""

    async def run(self) -> None:
        if not self.dir.exists():
            self.log.warning("Claude sessions dir not found at %s; idling", self.dir)
            await self._stop_event.wait()
            return

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("claude_session tick failed: %s", exc)
            if not await self.sleep(self.cfg.poll_secs):
                return

    async def _tick(self) -> None:
        # Pick the most recently modified .jsonl in the sessions dir.
        try:
            files = sorted(self.dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        if not files:
            return
        active = files[0]

        # If we switched sessions, reset offset and announce.
        if active != self._current_file:
            self._current_file = active
            self._offset = 0
            self.log.info("tailing session %s", active.name)

        # Read any new bytes appended since last tick.
        try:
            size = active.stat().st_size
        except OSError:
            return
        if size <= self._offset:
            return

        try:
            with active.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
                self._offset = f.tell()
        except OSError as exc:
            self.log.debug("read failed: %s", exc)
            return

        # Each new line is one event. Tolerant of partial trailing lines
        # (we'll re-read on the next tick).
        text = chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # If the chunk didn't end with newline, the last line is partial —
        # rewind the offset by that many bytes so we re-read it next tick.
        if not text.endswith("\n") and lines:
            partial = lines.pop()
            self._offset -= len(partial.encode("utf-8"))

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary = self._summarise(msg)
            if summary is None:
                continue
            await self.publish("claude_session_update", summary)

    def _summarise(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Extract a small, prompt-friendly summary from a Claude Code log entry.

        Claude Code's JSONL has many event types. We surface:
          - user messages
          - assistant text messages
          - tool calls (name only)
          - tool results (status only)
        """
        kind = msg.get("type") or msg.get("kind") or ""
        # Several shapes exist; try a few.
        message = msg.get("message") or msg.get("data") or {}
        role = message.get("role") if isinstance(message, dict) else None

        snippet = ""
        kind_out = kind or "log"

        # Assistant message
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, list):
                # blocks: text / tool_use / etc.
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        snippet = str(block.get("text", ""))[:self.cfg.snippet_chars]
                        kind_out = "assistant_text"
                        break
                    if block.get("type") == "tool_use":
                        kind_out = "assistant_tool_use"
                        snippet = f"tool: {block.get('name','?')}"
                        break
            elif isinstance(content, str):
                snippet = content[:self.cfg.snippet_chars]
                kind_out = "assistant_text"

        elif role == "user":
            content = message.get("content")
            if isinstance(content, str):
                snippet = content[:self.cfg.snippet_chars]
                kind_out = "user_text"
            elif isinstance(content, list):
                # Often a tool_result block
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        kind_out = "tool_result"
                        out = block.get("content", "")
                        if isinstance(out, list):
                            # nested text blocks
                            txt_bits = [b.get("text","") for b in out if isinstance(b, dict)]
                            snippet = " ".join(txt_bits)[:self.cfg.snippet_chars]
                        else:
                            snippet = str(out)[:self.cfg.snippet_chars]
                        break
                    if isinstance(block, dict) and block.get("type") == "text":
                        kind_out = "user_text"
                        snippet = str(block.get("text",""))[:self.cfg.snippet_chars]
                        break

        if not snippet:
            return None

        # Dedupe identical adjacent entries.
        key = f"{kind_out}|{snippet[:64]}"
        if key == self._last_emitted_key:
            return None
        self._last_emitted_key = key

        return {
            "kind": kind_out,
            "snippet": snippet,
            "ts_unix_ms": int(time.time() * 1000),
        }


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False
