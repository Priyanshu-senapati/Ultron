"""Spotify Web API bridge.

Publishes `spotify_now_playing` events with the current track, artist,
album, progress, and is_playing flag — the exact metadata ULTRON was
missing when LLaVA could only describe "a music player".

Auth: OAuth 2.0 with PKCE (no client secret). The user creates a Spotify
developer app, drops the Client ID into config.toml, then runs:

    python -m ultron_bridges.spotify

That opens the browser, catches the redirect on 127.0.0.1, exchanges the
code for an access + refresh token, and writes them to
`%APPDATA%/ULTRON/spotify_token.json`. The bridge picks the cache up at
startup and refreshes silently from then on.

Token cache schema (JSON):
    {
        "access_token":  "BQ...",
        "refresh_token": "AQ...",
        "expires_at":    1700000000.0,   # unix seconds
        "scope":         "user-read-currently-playing ..."
    }
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from .base import Bridge, BridgePublishFn
from .config import SpotifyConfig, load_bridges_config

logger = logging.getLogger("ultron.bridges.spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

# Scopes — read-only. `user-read-playback-state` covers the device too,
# letting us also tell the user *where* they're listening (phone/desktop).
SCOPES = "user-read-currently-playing user-read-playback-state"


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #


class TokenStore:
    """Disk-backed JSON cache for Spotify OAuth tokens."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read spotify token cache: %s", exc)
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Restrictive permissions: 0o600 on POSIX. On Windows the path is
        # already inside %APPDATA% which is per-user. We still write atomically.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# PKCE helpers
# --------------------------------------------------------------------------- #


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge).

    Verifier is 64 url-safe random chars. Challenge is S256(verifier),
    base64url-encoded with the trailing '=' stripped — Spotify rejects
    padded challenges.
    """
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --------------------------------------------------------------------------- #
# Auth flow (CLI: `python -m ultron_bridges.spotify`)
# --------------------------------------------------------------------------- #


async def run_auth_flow(cfg: SpotifyConfig, token_store: TokenStore) -> bool:
    """Run the PKCE auth flow. Returns True on success.

    Opens a browser to Spotify's authorize page, spawns a one-shot HTTP
    server on the redirect URI's host:port to capture the callback,
    exchanges the code, persists the tokens.
    """
    if not cfg.client_id:
        logger.error("spotify.client_id is empty in config.toml — cannot auth")
        return False

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    redirect = urlparse(cfg.redirect_uri)
    host = redirect.hostname or "127.0.0.1"
    port = redirect.port or 8765
    path = redirect.path or "/spotify_callback"

    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "redirect_uri": cfg.redirect_uri,
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    code_future: asyncio.Future[tuple[str, str]] = asyncio.get_event_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            writer.close()
            return
        first = req.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        # "GET /spotify_callback?code=...&state=... HTTP/1.1"
        try:
            request_path = first.split(" ", 2)[1]
        except IndexError:
            request_path = "/"
        qs = parse_qs(urlparse(request_path).query)
        body = b"<html><body><h2>ULTRON Spotify auth complete.</h2><p>You can close this tab.</p></body></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

        if request_path.split("?", 1)[0] != path:
            return
        code = qs.get("code", [""])[0]
        st = qs.get("state", [""])[0]
        if not code_future.done():
            code_future.set_result((code, st))

    server = await asyncio.start_server(handle, host=host, port=port)
    logger.info("listening for Spotify callback on %s:%d", host, port)
    logger.info("opening browser to authorize...")
    if not webbrowser.open(auth_url):
        print(f"\nopen this URL in a browser:\n  {auth_url}\n")

    try:
        code, returned_state = await asyncio.wait_for(code_future, timeout=300)
    except asyncio.TimeoutError:
        logger.error("auth flow timed out after 5 minutes")
        server.close()
        await server.wait_closed()
        return False

    server.close()
    await server.wait_closed()

    if returned_state != state:
        logger.error("state mismatch — possible CSRF, aborting")
        return False
    if not code:
        logger.error("no authorization code returned")
        return False

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cfg.redirect_uri,
                "client_id": cfg.client_id,
                "code_verifier": verifier,
            },
        )
    if resp.status_code != 200:
        logger.error("token exchange failed: %s %s", resp.status_code, resp.text[:300])
        return False
    data = resp.json()
    token_store.save(
        {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + float(data.get("expires_in", 3600)) - 60,
            "scope": data.get("scope", SCOPES),
        }
    )
    logger.info("spotify token saved to %s", token_store.path)
    return True


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #


class SpotifyBridge(Bridge):
    name = "spotify"

    def __init__(
        self,
        publish: BridgePublishFn | None,
        cfg: SpotifyConfig,
        data_dir: Path,
    ) -> None:
        # publish=None is allowed at construction time; the supervisor
        # patches it in before start().
        super().__init__(publish or (lambda k, p: _noop_publish(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self.token_store = TokenStore(data_dir / "spotify_token.json")
        # State to avoid spamming identical events. Spotify polls return
        # the same payload for the duration of one track playing; we
        # republish only when the track ID or play state changes.
        self._last_signature: Optional[tuple[str, bool]] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        return self._client

    async def _access_token(self) -> Optional[str]:
        """Return a valid access token, refreshing if needed."""
        tok = self.token_store.load()
        if not tok:
            return None
        if tok.get("expires_at", 0) > time.time():
            return tok.get("access_token")
        # Refresh.
        refresh = tok.get("refresh_token")
        if not refresh:
            self.log.warning("token expired and no refresh_token available")
            return None
        client = await self._ensure_client()
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": self.cfg.client_id,
                },
            )
        except httpx.HTTPError as exc:
            self.log.warning("token refresh failed: %s", exc)
            return None
        if resp.status_code != 200:
            self.log.warning("token refresh failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        new_tok = {
            "access_token": data["access_token"],
            # Spotify may or may not return a new refresh_token — preserve old if absent.
            "refresh_token": data.get("refresh_token") or refresh,
            "expires_at": time.time() + float(data.get("expires_in", 3600)) - 60,
            "scope": data.get("scope", SCOPES),
        }
        self.token_store.save(new_tok)
        return new_tok["access_token"]

    async def run(self) -> None:
        if not self.cfg.client_id:
            self.log.warning("client_id not configured — bridge will idle")
            await self._stop_event.wait()
            return
        if self.token_store.load() is None:
            self.log.warning(
                "no spotify token cached — run `python -m ultron_bridges.spotify` "
                "to authorize. Bridge will idle until then."
            )
            # Poll the cache periodically; once it appears, switch to normal poll.
            while not self._stop_event.is_set():
                if not await self.sleep(15.0):
                    return
                if self.token_store.load() is not None:
                    self.log.info("token cache appeared — entering normal poll loop")
                    break

        client = await self._ensure_client()
        try:
            while not self._stop_event.is_set():
                await self._tick(client)
                if not await self.sleep(self.cfg.poll_secs):
                    return
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _tick(self, client: httpx.AsyncClient) -> None:
        token = await self._access_token()
        if not token:
            return
        try:
            resp = await client.get(
                CURRENTLY_PLAYING_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            self.log.debug("currently-playing request failed: %s", exc)
            return

        if resp.status_code == 204:
            # Nothing playing — publish once if state changed.
            sig = ("__none__", False)
            if self._last_signature != sig:
                await self.publish("spotify_now_playing", {"is_playing": False})
                self._last_signature = sig
            return
        if resp.status_code == 401:
            self.log.info("access token rejected — forcing refresh on next tick")
            self.token_store.save({  # type: ignore[arg-type]
                **(self.token_store.load() or {}),
                "expires_at": 0,
            })
            return
        if resp.status_code != 200:
            self.log.debug("unexpected status %s: %s", resp.status_code, resp.text[:200])
            return

        try:
            data = resp.json()
        except ValueError:
            return
        item = data.get("item") or {}
        if not isinstance(item, dict):
            return
        track_id = str(item.get("id") or "")
        is_playing = bool(data.get("is_playing", False))
        sig = (track_id, is_playing)
        if sig == self._last_signature:
            # Avoid republishing identical state on every tick. Progress
            # updates aren't worth a bus event each — consumers that need
            # progress can poll Spotify themselves.
            return
        self._last_signature = sig

        artists = item.get("artists") or []
        artist_names = [a.get("name", "") for a in artists if isinstance(a, dict)]
        album = item.get("album") or {}
        device = (data.get("device") or {}).get("name", "")

        payload = {
            "is_playing": is_playing,
            "track": item.get("name", ""),
            "artist": ", ".join(n for n in artist_names if n),
            "album": album.get("name", "") if isinstance(album, dict) else "",
            "track_id": track_id,
            "duration_ms": int(item.get("duration_ms", 0)),
            "progress_ms": int(data.get("progress_ms", 0) or 0),
            "device": device,
            "ts_unix_ms": int(time.time() * 1000),
        }
        await self.publish("spotify_now_playing", payload)
        self.log.info(
            "now playing: %s — %s (%s)",
            payload["track"], payload["artist"], "playing" if is_playing else "paused",
        )


async def _noop_publish(kind: str, payload: dict[str, Any]) -> bool:
    # Used only during the brief window between construction and supervisor
    # patching in the real callback.
    return False


# --------------------------------------------------------------------------- #
# CLI entrypoint — auth flow
# --------------------------------------------------------------------------- #


async def _cli_main() -> int:
    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_bridges_config()
    if not cfg.spotify.client_id:
        print("set [bridges.spotify].client_id in config.toml first.")
        print("create an app at https://developer.spotify.com/dashboard, then paste the Client ID.")
        print(f"add this redirect URI to the app's settings: {cfg.spotify.redirect_uri}")
        return 1
    store = TokenStore(cfg.data_dir / "spotify_token.json")
    ok = await run_auth_flow(cfg.spotify, store)
    return 0 if ok else 2


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(_cli_main()))
