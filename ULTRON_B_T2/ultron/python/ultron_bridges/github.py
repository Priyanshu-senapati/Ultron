"""GitHub activity bridge.

Polls two endpoints with ETag conditional requests so quiet periods cost
zero rate-limit:

  GET /users/{user}/events        — recent commits/PRs/issues by the user
  GET /notifications              — unread notifications across all repos

Publishes a single `gh_activity` event combining both feeds. We
republish only when at least one of the two ETags changes (Spotify-style
delta-based publishing — saves bus volume).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from .base import Bridge, BridgePublishFn
from .config import GithubConfig

logger = logging.getLogger("ultron.bridges.github")

API = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


class GithubBridge(Bridge):
    name = "github"

    def __init__(self, publish: BridgePublishFn | None, cfg: GithubConfig) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self._etag_events: Optional[str] = None
        self._etag_notifs: Optional[str] = None
        self._last_events: list[dict[str, Any]] = []
        self._last_notifs: list[dict[str, Any]] = []
        self._client: Optional[httpx.AsyncClient] = None

    async def run(self) -> None:
        if not self.cfg.token or not self.cfg.username:
            self.log.warning(
                "github.token or github.username not set — bridge will idle"
            )
            await self._stop_event.wait()
            return

        self._client = httpx.AsyncClient(
            timeout=15,
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {self.cfg.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "ULTRON-bridges/1.0",
            },
        )
        try:
            while not self._stop_event.is_set():
                await self._tick()
                if not await self.sleep(self.cfg.poll_secs):
                    return
        finally:
            await self._client.aclose()
            self._client = None

    async def _tick(self) -> None:
        client = self._client
        if client is None:
            return

        changed = False

        # ---- /users/{user}/events ----
        try:
            headers: dict[str, str] = {}
            if self._etag_events:
                headers["If-None-Match"] = self._etag_events
            resp = await client.get(
                f"{API}/users/{self.cfg.username}/events",
                headers=headers,
                params={"per_page": 10},
            )
            if resp.status_code == 200:
                self._etag_events = resp.headers.get("ETag")
                try:
                    self._last_events = _summarize_events(resp.json())
                except ValueError:
                    pass
                changed = True
            elif resp.status_code == 304:
                pass  # no change
            elif resp.status_code in (401, 403):
                self.log.warning("github auth/rate-limit: %s %s", resp.status_code, resp.text[:200])
            else:
                self.log.debug("events status %s", resp.status_code)
        except httpx.HTTPError as exc:
            self.log.debug("events request failed: %s", exc)

        # ---- /notifications ----
        try:
            headers = {}
            if self._etag_notifs:
                headers["If-None-Match"] = self._etag_notifs
            resp = await client.get(
                f"{API}/notifications",
                headers=headers,
                params={"all": "false", "per_page": 10},
            )
            if resp.status_code == 200:
                self._etag_notifs = resp.headers.get("ETag")
                try:
                    self._last_notifs = _summarize_notifs(resp.json())
                except ValueError:
                    pass
                changed = True
            elif resp.status_code == 304:
                pass
            elif resp.status_code in (401, 403):
                self.log.warning("github auth/rate-limit: %s %s", resp.status_code, resp.text[:200])
            else:
                self.log.debug("notifications status %s", resp.status_code)
        except httpx.HTTPError as exc:
            self.log.debug("notifications request failed: %s", exc)

        if not changed:
            return

        payload = {
            "events": self._last_events,
            "notifications": self._last_notifs,
            "unread_count": len(self._last_notifs),
            "ts_unix_ms": int(time.time() * 1000),
        }
        await self.publish("gh_activity", payload)
        self.log.info(
            "gh_activity: %d events, %d unread notifications",
            len(self._last_events), len(self._last_notifs),
        )


# --------------------------------------------------------------------------- #
# Summarizers — keep payloads small so we don't bloat the WS bus.
# --------------------------------------------------------------------------- #


def _summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events[:10]:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type", "")
        repo = (ev.get("repo") or {}).get("name", "")
        payload = ev.get("payload") or {}
        summary = ""
        if kind == "PushEvent":
            commits = payload.get("commits") or []
            top = commits[0].get("message", "").split("\n", 1)[0] if commits else ""
            summary = f"push: {top}" if top else "push"
        elif kind == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            summary = f"PR {payload.get('action', '')}: {pr.get('title', '')}"
        elif kind == "IssuesEvent":
            issue = payload.get("issue") or {}
            summary = f"issue {payload.get('action', '')}: {issue.get('title', '')}"
        elif kind == "IssueCommentEvent":
            issue = payload.get("issue") or {}
            summary = f"comment on: {issue.get('title', '')}"
        elif kind == "WatchEvent":
            summary = "starred"
        elif kind == "CreateEvent":
            summary = f"created {payload.get('ref_type', '')} {payload.get('ref', '')}"
        else:
            summary = kind
        out.append({
            "kind": kind,
            "repo": repo,
            "summary": summary[:200],
            "created_at": ev.get("created_at", ""),
        })
    return out


def _summarize_notifs(notifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in notifs[:10]:
        if not isinstance(n, dict):
            continue
        subject = n.get("subject") or {}
        out.append({
            "repo": (n.get("repository") or {}).get("full_name", ""),
            "title": subject.get("title", ""),
            "type": subject.get("type", ""),
            "reason": n.get("reason", ""),
            "updated_at": n.get("updated_at", ""),
        })
    return out


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False
