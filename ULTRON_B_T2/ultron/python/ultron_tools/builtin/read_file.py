"""read_file tool — read a UTF-8 text file from disk.

Path must live under ``ToolsConfig.sandbox_root`` (no traversal escapes).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool

_MAX_BYTES = 256 * 1024  # 256 KiB cap — bigger files become a different tool


def build(config: ToolsConfig) -> Tool:
    sandbox = config.sandbox_root.resolve()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        rel = str(args.get("path", "")).strip()
        if not rel:
            raise ValueError("path is required")
        target = (sandbox / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        try:
            target.relative_to(sandbox)
        except ValueError as exc:
            raise PermissionError(f"path {target} is outside sandbox {sandbox}") from exc
        if not target.exists():
            raise FileNotFoundError(f"no such file: {target}")
        if not target.is_file():
            raise IsADirectoryError(f"not a file: {target}")
        data = target.read_bytes()
        truncated = False
        if len(data) > _MAX_BYTES:
            data = data[:_MAX_BYTES]
            truncated = True
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        return {
            "path": str(target),
            "bytes": len(data),
            "truncated": truncated,
            "content": text,
        }

    return Tool(
        name="read_file",
        description="Read the contents of a UTF-8 text file from the sandbox.",
        category="filesystem",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
    )
