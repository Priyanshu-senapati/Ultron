"""Config for the Daily Data bridge."""
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


# Default tickers the user is likely to watch alongside Sensex. yfinance
# uses ``^BSESN`` for Sensex itself, ``^NSEI`` for Nifty 50. Suffix
# ``.NS`` for individual NSE stocks (e.g. ``RELIANCE.NS``).
DEFAULT_TICKERS: tuple[str, ...] = ("^BSESN", "^NSEI")


@dataclass
class DailyDataConfig:
    ws_url: str
    ws_token: str

    # ── Weather (Open-Meteo) ───────────────────────────────────────────
    # Set explicit lat/lon to skip IP geolocation. Leave 0.0 to auto-
    # detect via ipapi.co (free, no key).
    latitude: float = 0.0
    longitude: float = 0.0
    weather_poll_minutes: int = 30

    # ── Stocks (yfinance) ──────────────────────────────────────────────
    tickers: tuple[str, ...] = DEFAULT_TICKERS
    # During Indian market hours (09:15 – 15:30 IST, Mon-Fri) poll more
    # often. Outside market hours fall back to ``stocks_idle_minutes``.
    stocks_active_minutes: int = 5
    stocks_idle_minutes: int = 60

    # ── News (Google News RSS) ─────────────────────────────────────────
    news_country: str = "IN"
    news_lang: str = "en"
    # The RSS topic to pull. NATION = India national news; TOP = global
    # top stories. Either is fine for "major news happening in India".
    news_topic: str = "NATION"
    news_poll_minutes: int = 15
    news_max_headlines: int = 5

    # HTTP timeout for every outbound call (seconds).
    http_timeout: float = 10.0


def load_dailydata_config(config_path: Path | None = None) -> DailyDataConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    d = raw.get("dailydata", {}) if isinstance(raw.get("dailydata"), dict) else {}
    tickers_raw = d.get("tickers") or list(DEFAULT_TICKERS)
    if isinstance(tickers_raw, str):
        tickers_raw = [tickers_raw]
    return DailyDataConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        latitude=float(d.get("latitude", 0.0)),
        longitude=float(d.get("longitude", 0.0)),
        weather_poll_minutes=int(d.get("weather_poll_minutes", 30)),
        tickers=tuple(str(t) for t in tickers_raw),
        stocks_active_minutes=int(d.get("stocks_active_minutes", 5)),
        stocks_idle_minutes=int(d.get("stocks_idle_minutes", 60)),
        news_country=str(d.get("news_country", "IN")),
        news_lang=str(d.get("news_lang", "en")),
        news_topic=str(d.get("news_topic", "NATION")),
        news_poll_minutes=int(d.get("news_poll_minutes", 15)),
        news_max_headlines=int(d.get("news_max_headlines", 5)),
        http_timeout=float(d.get("http_timeout", 10.0)),
    )
