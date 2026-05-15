"""delete_file tool — remove a single file from the sandbox.

Confirm-required. Refuses directories outright (use the future
``delete_dir`` tool, which will need its own confirm).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


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
        if target.is_dir():
            raise IsADirectoryError(f"refusing to delete directory via delete_file: {target}")
        target.unlink()
        return {"path": str(target), "deleted": True}

    return Tool(
        name="delete_file",
        description="Delete a single file from the sandbox. Refuses directories.",
        category="filesystem",
        confirm_required=True,
        confirm_reason="deletion is irreversible",
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
