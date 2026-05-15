"""bridges_service.py — Module Bridges entry point.

Run:
    python python/bridges_service.py

Reads `[bridges.*]` from %APPDATA%/ULTRON/config.toml and starts every
enabled integration as a supervised Bridge. Failures in one bridge do
not affect the others.

Bridges currently wired:
- Spotify         → spotify_now_playing
- Browser tab     → browser_tab        (via local HTTP receiver + extension)
- GitHub          → gh_activity
- Google          → calendar_upcoming, gmail_unread
- App detail      → app_detail         (per-provider sub-providers)
"""
from __future__ import annotations

import asyncio
import logging
import os

from ultron_bridges import BridgesService, load_bridges_config


logging.basicConfig(
    level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ultron.bridges_service")


def _build_service() -> BridgesService:
    cfg = load_bridges_config()
    svc = BridgesService(cfg)

    enabled: list[str] = []

    # Lazy imports keep optional dependencies optional. If `httpx` is the
    # only required dep and a user only enables `app_detail`, they don't
    # need `google-auth-oauthlib` installed.
    if cfg.spotify.enabled:
        from ultron_bridges.spotify import SpotifyBridge
        svc.register(SpotifyBridge(publish=None, cfg=cfg.spotify, data_dir=cfg.data_dir))  # type: ignore[arg-type]
        enabled.append("spotify")

    if cfg.browser_tab.enabled:
        from ultron_bridges.browser_tab import BrowserTabBridge
        svc.register(BrowserTabBridge(publish=None, cfg=cfg.browser_tab))  # type: ignore[arg-type]
        enabled.append("browser_tab")

    if cfg.github.enabled:
        from ultron_bridges.github import GithubBridge
        svc.register(GithubBridge(publish=None, cfg=cfg.github))  # type: ignore[arg-type]
        enabled.append("github")

    if cfg.google.enabled:
        from ultron_bridges.google import GoogleBridge
        svc.register(GoogleBridge(publish=None, cfg=cfg.google))  # type: ignore[arg-type]
        enabled.append("google")

    if cfg.app_detail.enabled:
        from ultron_bridges.app_detail import AppDetailBridge
        svc.register(AppDetailBridge(publish=None, cfg=cfg.app_detail))  # type: ignore[arg-type]
        enabled.append("app_detail")

    if cfg.dev_watch.enabled:
        from ultron_bridges.dev_watch import DevWatchBridge, DevWatchConfig as _DW
        dw_cfg = _DW(
            enabled=cfg.dev_watch.enabled,
            repo_path=cfg.dev_watch.repo_path,
            git_poll_secs=cfg.dev_watch.git_poll_secs,
            state_path=cfg.dev_watch.state_path,
        )
        svc.register(DevWatchBridge(publish=None, cfg=dw_cfg, data_dir=cfg.data_dir / "data"))  # type: ignore[arg-type]
        enabled.append("dev_watch")

    if cfg.claude_session.enabled:
        from ultron_bridges.claude_session import ClaudeSessionBridge, ClaudeSessionConfig as _CS
        cs_cfg = _CS(
            enabled=cfg.claude_session.enabled,
            sessions_dir=cfg.claude_session.sessions_dir,
            poll_secs=cfg.claude_session.poll_secs,
            snippet_chars=cfg.claude_session.snippet_chars,
        )
        svc.register(ClaudeSessionBridge(publish=None, cfg=cs_cfg))  # type: ignore[arg-type]
        enabled.append("claude_session")

    logger.info(
        "bridges_service starting — enabled: %s",
        ", ".join(enabled) if enabled else "(none — edit [bridges.*] in config.toml)",
    )
    return svc


async def _main() -> None:
    svc = _build_service()
    await svc.run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
