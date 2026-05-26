from __future__ import annotations
from dataclasses import dataclass


@dataclass
class SysHealthConfig:
    ws_url: str = "ws://127.0.0.1:9420/ws"
    ws_token: str = ""
    poll_secs: float = 10.0
    gpu_enabled: bool = True
    cpu_temp_enabled: bool = True
    alert_cpu_temp: float = 90.0
    alert_gpu_temp: float = 85.0
    alert_ram_percent: float = 90.0
    alert_cpu_percent: float = 95.0
