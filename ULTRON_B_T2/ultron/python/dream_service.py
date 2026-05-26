"""Entry point for the Dream Mode sidecar."""
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
from ultron_dream import DreamConfig, DreamEngine


def _load_config() -> DreamConfig:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    d = raw.get("dream", {})
    return DreamConfig(
        ws_url=f"ws://{bridge['bind']}/ws",
        ws_token=bridge["token"],
        idle_threshold_minutes=float(d.get("idle_threshold_minutes", 30.0)),
        max_insights=int(d.get("max_insights", 5)),
        ollama_model=str(d.get("ollama_model", raw.get("llm", {}).get("model", "llama3.1:8b"))),
        enabled=bool(d.get("enabled", True)),
    )


async def _main() -> None:
    cfg = _load_config()
    engine = DreamEngine(cfg, publish=None)

    async def on_event(event):
        kind = event.get("kind", "")
        if kind in ("voice_transcript", "input_activity", "voice_state_changed"):
            engine.on_activity()

    bridge = UltronBridge(
        url=cfg.ws_url,
        token=cfg.ws_token,
        on_event=on_event,
        subscribe_to=["voice_transcript", "input_activity", "voice_state_changed"],
        role="dream",
    )
    engine._publish = bridge.publish
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
