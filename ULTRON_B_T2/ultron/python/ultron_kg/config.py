"""Config for Module K (Knowledge Graph)."""
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


# Recognised entity kinds. Unknown kinds are accepted but warned about
# (the graph stores them verbatim — this is a soft taxonomy).
ENTITY_KINDS: tuple[str, ...] = (
    "person", "project", "concept", "place", "organization",
    "decision", "event", "tool", "skill", "habit",
)

EDGE_KINDS: tuple[str, ...] = (
    "knows", "works_on", "uses", "located_at", "part_of",
    "depends_on", "mentioned_in", "decided_at", "led_to",
    "blocks", "related_to", "owns",
)


@dataclass
class KnowledgeGraphConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "kg.db")

    # Cap on rows / nodes a single query may return.
    max_query_rows: int = 500

    # Default neighbourhood radius for ``egonet`` queries.
    default_radius: int = 1


def load_kg_config(config_path: Path | None = None) -> KnowledgeGraphConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    k = raw.get("knowledge_graph", {}) if isinstance(raw.get("knowledge_graph"), dict) else {}
    return KnowledgeGraphConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(k.get("db_path", data_dir / "data" / "kg.db"))),
        max_query_rows=int(k.get("max_query_rows", 500)),
        default_radius=int(k.get("default_radius", 1)),
    )
