"""DailyDataService — weather + sensex + India news on three timers.

Subscribes:
  - ``weather_request``       → publishes fresh ``weather_update`` now
  - ``stocks_request``        → publishes fresh ``stocks_update`` now
  - ``news_request``          → publishes fresh ``news_update`` now

Publishes:
  - ``weather_update`` / ``stocks_update`` / ``news_update``
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from ultron_bridge import UltronBridge

from .config import DailyDataConfig
from .news import fetch_headlines
from .stocks import fetch_quotes, insight_line
from .weather import fetch_weather, geolocate

logger = logging.getLogger("ultron.dailydata.service")


_IST = ZoneInfo("Asia/Kolkata")


def _india_market_open(now_utc: Optional[datetime] = None) -> bool:
    """True if NSE/BSE is in regular hours (Mon-Fri 09:15-15:30 IST)."""
    now = (now_utc or datetime.now()).astimezone(_IST)
    if now.weekday() >= 5:           # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


class DailyDataService:
    def __init__(self, config: DailyDataConfig) -> None:
        self._cfg = config
        self._bridge: Optional[UltronBridge] = None
        self._stop = asyncio.Event()
        self._client: Optional[httpx.AsyncClient] = None
        self._tasks: list[asyncio.Task[None]] = []
        # Cached coords so we don't re-geolocate every weather poll.
        self._lat: float = config.latitude
        self._lon: float = config.longitude
        self._city: str = ""

    # ── Per-source poll loops ──────────────────────────────────────────

    async def _weather_loop(self) -> None:
        assert self._client is not None
        await self._refresh_weather_once()
        period = max(60, self._cfg.weather_poll_minutes * 60)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=period)
                return
            except asyncio.TimeoutError:
                pass
            await self._refresh_weather_once()

    async def _stocks_loop(self) -> None:
        await self._refresh_stocks_once()
        while not self._stop.is_set():
            mins = (self._cfg.stocks_active_minutes
                    if _india_market_open() else self._cfg.stocks_idle_minutes)
            period = max(60, mins * 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=period)
                return
            except asyncio.TimeoutError:
                pass
            await self._refresh_stocks_once()

    async def _news_loop(self) -> None:
        await self._refresh_news_once()
        period = max(60, self._cfg.news_poll_minutes * 60)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=period)
                return
            except asyncio.TimeoutError:
                pass
            await self._refresh_news_once()

    # ── Refresh implementations ────────────────────────────────────────

    async def _refresh_weather_once(self) -> None:
        if self._client is None:
            return
        if self._lat == 0.0 and self._lon == 0.0:
            geo = await geolocate(self._client)
            if geo is not None:
                self._lat, self._lon, self._city = geo
            else:
                logger.warning("weather: geolocation failed; skipping tick")
                return
        payload = await fetch_weather(
            self._client, self._lat, self._lon, city=self._city,
        )
        payload["ts"] = time.time()
        if self._bridge is not None:
            await self._bridge.publish("weather_update", payload)

    async def _refresh_stocks_once(self) -> None:
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None, lambda: fetch_quotes(self._cfg.tickers)
        )
        payload = {
            "ts": time.time(),
            "market_open": _india_market_open(),
            "rows": rows,
            "insight": insight_line(rows),
        }
        if self._bridge is not None:
            await self._bridge.publish("stocks_update", payload)

    async def _refresh_news_once(self) -> None:
        if self._client is None:
            return
        headlines = await fetch_headlines(
            self._client,
            country=self._cfg.news_country,
            lang=self._cfg.news_lang,
            topic=self._cfg.news_topic,
            limit=self._cfg.news_max_headlines,
        )
        payload = {"ts": time.time(), "headlines": headlines}
        if self._bridge is not None:
            await self._bridge.publish("news_update", payload)

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start dailydata")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["weather_request", "stocks_request", "news_request"],
            role="dailydata",
        )
        self._client = httpx.AsyncClient(
            timeout=self._cfg.http_timeout,
            headers={"User-Agent": "ULTRON/1.0 (+local cognitive twin)"},
        )
        self._tasks = [
            asyncio.create_task(self._weather_loop()),
            asyncio.create_task(self._stocks_loop()),
            asyncio.create_task(self._news_loop()),
        ]
        logger.info(
            "DailyDataService starting — tickers=%s news=%s/%s/%s",
            self._cfg.tickers, self._cfg.news_country,
            self._cfg.news_lang, self._cfg.news_topic,
        )
        try:
            await self._bridge.run_forever()
        finally:
            self._stop.set()
            for t in self._tasks:
                t.cancel()
            for t in self._tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._client.aclose()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        if kind == "weather_request":
            asyncio.create_task(self._refresh_weather_once())
        elif kind == "stocks_request":
            asyncio.create_task(self._refresh_stocks_once())
        elif kind == "news_request":
            asyncio.create_task(self._refresh_news_once())
