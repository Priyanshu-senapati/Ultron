"""Entry point for the Context Tunneling sidecar."""
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
from ultron_context_tunnel import ContextTunnel


async def _main() -> None:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge_cfg = raw["bridge"]

    tunnel = ContextTunnel(publish=None)

    async def on_event(event):
        kind = event.get("kind", "")
        payload = event.get("payload") or {}

        if kind == "focus_app":
            app = payload.get("app") or payload.get("title", "")
            title = payload.get("title", "")
            restore = tunnel.on_focus_change(app, title)
            if restore:
                away = restore["away_minutes"]
                app_name = restore["app"]
                last = restore.get("last_transcript", "")
                brief = f"Welcome back to {app_name}."
                if last:
                    brief += f" Last time you were here: {last}."
                brief += f" You were away {away:.0f} minutes."
                await bridge.publish("context_tunnel_restore", {
                    "brief": brief,
                    **restore,
                })
                logging.getLogger("ultron.context_tunnel").info(
                    "context restore: %s (away %.0fm)", app_name, away)

        elif kind == "voice_transcript":
            tunnel.on_transcript(payload.get("text", ""))

        elif kind == "app_detail":
            tunnel.on_app_detail(
                payload.get("app", ""),
                payload.get("detail", {}).get("file", "") or payload.get("title", ""),
            )

    bridge = UltronBridge(
        url=f"ws://{bridge_cfg['bind']}/ws",
        token=bridge_cfg["token"],
        on_event=on_event,
        subscribe_to=["focus_app", "voice_transcript", "app_detail"],
        role="context-tunnel",
    )
    tunnel._publish = bridge.publish
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
