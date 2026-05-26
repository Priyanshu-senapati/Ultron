"""screen_control tool -- read screen content and interact with UI.

Capabilities:
  read_screen   -- screenshot + describe what's on screen via LLaVA
  click_at      -- click at x,y coordinates or find text and click it
  type_text     -- type text at the current cursor position
  scroll        -- scroll up/down by N clicks
  move_mouse    -- move the mouse to x,y

Uses pyautogui for input simulation and the existing screenshot +
LLaVA pipeline for understanding what's on screen.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from typing import Any, Optional

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.screen_control")


def _screenshot_and_describe() -> Optional[str]:
    """Take a screenshot and describe it via Ollama LLaVA."""
    try:
        import pyautogui
        import tempfile
        import os

        screenshot = pyautogui.screenshot()
        tmp = os.path.join(tempfile.gettempdir(), "ultron_screen_read.png")
        screenshot.save(tmp)

        result = subprocess.run(
            ["ollama", "run", "llava", "--image", tmp,
             "Describe everything visible on this screen in detail. "
             "Include: window titles, text content, buttons, menus, "
             "notifications, and any important visual elements. "
             "Be specific about positions (top-left, center, bottom, etc)."],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except Exception as exc:
        logger.error("screenshot describe failed: %s", exc)
        return None


def _find_text_on_screen(text: str) -> Optional[tuple[int, int]]:
    """Try to find text on screen using OCR. Returns center x,y or None."""
    try:
        import pyautogui
        location = pyautogui.locateOnScreen(text)
        if location:
            center = pyautogui.center(location)
            return (center.x, center.y)
    except Exception:
        pass
    return None


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "")).strip().lower()

        if action == "read_screen":
            loop = asyncio.get_event_loop()
            description = await loop.run_in_executor(
                None, _screenshot_and_describe
            )
            if description:
                return {
                    "ok": True,
                    "action": "read_screen",
                    "description": description[:2000],
                }
            return {"ok": False, "reason": "could not read screen"}

        elif action == "click_at":
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                return {"ok": False, "reason": "x and y coordinates required"}
            try:
                import pyautogui
                pyautogui.click(int(x), int(y))
                return {"ok": True, "action": "click_at",
                        "x": int(x), "y": int(y)}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "double_click":
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                return {"ok": False, "reason": "x and y coordinates required"}
            try:
                import pyautogui
                pyautogui.doubleClick(int(x), int(y))
                return {"ok": True, "action": "double_click",
                        "x": int(x), "y": int(y)}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "right_click":
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                return {"ok": False, "reason": "x and y coordinates required"}
            try:
                import pyautogui
                pyautogui.rightClick(int(x), int(y))
                return {"ok": True, "action": "right_click",
                        "x": int(x), "y": int(y)}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "type_text":
            text = str(args.get("text", ""))
            if not text:
                return {"ok": False, "reason": "text is required"}
            try:
                import pyautogui
                pyautogui.typewrite(text, interval=0.02)
                return {"ok": True, "action": "type_text",
                        "length": len(text)}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "hotkey":
            keys = args.get("keys", [])
            if not keys:
                return {"ok": False, "reason": "keys list required"}
            try:
                import pyautogui
                pyautogui.hotkey(*keys)
                return {"ok": True, "action": "hotkey", "keys": keys}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "scroll":
            clicks = int(args.get("clicks", 3))
            direction = str(args.get("direction", "down")).lower()
            try:
                import ctypes
                MOUSEEVENTF_WHEEL = 0x0800
                WHEEL_DELTA = 120
                amount = WHEEL_DELTA * clicks
                if direction == "down":
                    amount = -amount
                ctypes.windll.user32.mouse_event(
                    MOUSEEVENTF_WHEEL, 0, 0, amount, 0
                )
                return {"ok": True, "action": "scroll",
                        "direction": direction, "clicks": clicks}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "move_mouse":
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                return {"ok": False, "reason": "x and y required"}
            try:
                import pyautogui
                pyautogui.moveTo(int(x), int(y), duration=0.3)
                return {"ok": True, "action": "move_mouse",
                        "x": int(x), "y": int(y)}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        elif action == "mouse_position":
            try:
                import pyautogui
                pos = pyautogui.position()
                return {"ok": True, "action": "mouse_position",
                        "x": pos.x, "y": pos.y}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}

        return {
            "ok": False,
            "reason": f"unknown action '{action}'. "
                      "Use: read_screen, click_at, double_click, right_click, "
                      "type_text, hotkey, scroll, move_mouse, mouse_position",
        }

    return Tool(
        name="screen_control",
        description=(
            "Read and interact with the screen. "
            "action=read_screen to describe what's visible. "
            "action=click_at with x,y to click a position. "
            "action=type_text with text to type. "
            "action=hotkey with keys=['ctrl','c'] to press key combos. "
            "action=scroll with direction and clicks. "
            "action=mouse_position to get current mouse location."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read_screen", "click_at", "double_click",
                             "right_click", "type_text", "hotkey",
                             "scroll", "move_mouse", "mouse_position"],
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "text": {"type": "string", "maxLength": 1000},
                "keys": {"type": "array", "items": {"type": "string"}},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 20},
                "direction": {"type": "string", "enum": ["up", "down"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )
