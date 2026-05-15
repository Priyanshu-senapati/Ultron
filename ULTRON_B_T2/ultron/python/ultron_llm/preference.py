"""
preference.py — PreferenceEngine: track which responses needed correction.

A correction is detected when:
  - The user's next message starts with "no", "that's wrong", "not what I meant",
    "incorrect", "wrong", etc. within 60 seconds of the assistant's response.
  - The user sends a follow-up that directly contradicts or re-asks the same thing.

PreferenceEngine uses this signal to:
  1. Log the (shard, cognitive_load_band, correction) triplet.
  2. Adjust routing: if a shard has high correction rate at a given load band,
     prefer a different shard for similar contexts.
  3. Prefer Claude API fallback when local model shows repeated corrections.

This is the Phase 1 stub — it accumulates data and adjusts weights.
Full RLHF (Dopamine Marker, Module Y) uses this as a seed.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ultron.llm.preference")

# Correction indicators — checked against the start of user messages.
CORRECTION_SIGNALS = (
    "no,", "no that", "not what", "that's wrong", "thats wrong",
    "incorrect", "wrong,", "you misunderstood", "that's not",
    "thats not", "i meant", "actually,", "no —", "no –",
)


@dataclass
class PreferenceRecord:
    ts_unix_ms: int
    shard: str
    load_band: str    # "low"|"medium"|"high"
    was_correction: bool
    response_length: int


def _load_band(cognitive_load: float) -> str:
    """Bucket a cognitive_load value into a coarse band. Exposed at module
    level so other modules (service.py) can import it directly."""
    if cognitive_load < 0.35:
        return "low"
    if cognitive_load < 0.70:
        return "medium"
    return "high"


def _is_correction(text: str) -> bool:
    t = text.strip().lower()
    return any(t.startswith(sig) for sig in CORRECTION_SIGNALS)


class PreferenceEngine:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._last_response_ts: float = 0.0
        self._last_shard: Optional[str] = None
        self._last_load: float = 0.0
        self._last_response_len: int = 0
        # Ensure parent dir exists before sqlite tries to open the file.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create preference table if it doesn't exist."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS preference_records (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_unix_ms      INTEGER NOT NULL,
                        shard           TEXT NOT NULL,
                        load_band       TEXT NOT NULL,
                        was_correction  INTEGER NOT NULL,
                        response_length INTEGER NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pref_ts ON preference_records(ts_unix_ms)"
                )
        except sqlite3.Error as exc:
            logger.warning("preference db init failed: %s", exc)

    def on_response(
        self, shard: str, cognitive_load: float, response: str
    ) -> None:
        """Call after every assistant response."""
        self._last_response_ts = time.monotonic()
        self._last_shard = shard
        self._last_load = cognitive_load
        self._last_response_len = len(response)

    def on_user_message(self, text: str) -> None:
        """
        Call before processing each user message.
        Detects if this message is a correction of the previous response.
        """
        if self._last_shard is None:
            return
        elapsed = time.monotonic() - self._last_response_ts
        if elapsed > 90.0:
            # Too long ago — don't attribute.
            return
        correction = _is_correction(text)
        rec = PreferenceRecord(
            ts_unix_ms=int(time.time() * 1000),
            shard=self._last_shard,
            load_band=_load_band(self._last_load),
            was_correction=correction,
            response_length=self._last_response_len,
        )
        self._persist(rec)

    def correction_rate(
        self, shard: str, load_band: str, lookback_days: int = 7
    ) -> float:
        """
        Correction rate for (shard, load_band) over the last N days.
        Returns 0.0 if no data. Used by service.py to downweight shards.
        """
        since_ms = int((time.time() - lookback_days * 86400) * 1000)
        try:
            with sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True
            ) as conn:
                row = conn.execute("""
                    SELECT
                        SUM(was_correction) as corrections,
                        COUNT(*) as total
                    FROM preference_records
                    WHERE shard = ? AND load_band = ? AND ts_unix_ms >= ?
                """, (shard, load_band, since_ms)).fetchone()
            if row and row[1] and row[1] > 0:
                return float(row[0]) / float(row[1])
        except sqlite3.Error as exc:
            logger.debug("correction_rate query failed: %s", exc)
        return 0.0

    def _persist(self, rec: PreferenceRecord) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO preference_records
                        (ts_unix_ms, shard, load_band, was_correction, response_length)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    rec.ts_unix_ms, rec.shard, rec.load_band,
                    int(rec.was_correction), rec.response_length,
                ))
        except sqlite3.Error as exc:
            logger.warning("preference persist failed: %s", exc)
