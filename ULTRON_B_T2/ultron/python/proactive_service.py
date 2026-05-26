"""Entry point for the proactive suggestions sidecar."""
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
from ultron_proactive import ProactiveConfig, ProactiveEngine


def _load_config() -> ProactiveConfig:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    p = raw.get("proactive", {})
    return ProactiveConfig(
        ws_url=f"ws://{bridge['bind']}/ws",
        ws_token=bridge["token"],
        tick_secs=float(p.get("tick_secs", 300.0)),
        boot_delay_secs=float(p.get("boot_delay_secs", 60.0)),
        cooldown_secs=float(p.get("cooldown_secs", 1800.0)),
        quiet_hours_start=int(p.get("quiet_hours_start", 22)),
        quiet_hours_end=int(p.get("quiet_hours_end", 7)),
        enabled=bool(p.get("enabled", True)),
    )


async def _main() -> None:
    cfg = _load_config()
    bridge = UltronBridge(
        url=cfg.ws_url,
        token=cfg.ws_token,
        on_event=lambda e: asyncio.sleep(0),
        subscribe_to=[],
        role="proactive",
    )
    engine = ProactiveEngine(cfg, bridge.publish)
    engine.start()
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
