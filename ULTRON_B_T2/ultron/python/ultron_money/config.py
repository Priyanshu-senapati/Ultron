"""Config for Module P (Money OS)."""
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


# Default category set — kept short on purpose. Users add more at runtime
# via money_record_request; the store auto-creates categories on first use.
DEFAULT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("food", "need"),
    ("groceries", "need"),
    ("transport", "need"),
    ("rent", "need"),
    ("utilities", "need"),
    ("health", "need"),
    ("entertainment", "want"),
    ("shopping", "want"),
    ("subscriptions", "want"),
    ("travel", "want"),
    ("savings", "save"),
    ("investments", "save"),
    ("salary", "income"),
    ("refund", "income"),
    ("other", "want"),
)


@dataclass
class MoneyConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "money.db")

    # ISO 4217 currency code. User is in India → INR default.
    default_currency: str = "INR"

    # Trigger an alert event when monthly category spend crosses this
    # fraction of its budget. 0.8 → warn at 80 %.
    budget_alert_threshold: float = 0.8

    # Hard cap on how many transactions a single query can return. Stops
    # an LLM from accidentally pulling the whole ledger into a prompt.
    max_query_rows: int = 500


def load_money_config(config_path: Path | None = None) -> MoneyConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    m = raw.get("money", {}) if isinstance(raw.get("money"), dict) else {}
    return MoneyConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(m.get("db_path", data_dir / "data" / "money.db"))),
        default_currency=str(m.get("default_currency", "INR")),
        budget_alert_threshold=float(m.get("budget_alert_threshold", 0.8)),
        max_query_rows=int(m.get("max_query_rows", 500)),
    )
