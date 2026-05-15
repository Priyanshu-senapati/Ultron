"""Config loader for Module E (Tool Registry).

Reads ``[tools]`` from %APPDATA%/ULTRON/config.toml. All keys have safe
defaults so the section is optional.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


# Tools that should NEVER auto-execute — they need a fresh confirm token
# every time. Built-in defaults; users can extend via config.
DEFAULT_CONFIRM_REQUIRED_TOOLS: tuple[str, ...] = (
    "shell",
    "write_file",
    "delete_file",
    "git_commit",
    "git_push",
    "open_url",
    "kill_process",
)


@dataclass
class ToolsConfig:
    # WS bridge
    ws_url: str
    ws_token: str

    # Always-confirm list (additive on top of per-tool defaults)
    confirm_required_tools: tuple[str, ...] = DEFAULT_CONFIRM_REQUIRED_TOOLS

    # Confirm token lifetime — user has this long to approve a pending call
    confirm_timeout_seconds: int = 60

    # Audit JSONL — every tool call is appended here, in addition to the
    # WS audit event Z (quantum log) records.
    audit_log_path: Path = field(
        default_factory=lambda: _ultron_data_dir() / "data" / "tool_audit.jsonl"
    )

    # Sandbox root for read/write file tools. Anything outside this tree is
    # rejected unless the call also carries an explicit confirm token.
    sandbox_root: Path = field(default_factory=lambda: Path("C:/dev"))

    # Shell tool — hard cap on output size and runtime
    shell_max_output_bytes: int = 64 * 1024
    shell_timeout_seconds: int = 30

    # Web search — cap result count to keep prompts small
    web_search_max_results: int = 5


def load_tools_config(config_path: Path | None = None) -> ToolsConfig:
    """Read the [tools] section. Missing values use defaults."""
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    t = raw.get("tools", {}) if isinstance(raw.get("tools"), dict) else {}

    confirm_list = t.get("confirm_required_tools") or DEFAULT_CONFIRM_REQUIRED_TOOLS
    if isinstance(confirm_list, list):
        confirm_list = tuple(str(x) for x in confirm_list)

    sandbox_raw = str(t.get("sandbox_root", "C:/dev")).strip() or "C:/dev"

    return ToolsConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        confirm_required_tools=confirm_list,
        confirm_timeout_seconds=int(t.get("confirm_timeout_seconds", 60)),
        audit_log_path=Path(
            str(t.get("audit_log_path", data_dir / "data" / "tool_audit.jsonl"))
        ),
        sandbox_root=Path(sandbox_raw),
        shell_max_output_bytes=int(t.get("shell_max_output_bytes", 64 * 1024)),
        shell_timeout_seconds=int(t.get("shell_timeout_seconds", 30)),
        web_search_max_results=int(t.get("web_search_max_results", 5)),
    )
