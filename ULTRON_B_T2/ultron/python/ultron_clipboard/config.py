from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ClipboardConfig:
    ws_url: str = "ws://127.0.0.1:9420/ws"
    ws_token: str = ""
    poll_secs: float = 2.0
    max_content_chars: int = 500
