"""
tool_parser.py — Parse tool calls from LLM output.

ULTRON uses a simple JSON block convention for tool calls:
The LLM outputs a fenced code block with language "tool":

    ```tool
    {"name": "shell", "args": {"cmd": "ls -la"}}
    ```

This parser extracts all such blocks from a response string.
Other modules (E — Tool Registry, future) consume the result.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("ultron.llm.tool_parser")


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    raw: str   # original matched block, for logging


# Match ```tool ... ``` blocks (non-greedy, multiline)
_TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL)


def parse_tool_calls(text: str) -> list[ToolCall]:
    """
    Extract all tool call blocks from `text`.
    Returns an empty list if none found.
    Silently skips malformed blocks (logs warning).
    """
    calls: list[ToolCall] = []
    for match in _TOOL_BLOCK_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            obj = json.loads(raw)
            name = obj.get("name", "")
            args = obj.get("args", {})
            if not name:
                logger.warning("tool call missing 'name': %s", raw[:80])
                continue
            calls.append(ToolCall(name=name, args=args, raw=raw))
        except json.JSONDecodeError as exc:
            logger.warning("malformed tool call block: %s — %s", raw[:80], exc)
    return calls


def strip_tool_calls(text: str) -> str:
    """Remove all ```tool ... ``` blocks from text. Returns clean response."""
    return _TOOL_BLOCK_RE.sub("", text).strip()
