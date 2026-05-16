"""Config for the Claude Code feed."""
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


@dataclass
class ClaudeFeedConfig:
    ws_url: str
    ws_token: str

    # Where the daily feed files live. Default sits inside C:/dev so the
    # repo Claude Code is opened on sees the feed immediately.
    feed_dir: Path = field(default_factory=lambda: Path("C:/dev/.ultron-feed"))

    # If a single feed file grows past this, roll over (write to a
    # numbered suffix). Avoids unbounded growth.
    max_file_bytes: int = 1_000_000

    # If True, also log non-failure tool calls as "✓" entries. Useful
    # when you want a transcript of every action ULTRON took. False
    # keeps the feed focused on issues only.
    log_successes: bool = False


def load_claude_feed_config(config_path: Path | None = None) -> ClaudeFeedConfig:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    config_path = config_path or (Path(appdata) / "ULTRON" / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    f = raw.get("claude_feed", {}) if isinstance(raw.get("claude_feed"), dict) else {}
    return ClaudeFeedConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        feed_dir=Path(str(f.get("feed_dir", "C:/dev/.ultron-feed"))),
        max_file_bytes=int(f.get("max_file_bytes", 1_000_000)),
        log_successes=bool(f.get("log_successes", False)),
    )
