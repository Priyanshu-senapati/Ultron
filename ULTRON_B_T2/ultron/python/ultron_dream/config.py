from __future__ import annotations
from dataclasses import dataclass


@dataclass
class DreamConfig:
    ws_url: str = "ws://127.0.0.1:9420/ws"
    ws_token: str = ""
    idle_threshold_minutes: float = 30.0
    min_data_points: int = 5
    max_insights: int = 5
    ollama_model: str = "llama3.1:8b"
    enabled: bool = True
