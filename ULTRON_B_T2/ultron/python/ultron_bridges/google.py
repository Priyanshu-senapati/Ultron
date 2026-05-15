"""Google Calendar + Gmail bridge.

Uses Google's OAuth2 installed-app flow (PKCE optional, refresh tokens).
The user creates an OAuth Client ID (Desktop app) in Google Cloud Console
and enables the Calendar + Gmail APIs. After dropping client_id /
client_secret into config.toml, they run:

    python -m ultron_bridges.google

…which opens a browser, catches the redirect on 127.0.0.1, exchanges
the code, and persists access + refresh tokens.

Polls two endpoints at independent cadences:

  Calendar: next 24h of events from `primary` calendar, every 60s
  Gmail:    unread thread count + last 5 unread subjects, every 120s

Events published:
  calendar_upcoming → { events: [...], ts_unix_ms }
  gmail_unread      → { count, messages: [...], ts_unix_ms }
"""
from __future__ import annotations

import asyncio
import base64
import datetime
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
from .config import GoogleConfig, load_bridges_config

logger = logging.getLogger("ultron.bridges.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/gmail.readonly"
)

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8767
REDIRECT_PATH = "/google_callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Auth flow (CLI: `python -m ultron_bridges.google`)
# --------------------------------------------------------------------------- #


async def run_auth_flow(cfg: GoogleConfig, store: TokenStore) -> bool:
    if not cfg.client_id or not cfg.client_secret:
        logger.error("google.client_id / client_secret missing in config.toml")
        return False

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # force refresh_token issuance on re-auth
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
        try:
            request_path = first.split(" ", 2)[1]
        except IndexError:
            request_path = "/"
        qs = parse_qs(urlparse(request_path).query)
        body = b"<html><body><h2>ULTRON Google auth complete.</h2><p>You can close this tab.</p></body></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        if request_path.split("?", 1)[0] != REDIRECT_PATH:
            return
        code = qs.get("code", [""])[0]
        st = qs.get("state", [""])[0]
        if not code_future.done():
            code_future.set_result((code, st))

    server = await asyncio.start_server(handle, host=REDIRECT_HOST, port=REDIRECT_PORT)
    logger.info("listening for Google callback on %s", REDIRECT_URI)
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

    if returned_state != state or not code:
        logger.error("state mismatch or empty code")
        return False

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        logger.error("token exchange failed: %s %s", resp.status_code, resp.text[:300])
        return False
    data = resp.json()
    store.save({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": time.time() + float(data.get("expires_in", 3600)) - 60,
        "scope": data.get("scope", SCOPES),
    })
    logger.info("google token saved to %s", store.path)
    return True


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #


class GoogleBridge(Bridge):
    name = "google"

    def __init__(self, publish: BridgePublishFn | None, cfg: GoogleConfig) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self.store = TokenStore(Path(cfg.token_cache))
        self._client: Optional[httpx.AsyncClient] = None
        self._calendar_last_sig: Optional[str] = None
        self._gmail_last_sig: Optional[str] = None
        self._calendar_next_at: float = 0.0
        self._gmail_next_at: float = 0.0

    async def run(self) -> None:
        if not self.cfg.client_id or not self.cfg.client_secret:
            self.log.warning("client_id/secret not configured — bridge will idle")
            await self._stop_event.wait()
            return
        if self.store.load() is None:
            self.log.warning(
                "no google token cached — run `python -m ultron_bridges.google` "
                "to authorize. Bridge will idle until then."
            )
            while not self._stop_event.is_set():
                if not await self.sleep(15.0):
                    return
                if self.store.load() is not None:
                    self.log.info("token cache appeared — entering normal poll loop")
                    break

        self._client = httpx.AsyncClient(timeout=20)
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= self._calendar_next_at:
                    await self._poll_calendar()
                    self._calendar_next_at = now + self.cfg.calendar_poll_secs
                if now >= self._gmail_next_at:
                    await self._poll_gmail()
                    self._gmail_next_at = now + self.cfg.gmail_poll_secs
                # Sleep until the next poll instant or 5s — whichever is sooner.
                next_due = min(self._calendar_next_at, self._gmail_next_at)
                delay = max(1.0, min(5.0, next_due - time.monotonic()))
                if not await self.sleep(delay):
                    return
        finally:
            await self._client.aclose()
            self._client = None

    async def _access_token(self) -> Optional[str]:
        tok = self.store.load()
        if not tok:
            return None
        if tok.get("expires_at", 0) > time.time():
            return tok.get("access_token")
        refresh = tok.get("refresh_token")
        if not refresh:
            self.log.warning("token expired and no refresh_token cached")
            return None
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.cfg.client_id,
                    "client_secret": self.cfg.client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            self.log.warning("refresh failed: %s", exc)
            return None
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code != 200:
            self.log.warning("refresh failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        new_tok = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh,
            "expires_at": time.time() + float(data.get("expires_in", 3600)) - 60,
            "scope": data.get("scope", SCOPES),
        }
        self.store.save(new_tok)
        return new_tok["access_token"]

    async def _poll_calendar(self) -> None:
        client = self._client
        token = await self._access_token()
        if client is None or not token:
            return
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        time_max = now_utc + datetime.timedelta(hours=24)
        try:
            resp = await client.get(
                CALENDAR_EVENTS_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": now_utc.isoformat().replace("+00:00", "Z"),
                    "timeMax": time_max.isoformat().replace("+00:00", "Z"),
                    "maxResults": 10,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
        except httpx.HTTPError as exc:
            self.log.debug("calendar request failed: %s", exc)
            return
        if resp.status_code != 200:
            self.log.debug("calendar status %s: %s", resp.status_code, resp.text[:200])
            return
        try:
            data = resp.json()
        except ValueError:
            return
        events = data.get("items") or []
        summarized = [_summarize_event(e) for e in events if isinstance(e, dict)]
        sig = json.dumps([(e["start"], e["summary"]) for e in summarized], sort_keys=True)
        if sig == self._calendar_last_sig:
            return
        self._calendar_last_sig = sig
        await self.publish(
            "calendar_upcoming",
            {"events": summarized, "ts_unix_ms": int(time.time() * 1000)},
        )
        self.log.info("calendar_upcoming: %d events in next 24h", len(summarized))

    async def _poll_gmail(self) -> None:
        client = self._client
        token = await self._access_token()
        if client is None or not token:
            return
        # First: how many unread.
        try:
            resp = await client.get(
                GMAIL_MESSAGES_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"q": "is:unread in:inbox", "maxResults": 5},
            )
        except httpx.HTTPError as exc:
            self.log.debug("gmail list failed: %s", exc)
            return
        if resp.status_code != 200:
            self.log.debug("gmail list status %s: %s", resp.status_code, resp.text[:200])
            return
        try:
            data = resp.json()
        except ValueError:
            return
        messages = data.get("messages") or []
        unread_count = int(data.get("resultSizeEstimate", len(messages)) or len(messages))

        # Fetch each message's subject + from (cheap: format=metadata).
        summaries: list[dict[str, Any]] = []
        for m in messages[:5]:
            mid = m.get("id")
            if not mid:
                continue
            try:
                mresp = await client.get(
                    f"{GMAIL_MESSAGES_URL}/{mid}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["Subject", "From"],
                    },
                )
            except httpx.HTTPError:
                continue
            if mresp.status_code != 200:
                continue
            try:
                md = mresp.json()
            except ValueError:
                continue
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in (md.get("payload") or {}).get("headers", [])
                if isinstance(h, dict)
            }
            summaries.append({
                "id": mid,
                "subject": headers.get("subject", "(no subject)"),
                "from": headers.get("from", ""),
                "snippet": (md.get("snippet") or "")[:160],
            })

        sig = json.dumps((unread_count, [(s["id"], s["subject"]) for s in summaries]), sort_keys=True)
        if sig == self._gmail_last_sig:
            return
        self._gmail_last_sig = sig
        await self.publish(
            "gmail_unread",
            {
                "count": unread_count,
                "messages": summaries,
                "ts_unix_ms": int(time.time() * 1000),
            },
        )
        self.log.info("gmail_unread: count=%d (%d sampled)", unread_count, len(summaries))


def _summarize_event(ev: dict[str, Any]) -> dict[str, Any]:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    return {
        "summary": ev.get("summary", "(no title)"),
        "location": ev.get("location", ""),
        "start": start.get("dateTime") or start.get("date", ""),
        "end": end.get("dateTime") or end.get("date", ""),
        "html_link": ev.get("htmlLink", ""),
    }


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #


async def _cli_main() -> int:
    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_bridges_config()
    if not cfg.google.client_id or not cfg.google.client_secret:
        print("set [bridges.google].client_id and client_secret in config.toml first.")
        print("create an OAuth client (type: Desktop app) at https://console.cloud.google.com/apis/credentials")
        print(f"add this redirect URI to the client: {REDIRECT_URI}")
        print("enable the Calendar API and Gmail API for the project too.")
        return 1
    store = TokenStore(Path(cfg.google.token_cache))
    ok = await run_auth_flow(cfg.google, store)
    return 0 if ok else 2


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(_cli_main()))
