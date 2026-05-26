"""Periodic system health poller.

Publishes ``system_health_update`` every tick with CPU, RAM, disk, and
GPU metrics. Also publishes ``system_health_alert`` when a threshold
is crossed (e.g. GPU over 85 C).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import Any, Optional

import psutil

from .config import SysHealthConfig

logger = logging.getLogger("ultron.syshealth")


def _gpu_stats() -> Optional[dict[str, Any]]:
    """Query nvidia-smi for GPU metrics. Returns None if unavailable."""
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,"
                "utilization.memory,memory.used,memory.total,"
                "clocks.current.graphics,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        if len(parts) < 8:
            return None
        return {
            "name": parts[0],
            "temp_c": _safe_float(parts[1]),
            "util_pct": _safe_float(parts[2]),
            "mem_util_pct": _safe_float(parts[3]),
            "mem_used_mb": _safe_float(parts[4]),
            "mem_total_mb": _safe_float(parts[5]),
            "clock_mhz": _safe_float(parts[6]),
            "power_w": _safe_float(parts[7]),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _cpu_temp() -> Optional[float]:
    """Try to read CPU temperature. Returns None if unavailable."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for name in ("coretemp", "k10temp", "acpitz", "cpu_thermal"):
            if name in temps and temps[name]:
                return temps[name][0].current
        first = next(iter(temps.values()), [])
        if first:
            return first[0].current
    except (AttributeError, RuntimeError):
        pass
    return None


class SysHealthMonitor:
    def __init__(self, cfg: SysHealthConfig, publish) -> None:
        self._cfg = cfg
        self._publish = publish
        self._task: Optional[asyncio.Task] = None
        self._last_alert: dict[str, float] = {}

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="syshealth")
        logger.info("system health monitor started (poll=%.0fs)", self._cfg.poll_secs)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                snapshot = self._collect()
                await self._publish("system_health_update", snapshot)
                await self._check_alerts(snapshot)
            except Exception as exc:
                logger.error("syshealth tick failed: %s", exc)
            await asyncio.sleep(self._cfg.poll_secs)

    def _collect(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        cpu_freq = psutil.cpu_freq()
        snapshot: dict[str, Any] = {
            "ts": time.time(),
            "cpu_percent": psutil.cpu_percent(interval=0),
            "cpu_count": psutil.cpu_count(),
            "cpu_freq_mhz": round(cpu_freq.current) if cpu_freq else None,
            "cpu_temp_c": _cpu_temp(),
            "ram_percent": round(mem.percent, 1),
            "ram_used_gb": round(mem.used / (1024 ** 3), 1),
            "ram_total_gb": round(mem.total / (1024 ** 3), 1),
        }
        if self._cfg.gpu_enabled:
            gpu = _gpu_stats()
            if gpu:
                snapshot["gpu"] = gpu
        return snapshot

    async def _check_alerts(self, snap: dict[str, Any]) -> None:
        now = time.time()
        alerts: list[dict[str, Any]] = []
        cfg = self._cfg

        cpu_pct = snap.get("cpu_percent", 0)
        if cpu_pct and cpu_pct >= cfg.alert_cpu_percent:
            alerts.append({"kind": "cpu_high", "value": cpu_pct,
                           "threshold": cfg.alert_cpu_percent,
                           "msg": f"CPU at {cpu_pct:.0f}%"})

        ram_pct = snap.get("ram_percent", 0)
        if ram_pct and ram_pct >= cfg.alert_ram_percent:
            alerts.append({"kind": "ram_high", "value": ram_pct,
                           "threshold": cfg.alert_ram_percent,
                           "msg": f"RAM at {ram_pct:.0f}%"})

        cpu_temp = snap.get("cpu_temp_c")
        if cpu_temp and cpu_temp >= cfg.alert_cpu_temp:
            alerts.append({"kind": "cpu_hot", "value": cpu_temp,
                           "threshold": cfg.alert_cpu_temp,
                           "msg": f"CPU temp {cpu_temp:.0f}C"})

        gpu = snap.get("gpu")
        if gpu:
            gpu_temp = gpu.get("temp_c")
            if gpu_temp and gpu_temp >= cfg.alert_gpu_temp:
                alerts.append({"kind": "gpu_hot", "value": gpu_temp,
                               "threshold": cfg.alert_gpu_temp,
                               "msg": f"GPU temp {gpu_temp:.0f}C"})

        for alert in alerts:
            kind = alert["kind"]
            if now - self._last_alert.get(kind, 0) < 300:
                continue
            self._last_alert[kind] = now
            await self._publish("system_health_alert", alert)
            logger.warning("system health alert: %s", alert["msg"])
