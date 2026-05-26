"""run_macro tool -- execute a named multi-step routine.

Macros are defined in config.toml under [macros]. Each macro is a list
of tool calls executed sequentially. Example config:

    [macros]
    morning_routine = [
        {name = "open_app", args = {name = "chrome"}},
        {name = "brightness", args = {action = "set", level = 80}},
        {name = "media_control", args = {what = "play_pause"}},
    ]
    study_mode = [
        {name = "close_app", args = {name = "discord"}},
        {name = "close_app", args = {name = "spotify"}},
        {name = "open_app", args = {name = "vscode"}},
        {name = "brightness", args = {action = "set", level = 60}},
    ]

Built-in macros are provided as defaults for common workflows.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from ..config import ToolsConfig
from ..registry import Tool

logger = logging.getLogger("ultron.tools.run_macro")


BUILTIN_MACROS: dict[str, list[dict[str, Any]]] = {
    "morning_routine": [
        {"name": "open_app", "args": {"name": "chrome"}},
        {"name": "brightness", "args": {"action": "set", "level": 80}},
    ],
    "study_mode": [
        {"name": "close_app", "args": {"name": "discord"}},
        {"name": "close_app", "args": {"name": "spotify"}},
        {"name": "open_app", "args": {"name": "vscode"}},
        {"name": "brightness", "args": {"action": "set", "level": 60}},
    ],
    "gaming_mode": [
        {"name": "close_app", "args": {"name": "vscode"}},
        {"name": "close_app", "args": {"name": "teams"}},
        {"name": "brightness", "args": {"action": "set", "level": 100}},
    ],
    "work_mode": [
        {"name": "open_app", "args": {"name": "chrome"}},
        {"name": "open_app", "args": {"name": "vscode"}},
        {"name": "open_app", "args": {"name": "terminal"}},
        {"name": "brightness", "args": {"action": "set", "level": 70}},
    ],
    "night_mode": [
        {"name": "brightness", "args": {"action": "set", "level": 20}},
    ],
    "presentation_mode": [
        {"name": "close_app", "args": {"name": "discord"}},
        {"name": "close_app", "args": {"name": "spotify"}},
        {"name": "close_app", "args": {"name": "teams"}},
        {"name": "brightness", "args": {"action": "set", "level": 100}},
    ],
}


def _load_user_macros() -> dict[str, list[dict[str, Any]]]:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return {}
    section = raw.get("macros", {})
    if not isinstance(section, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for name, steps in section.items():
        if isinstance(steps, list):
            parsed = []
            for step in steps:
                if isinstance(step, dict) and "name" in step:
                    parsed.append({
                        "name": str(step["name"]),
                        "args": step.get("args", {}),
                    })
            if parsed:
                result[name.lower()] = parsed
    return result


def build(config: ToolsConfig) -> Tool:
    user_macros = _load_user_macros()
    all_macros = dict(BUILTIN_MACROS)
    all_macros.update(user_macros)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip().lower().replace(" ", "_")
        if not name:
            return {"ok": False, "reason": "name is required"}

        if name not in all_macros:
            available = ", ".join(sorted(all_macros.keys()))
            return {
                "ok": False,
                "reason": f"macro '{name}' not found. Available: {available}",
            }

        steps = all_macros[name]
        from ultron_tools import get_registry
        registry = get_registry()

        results: list[dict[str, Any]] = []
        for i, step in enumerate(steps):
            tool_name = step["name"]
            tool_args = step.get("args", {})
            tool = registry.get(tool_name)
            if tool is None:
                results.append({
                    "step": i + 1,
                    "tool": tool_name,
                    "ok": False,
                    "reason": f"tool '{tool_name}' not found",
                })
                continue
            try:
                result = await tool.handler(tool_args)
                results.append({
                    "step": i + 1,
                    "tool": tool_name,
                    "ok": result.get("ok", True),
                })
            except Exception as exc:
                results.append({
                    "step": i + 1,
                    "tool": tool_name,
                    "ok": False,
                    "reason": str(exc),
                })

        ok_count = sum(1 for r in results if r.get("ok"))
        return {
            "ok": ok_count > 0,
            "macro": name,
            "steps_total": len(steps),
            "steps_ok": ok_count,
            "results": results,
        }

    macro_names = ", ".join(sorted(all_macros.keys()))
    return Tool(
        name="run_macro",
        description=(
            f"Run a named multi-step routine. Available macros: {macro_names}. "
            "Each macro executes a sequence of tool calls. "
            "User can define custom macros in config.toml [macros] section."
        ),
        category="system",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 64},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )
