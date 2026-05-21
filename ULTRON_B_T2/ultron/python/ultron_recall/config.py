"""Config for the unified Recall (long-term memory) service."""
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
class RecallConfig:
    ws_url: str
    ws_token: str

    db_path: Path = Path()

    # ── Embedding model ──────────────────────────────────────────────
    # Defaults to the same all-MiniLM-L6-v2 used by ultron_knowledge so
    # both layers' vectors live in the same space (we may later allow
    # cross-source queries).
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # ── Indexing ─────────────────────────────────────────────────────
    # Skip turns shorter than this — "ok", "yes", "what" carry no real
    # information and would just dilute search results.
    min_content_chars: int = 8
    # Cap content length at ingest. Long turns still get indexed but
    # the embedded representation is truncated to this many chars.
    # 1500 ≈ ~600 BPE tokens, well within model context.
    max_indexed_chars: int = 1500
    # Batch embedding flushes — index N pending turns at a time to
    # amortize the sentence-transformer call cost.
    embed_batch_size: int = 8
    embed_flush_interval_secs: float = 5.0

    # ── Retrieval ────────────────────────────────────────────────────
    default_top_k: int = 6
    max_top_k: int = 30
    # Minimum cosine to be considered "relevant". Below this the hit
    # is likely noise and we drop it before returning.
    min_score: float = 0.30
    # When returning turns, pair each with N neighbouring turns in the
    # same conversation so the LLM sees enough context to interpret it.
    neighbour_window: int = 1

    # ── Reflections (Phase 2) ─────────────────────────────────────────
    enable_reflections: bool = True
    reflection_chars: int = 1200

    # ── Fact extraction (Phase 2) ─────────────────────────────────────
    enable_fact_extraction: bool = True
    # First extract pass waits this long after boot so content has time
    # to accumulate (and the LLM isn't fighting voice for Ollama).
    extract_first_delay_secs: float = 180.0
    # How often to attempt an extract pass while running.
    extract_interval_secs: float = 600.0
    # Each pass operates on at most this many new turns.
    extract_max_turns_per_pass: int = 24
    # Minimum new turns required to bother extracting.
    extract_min_new_turns: int = 4


def load_recall_config(config_path: Path | None = None) -> RecallConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    r = raw.get("recall", {}) if isinstance(raw.get("recall"), dict) else {}
    db_default = data_dir / "data" / "recall.db"
    return RecallConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(r.get("db_path", db_default))),
        embedding_model=str(r.get("embedding_model",
                                  "sentence-transformers/all-MiniLM-L6-v2")),
        min_content_chars=int(r.get("min_content_chars", 8)),
        max_indexed_chars=int(r.get("max_indexed_chars", 1500)),
        embed_batch_size=int(r.get("embed_batch_size", 8)),
        embed_flush_interval_secs=float(r.get("embed_flush_interval_secs", 5.0)),
        default_top_k=int(r.get("default_top_k", 6)),
        max_top_k=int(r.get("max_top_k", 30)),
        min_score=float(r.get("min_score", 0.30)),
        neighbour_window=int(r.get("neighbour_window", 1)),
        enable_reflections=bool(r.get("enable_reflections", True)),
        reflection_chars=int(r.get("reflection_chars", 1200)),
        enable_fact_extraction=bool(r.get("enable_fact_extraction", True)),
        extract_first_delay_secs=float(r.get("extract_first_delay_secs", 180.0)),
        extract_interval_secs=float(r.get("extract_interval_secs", 600.0)),
        extract_max_turns_per_pass=int(r.get("extract_max_turns_per_pass", 24)),
        extract_min_new_turns=int(r.get("extract_min_new_turns", 4)),
    )
