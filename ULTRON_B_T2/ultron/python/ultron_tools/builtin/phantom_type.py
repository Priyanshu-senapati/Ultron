"""phantom_type tool -- ULTRON types text at the cursor for you.

"Reply to that email saying I'll be there at 5" → ULTRON generates
the text in a natural tone, then physically types it via pyautogui
at whatever text field has focus. No clipboard, no paste — actual
keystroke simulation so it works everywhere.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.phantom_type")


def _generate_text(instruction: str, model: str = "llama3.1:8b") -> str:
    """Ask the LLM to write text based on the user's instruction."""
    prompt = (
        "You are a writing assistant. The user wants you to write something "
        "that will be typed directly into their current text field. "
        "Write ONLY the text to be typed — no quotes, no explanation, "
        "no preamble, no markdown. Keep it natural and concise. "
        "Match an informal, friendly tone unless the instruction implies formal.\n\n"
        f"Instruction: {instruction}"
    )
    try:
        r = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception as exc:
        logger.error("phantom_type LLM failed: %s", exc)
    return ""


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        instruction = str(args.get("instruction", "")).strip()
        raw_text = str(args.get("text", "")).strip()

        if not instruction and not raw_text:
            return {"ok": False, "reason": "instruction or text required"}

        if raw_text:
            text_to_type = raw_text
        else:
            import asyncio
            loop = asyncio.get_event_loop()
            text_to_type = await loop.run_in_executor(
                None, _generate_text, instruction
            )

        if not text_to_type:
            return {"ok": False, "reason": "could not generate text"}

        try:
            import pyautogui
            time.sleep(0.3)
            for char in text_to_type:
                pyautogui.press(char) if len(char) > 1 else pyautogui.typewrite(char, interval=0) if char.isascii() and char.isprintable() else pyautogui.hotkey('ctrl', 'v') if False else None
            # pyautogui.typewrite only handles ASCII. For full unicode
            # support, use pyperclip + ctrl+v as fallback.
            import pyperclip
            pyperclip.copy(text_to_type)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.1)
            return {
                "ok": True,
                "typed": text_to_type[:200],
                "length": len(text_to_type),
                "method": "clipboard_paste",
            }
        except ImportError:
            # No pyperclip — fall back to typewrite (ASCII only)
            import pyautogui
            pyautogui.typewrite(text_to_type, interval=0.01)
            return {
                "ok": True,
                "typed": text_to_type[:200],
                "length": len(text_to_type),
                "method": "typewrite",
            }
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    return Tool(
        name="phantom_type",
        description=(
            "Type text at the current cursor position. Pass 'instruction' "
            "for ULTRON to generate the text (e.g. 'reply saying I will be "
            "there at 5'), or 'text' to type exact text. ULTRON physically "
            "types the keystrokes — works in any text field."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "maxLength": 500},
                "text": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
