"""Entry point for the clipboard intelligence sidecar."""
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
from ultron_clipboard import ClipboardConfig, ClipboardWatcher


def _load_config() -> ClipboardConfig:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    cb = raw.get("clipboard", {})
    return ClipboardConfig(
        ws_url=f"ws://{bridge['bind']}/ws",
        ws_token=bridge["token"],
        poll_secs=float(cb.get("poll_secs", 2.0)),
        max_content_chars=int(cb.get("max_content_chars", 500)),
    )


async def _main() -> None:
    cfg = _load_config()
    bridge = UltronBridge(
        url=cfg.ws_url,
        token=cfg.ws_token,
        on_event=lambda e: asyncio.sleep(0),
        subscribe_to=[],
        role="clipboard",
    )
    watcher = ClipboardWatcher(cfg, bridge.publish)
    watcher.start()
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
