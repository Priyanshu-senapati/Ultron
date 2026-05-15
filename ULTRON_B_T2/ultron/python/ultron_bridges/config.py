"""Load `[bridges.*]` from `%APPDATA%/ULTRON/config.toml`.

Each integration owns its own subsection. Top-level `[bridges]` keys are
shared (HTTP receiver port, default poll interval, etc.). Missing
subsections mean that bridge is disabled — keeps adoption opt-in.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


@dataclass
class SpotifyConfig:
    enabled: bool = False
    client_id: str = ""
    # PKCE flow — no client_secret needed. Token cache sits next to config.
    redirect_uri: str = "http://127.0.0.1:8765/spotify_callback"
    poll_secs: float = 5.0


@dataclass
class BrowserTabConfig:
    enabled: bool = False
    # Local HTTP receiver for the browser extension to POST to.
    bind_host: str = "127.0.0.1"
    bind_port: int = 8766
    # Drop signal if the browser hasn't pinged in this long — the user
    # probably alt-tabbed out of the browser entirely.
    staleness_secs: float = 30.0


@dataclass
class GithubConfig:
    enabled: bool = False
    # Fine-grained PAT. Scopes needed: read:user, notifications, repo (read).
    token: str = ""
    username: str = ""
    poll_secs: float = 60.0


@dataclass
class GoogleConfig:
    enabled: bool = False
    # OAuth client_id/secret from Google Cloud Console.
    client_id: str = ""
    client_secret: str = ""
    # Token cache path; we write/refresh as needed.
    token_cache: str = ""
    calendar_poll_secs: float = 60.0
    gmail_poll_secs: float = 120.0


@dataclass
class AppDetailConfig:
    enabled: bool = True
    # Per-provider toggles
    vscode: bool = True
    discord: bool = True
    generic: bool = True
    poll_secs: float = 5.0


@dataclass
class DevWatchConfig:
    enabled: bool = True
    repo_path: str = r"C:\dev"
    git_poll_secs: float = 60.0
    state_path: str = ""  # empty → defaults to data_dir/dev_watch_state.json


@dataclass
class ClaudeSessionConfig:
    enabled: bool = True
    sessions_dir: str = ""  # empty → ~/.claude/projects/C--dev
    poll_secs: float = 4.0
    snippet_chars: int = 400


@dataclass
class BridgesConfig:
    # Bridge (WS) — reused from the same [bridge] section as the rest of ULTRON.
    ws_url: str = "ws://127.0.0.1:9420/ws"
    ws_token: str = ""

    # Per-integration
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)
    browser_tab: BrowserTabConfig = field(default_factory=BrowserTabConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)
    app_detail: AppDetailConfig = field(default_factory=AppDetailConfig)
    dev_watch: DevWatchConfig = field(default_factory=DevWatchConfig)
    claude_session: ClaudeSessionConfig = field(default_factory=ClaudeSessionConfig)

    # Where Spotify/Google token caches live.
    data_dir: Path = field(default_factory=_ultron_data_dir)


def _section(raw: dict[str, Any], *path: str) -> dict[str, Any]:
    """Walk a TOML dict path, returning {} if any segment is missing.

    Lets us treat `[bridges.spotify]` as optional without nested .get() chains.
    """
    cur: Any = raw
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return {}
        cur = cur[key]
    return cur if isinstance(cur, dict) else {}


def load_bridges_config(config_path: Path | None = None) -> BridgesConfig:
    """Load the bridges config from `%APPDATA%/ULTRON/config.toml`."""
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    sp = _section(raw, "bridges", "spotify")
    bt = _section(raw, "bridges", "browser_tab")
    gh = _section(raw, "bridges", "github")
    go = _section(raw, "bridges", "google")
    ad = _section(raw, "bridges", "app_detail")
    dw = _section(raw, "bridges", "dev_watch")
    cs = _section(raw, "bridges", "claude_session")

    google_token_cache = go.get("token_cache") or str(data_dir / "google_token.json")

    return BridgesConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        spotify=SpotifyConfig(
            enabled=bool(sp.get("enabled", False)),
            client_id=str(sp.get("client_id", "")),
            redirect_uri=str(sp.get("redirect_uri", SpotifyConfig.redirect_uri)),
            poll_secs=float(sp.get("poll_secs", SpotifyConfig.poll_secs)),
        ),
        browser_tab=BrowserTabConfig(
            enabled=bool(bt.get("enabled", False)),
            bind_host=str(bt.get("bind_host", BrowserTabConfig.bind_host)),
            bind_port=int(bt.get("bind_port", BrowserTabConfig.bind_port)),
            staleness_secs=float(bt.get("staleness_secs", BrowserTabConfig.staleness_secs)),
        ),
        github=GithubConfig(
            enabled=bool(gh.get("enabled", False)),
            token=str(gh.get("token", "")),
            username=str(gh.get("username", "")),
            poll_secs=float(gh.get("poll_secs", GithubConfig.poll_secs)),
        ),
        google=GoogleConfig(
            enabled=bool(go.get("enabled", False)),
            client_id=str(go.get("client_id", "")),
            client_secret=str(go.get("client_secret", "")),
            token_cache=google_token_cache,
            calendar_poll_secs=float(go.get("calendar_poll_secs", GoogleConfig.calendar_poll_secs)),
            gmail_poll_secs=float(go.get("gmail_poll_secs", GoogleConfig.gmail_poll_secs)),
        ),
        app_detail=AppDetailConfig(
            enabled=bool(ad.get("enabled", True)),
            vscode=bool(ad.get("vscode", True)),
            discord=bool(ad.get("discord", True)),
            generic=bool(ad.get("generic", True)),
            poll_secs=float(ad.get("poll_secs", AppDetailConfig.poll_secs)),
        ),
        dev_watch=DevWatchConfig(
            enabled=bool(dw.get("enabled", True)),
            repo_path=str(dw.get("repo_path", DevWatchConfig.repo_path)),
            git_poll_secs=float(dw.get("git_poll_secs", DevWatchConfig.git_poll_secs)),
            state_path=str(dw.get("state_path", "")),
        ),
        claude_session=ClaudeSessionConfig(
            enabled=bool(cs.get("enabled", True)),
            sessions_dir=str(cs.get("sessions_dir", "")),
            poll_secs=float(cs.get("poll_secs", ClaudeSessionConfig.poll_secs)),
            snippet_chars=int(cs.get("snippet_chars", ClaudeSessionConfig.snippet_chars)),
        ),
        data_dir=data_dir,
    )
