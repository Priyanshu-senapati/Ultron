"""screenshot tool — capture the primary display and return a base64 PNG.

Thin wrapper over ``ultron_llm.vision.capture_screen_b64``. Used by the
LLM when it wants to ground a question on what's currently on-screen.
"""
from __future__ import annotations

from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # Local import — PIL.ImageGrab is heavy to load.
        from ultron_llm.vision import capture_screen_b64  # type: ignore[import]

        max_dim = int(args.get("max_dim", 1280))
        max_dim = max(320, min(max_dim, 3840))
        b64 = capture_screen_b64(max_dim=max_dim)
        return {
            "format": "png_base64",
            "max_dim": max_dim,
            "size_bytes": len(b64),
            "data": b64,
        }

    return Tool(
        name="screenshot",
        description="Capture the primary display and return PNG bytes as base64.",
        category="perception",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "max_dim": {"type": "integer", "minimum": 320, "maximum": 3840},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
