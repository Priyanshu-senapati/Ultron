"""Load ULTRON config for Module C."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# tomllib is stdlib from 3.11; fall back to tomli on older interpreters.
if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


@dataclass
class LLMConfig:
    # Bridge
    ws_url: str
    token: str
    # Identity
    user_name: str = ""
    # Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"        # fast local default
    ollama_model_large: str = "llama3.1:8b"  # for complex reasoning
    # Claude API fallback
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    # Routing thresholds
    # cognitive_load above this → use shorter, simpler prompts
    high_load_threshold: float = 0.70
    # Tasks that score above this complexity → Claude API fallback
    claude_complexity_threshold: float = 0.80
    # Context
    max_history_turns: int = 20         # conversation turns to keep
    max_context_memories: int = 5       # visual labels to inject
    # Paths
    memory_db_path: Path = field(default_factory=lambda: Path(
        os.environ.get("APPDATA", os.path.expanduser("~"))
    ) / "ULTRON" / "data" / "memory.db")


def load_config() -> LLMConfig:
    data_dir = _ultron_data_dir()
    config_path = data_dir / "config.toml"
    raw: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {})
    ollama = raw.get("ollama", {})
    llm = raw.get("llm", {})
    general = raw.get("general", {})

    data_path = Path(general.get("data_dir", str(data_dir / "data")))

    return LLMConfig(
        ws_url=f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws",
        token=bridge.get("token", os.environ.get("ULTRON_TOKEN", "")),
        user_name=general.get("user_name", ""),
        ollama_url=ollama.get("url", "http://localhost:11434"),
        ollama_model=llm.get("model", "llama3.2:3b"),
        ollama_model_large=llm.get("model_large", "llama3.1:8b"),
        claude_api_key=llm.get(
            "claude_api_key", os.environ.get("ANTHROPIC_API_KEY", "")
        ),
        claude_model=llm.get("claude_model", "claude-sonnet-4-20250514"),
        high_load_threshold=float(llm.get("high_load_threshold", 0.70)),
        claude_complexity_threshold=float(
            llm.get("claude_complexity_threshold", 0.80)
        ),
        max_history_turns=int(llm.get("max_history_turns", 20)),
        max_context_memories=int(llm.get("max_context_memories", 5)),
        memory_db_path=data_path / "memory.db",
    )
