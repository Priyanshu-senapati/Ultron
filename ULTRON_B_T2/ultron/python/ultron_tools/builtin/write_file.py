"""write_file tool — atomic write of UTF-8 text to a sandboxed path.

Confirm-required: this mutates disk state. Creates parent dirs as needed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    sandbox = config.sandbox_root.resolve()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        rel = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not rel:
            raise ValueError("path is required")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        target = (sandbox / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        try:
            target.relative_to(sandbox)
        except ValueError as exc:
            raise PermissionError(f"path {target} is outside sandbox {sandbox}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in same dir → fsync → rename
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tool-write-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # filesystem may not support fsync
            os.replace(tmp, target)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {
            "path": str(target),
            "bytes_written": len(content.encode("utf-8")),
        }

    return Tool(
        name="write_file",
        description="Atomically write UTF-8 text to a sandbox path. Overwrites existing files.",
        category="filesystem",
        confirm_required=True,
        confirm_reason="writes to disk; cannot be auto-approved",
        args_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "content": {"type": "string", "maxLength": 1024 * 1024},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
    )
