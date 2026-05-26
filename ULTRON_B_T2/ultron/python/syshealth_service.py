"""Entry point for the system health monitor sidecar."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from ultron_bridge import UltronBridge
from ultron_syshealth import SysHealthConfig, SysHealthMonitor


def _load_config() -> SysHealthConfig:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    sh = raw.get("syshealth", {})
    return SysHealthConfig(
        ws_url=f"ws://{bridge['bind']}/ws",
        ws_token=bridge["token"],
        poll_secs=float(sh.get("poll_secs", 10.0)),
        gpu_enabled=bool(sh.get("gpu_enabled", True)),
        alert_cpu_temp=float(sh.get("alert_cpu_temp", 90.0)),
        alert_gpu_temp=float(sh.get("alert_gpu_temp", 85.0)),
        alert_ram_percent=float(sh.get("alert_ram_percent", 90.0)),
        alert_cpu_percent=float(sh.get("alert_cpu_percent", 95.0)),
    )


async def _main() -> None:
    cfg = _load_config()
    bridge = UltronBridge(
        url=cfg.ws_url,
        token=cfg.ws_token,
        on_event=lambda e: asyncio.sleep(0),
        subscribe_to=[],
        role="syshealth",
    )
    monitor = SysHealthMonitor(cfg, bridge.publish)

    async def _on_connected(_):
        monitor.start()

    bridge._on_connect_callback = _on_connected
    monitor.start()
    await bridge.run_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
