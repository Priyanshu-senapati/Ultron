"""Weather collector — IP geolocation + Open-Meteo current conditions.

Open-Meteo returns ``weather_code`` (WMO) which we map to a short
plain-English label. No API key required.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("ultron.dailydata.weather")


# WMO code → human label. Lifted from the Open-Meteo docs; trimmed to
# the codes the user is most likely to see in India.
_WMO_LABELS: dict[int, str] = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


async def geolocate(client: httpx.AsyncClient) -> Optional[tuple[float, float, str]]:
    """IP-geolocation via ipapi.co (free, no key).
    Returns (lat, lon, city) or None on failure."""
    try:
        r = await client.get("https://ipapi.co/json/", timeout=client.timeout)
        if r.status_code != 200:
            return None
        d = r.json() or {}
        lat = float(d.get("latitude") or 0.0)
        lon = float(d.get("longitude") or 0.0)
        city = str(d.get("city") or d.get("region") or "")
        if lat == 0.0 and lon == 0.0:
            return None
        return lat, lon, city
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("geolocate failed: %s", exc)
        return None


async def fetch_weather(
    client: httpx.AsyncClient,
    lat: float, lon: float, *, city: str = "",
) -> dict[str, Any]:
    """Current weather + 24h min/max from Open-Meteo. Returns a flat dict
    safe to publish on the bus."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "weather_code,wind_speed_10m,is_day"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code"
        "&timezone=auto&forecast_days=1"
    )
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "reason": f"open-meteo error: {exc}"}
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(cur.get("weather_code") or 0)
    high = (daily.get("temperature_2m_max") or [None])[0]
    low = (daily.get("temperature_2m_min") or [None])[0]
    return {
        "available": True,
        "city": city,
        "lat": lat, "lon": lon,
        "temp_c": cur.get("temperature_2m"),
        "feels_c": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "is_day": bool(cur.get("is_day", 1)),
        "label": _WMO_LABELS.get(code, f"code {code}"),
        "code": code,
        "high_c": high,
        "low_c": low,
    }
