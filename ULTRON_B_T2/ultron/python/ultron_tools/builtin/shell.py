"""shell tool — run a shell command with strict timeout and output cap.

Confirm-required. The command is passed verbatim to ``cmd.exe /C`` on
Windows. Stdout and stderr are merged and truncated to
``ToolsConfig.shell_max_output_bytes``.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    is_windows = os.name == "nt"

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        cmd = str(args.get("cmd", "")).strip()
        if not cmd:
            raise ValueError("cmd is required")
        cwd = str(args.get("cwd", str(config.sandbox_root))).strip() or str(config.sandbox_root)

        if is_windows:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        else:
            # POSIX: avoid the shell — split, run argv directly.
            argv = shlex.split(cmd)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

        try:
            out_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=config.shell_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"shell command exceeded {config.shell_timeout_seconds}s timeout"
            )

        truncated = False
        if len(out_bytes) > config.shell_max_output_bytes:
            out_bytes = out_bytes[: config.shell_max_output_bytes]
            truncated = True
        text = out_bytes.decode("utf-8", errors="replace")
        return {
            "cmd": cmd,
            "cwd": cwd,
            "exit_code": proc.returncode,
            "output": text,
            "truncated": truncated,
        }

    return Tool(
        name="shell",
        description="Run a shell command. Windows: cmd.exe /C. POSIX: argv exec. Bounded by timeout + output cap.",
        category="system",
        confirm_required=True,
        confirm_reason="shell commands can mutate the system",
        args_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "minLength": 1, "maxLength": 4096},
                "cwd": {"type": "string", "minLength": 0, "maxLength": 1024},
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
        handler=handler,
    )
