from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ProactiveConfig:
    ws_url: str = "ws://127.0.0.1:9420/ws"
    ws_token: str = ""
    tick_secs: float = 300.0
    boot_delay_secs: float = 60.0
    cooldown_secs: float = 1800.0
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7
    enabled: bool = True
