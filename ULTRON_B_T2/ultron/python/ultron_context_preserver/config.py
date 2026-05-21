"""Config for the Context Preserver."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
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
class ContextPreserverConfig:
    ws_url: str
    ws_token: str
    user_name: str

    # ── Output paths ──────────────────────────────────────────────────
    # The human-readable packet a returning session can read first.
    packet_md_path: Path = Path()
    # The machine-readable companion (same data, JSON).
    packet_json_path: Path = Path()
    # Rolling archive of past packets so we don't lose history when
    # the heartbeat overwrites the current file.
    archive_dir: Path = Path()
    archive_keep: int = 20

    # ── Cadence ───────────────────────────────────────────────────────
    # Heartbeat write while running so an ungraceful crash still leaves
    # a recent packet on disk.
    heartbeat_interval_secs: float = 300.0
    # Wait this long after startup before the first heartbeat so the
    # service has had a chance to receive a few events.
    boot_delay_secs: float = 30.0

    # ── Content sizing ────────────────────────────────────────────────
    # Cap the LLM-quote chars in the packet (a 4 KB response is fine in
    # memory but pollutes the .md). Whole sentences only.
    max_llm_quote_chars: int = 600
    # Cap commits + claude-session snippets per packet.
    max_commits: int = 10
    max_claude_snippet_chars: int = 800


def load_context_preserver_config(config_path: Path | None = None) -> ContextPreserverConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    user_name = str((raw.get("general") or {}).get("user_name", "the user"))
    c = raw.get("context_preserver", {}) if isinstance(raw.get("context_preserver"), dict) else {}
    return ContextPreserverConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        user_name=user_name,
        packet_md_path=Path(str(c.get("packet_md_path",
                                      data_dir / "context_packet.md"))),
        packet_json_path=Path(str(c.get("packet_json_path",
                                        data_dir / "context_packet.json"))),
        archive_dir=Path(str(c.get("archive_dir",
                                   data_dir / "context_archive"))),
        archive_keep=int(c.get("archive_keep", 20)),
        heartbeat_interval_secs=float(c.get("heartbeat_interval_secs", 300.0)),
        boot_delay_secs=float(c.get("boot_delay_secs", 30.0)),
        max_llm_quote_chars=int(c.get("max_llm_quote_chars", 600)),
        max_commits=int(c.get("max_commits", 10)),
        max_claude_snippet_chars=int(c.get("max_claude_snippet_chars", 800)),
    )
