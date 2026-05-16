"""Snapshot collectors for time/battery/wifi/bluetooth.

Each function is sync and fast. The service runs them in
``run_in_executor`` so the bridge loop isn't blocked. Anything that
shells out to netsh/Get-PnpDevice is gated to the heavy tick.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("ultron.sysinfo.collector")


def collect_time(timezone: str) -> dict[str, Any]:
    """Local time + ISO timestamp + day-of-week + tz."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001  — fall back to UTC if tz is bogus
        tz = ZoneInfo("UTC")
        timezone = "UTC"
    now = datetime.now(tz)
    return {
        "iso": now.isoformat(),
        "hh_mm": now.strftime("%H:%M"),
        "date": now.strftime("%a, %d %b %Y"),
        "weekday": now.strftime("%A"),
        "tz": timezone,
        "hour": now.hour,
        "minute": now.minute,
    }


def collect_battery() -> dict[str, Any]:
    """Battery percent + AC state + secs-left. {available: False} on desktops."""
    try:
        import psutil  # type: ignore[import]
    except ImportError:
        return {"available": False, "reason": "psutil missing"}
    try:
        b = psutil.sensors_battery()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"psutil error: {exc}"}
    if b is None:
        return {"available": False, "reason": "no battery"}
    secs_left = int(b.secsleft) if b.secsleft is not None and b.secsleft > 0 else None
    return {
        "available": True,
        "percent": round(float(b.percent), 1),
        "plugged": bool(b.power_plugged),
        "secs_left": secs_left,
    }


def _run_ps(cmd: str, *, timeout: float = 4.0) -> str:
    """Run a small PowerShell snippet; return stdout (empty on failure)."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return (result.stdout or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("ps shell-out failed: %s", exc)
        return ""


def collect_wifi() -> dict[str, Any]:
    """SSID, signal %, BSSID via netsh. Returns connected=False if unplugged
    or if WLAN is off."""
    out = _run_ps("netsh wlan show interfaces")
    if not out:
        return {"available": False, "connected": False}
    info: dict[str, Any] = {"available": True, "connected": False}
    for line in out.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "state" and val.lower() == "connected":
            info["connected"] = True
        elif key == "ssid":
            info["ssid"] = val
        elif key == "signal":
            info["signal"] = val
        elif key == "bssid":
            info["bssid"] = val
        elif key == "radio type":
            info["radio"] = val
    # If netsh ran but reported State: disconnected, still mark "available"
    # so the HUD can show "wifi off" rather than missing the section.
    return info


def collect_bluetooth() -> dict[str, Any]:
    """Bluetooth radio state + count of connected devices."""
    # The radio's "Status" tells us OK/Disabled. Connected devices are
    # the BluetoothLE / Bluetooth devices whose Status is OK.
    radio_out = _run_ps(
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object { $_.FriendlyName -like '*Radio*' -or "
        "$_.Service -eq 'BTHUSB' } | Select-Object -First 1 | "
        "Format-Table -HideTableHeaders Status"
    )
    radio_status = (radio_out.splitlines() or [""])[0].strip()
    devices_out = _run_ps(
        "Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'OK' -and $_.FriendlyName -notlike '*Radio*' } | "
        "Measure-Object | Format-Table -HideTableHeaders Count"
    )
    try:
        connected = int((devices_out.splitlines() or ["0"])[0].strip() or 0)
    except ValueError:
        connected = 0
    return {
        "available": bool(radio_status),
        "radio": radio_status or "unknown",
        "connected_devices": connected,
    }
