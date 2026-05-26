"""Entry point for the emotion-reactive environment sidecar."""
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
from ultron_emotion_env import EmotionReactor


async def _main() -> None:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge_cfg = raw["bridge"]

    reactor = EmotionReactor(publish=None)

    async def on_event(event):
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        if kind == "emotion_state_changed":
            await reactor.on_emotion(payload)

    bridge = UltronBridge(
        url=f"ws://{bridge_cfg['bind']}/ws",
        token=bridge_cfg["token"],
        on_event=on_event,
        subscribe_to=["emotion_state_changed"],
        role="emotion-env",
    )
    reactor._publish = bridge.publish
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
