"""Config for Module F (Agent Mesh)."""
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


@dataclass
class AgentMeshConfig:
    ws_url: str
    ws_token: str

    # Tool-loop guardrails — protects against runaway agents.
    max_tool_rounds: int = 6
    max_total_steps: int = 16

    # Per-agent timeout (seconds)
    task_timeout_seconds: int = 180

    # Append every step to JSONL for forensic review (Z also gets WS events)
    audit_log_path: Path = field(
        default_factory=lambda: _ultron_data_dir() / "data" / "agent_audit.jsonl"
    )

    # If the LLM is not configured (no get_service available), agent calls
    # short-circuit with an explanatory error rather than hanging.
    require_llm: bool = True


def load_agent_mesh_config(config_path: Path | None = None) -> AgentMeshConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    a = raw.get("agents", {}) if isinstance(raw.get("agents"), dict) else {}

    return AgentMeshConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        max_tool_rounds=int(a.get("max_tool_rounds", 6)),
        max_total_steps=int(a.get("max_total_steps", 16)),
        task_timeout_seconds=int(a.get("task_timeout_seconds", 180)),
        audit_log_path=Path(
            str(a.get("audit_log_path", data_dir / "data" / "agent_audit.jsonl"))
        ),
        require_llm=bool(a.get("require_llm", True)),
    )
