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


# Properly fenced ```tool … ``` blocks (non-greedy, multiline).
_TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL)
# Fallback: small / local models sometimes drop the closing fence.
# Match ```tool followed by content to end-of-string. Used only if the
# strict regex finds nothing, otherwise it would also consume the
# properly-closed case and lose the trailing content.
_TOOL_BLOCK_UNCLOSED_RE = re.compile(r"```tool\s*\n(.*?)(?:```|$)", re.DOTALL)


def parse_tool_calls(text: str) -> list[ToolCall]:
    """
    Extract all tool call blocks from `text`.
    Returns an empty list if none found.
    Silently skips malformed blocks (logs warning).

    Handles the unclosed-fence case: if the model emits ```tool but
    forgets the closing ```, we still parse the JSON. This happens with
    small local models often enough that it's worth the leniency.
    """
    calls: list[ToolCall] = []
    matches = list(_TOOL_BLOCK_RE.finditer(text))
    if not matches and "```tool" in text:
        matches = list(_TOOL_BLOCK_UNCLOSED_RE.finditer(text))
    for match in matches:
        raw = match.group(1).strip()
        # If we used the unclosed-fence path, the JSON often has trailing
        # cruft. Trim to the closing brace of the outermost object.
        if raw and not raw.endswith("}"):
            last_brace = raw.rfind("}")
            if last_brace > 0:
                raw = raw[: last_brace + 1]
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
    """Remove all ```tool ... ``` blocks from text. Returns clean response.

    The unclosed-fence variant is stripped too, so if the model emits a
    tool block without closing it, the TTS never reads `"name", "open_app"`
    out loud.
    """
    cleaned = _TOOL_BLOCK_RE.sub("", text)
    cleaned = _TOOL_BLOCK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()
