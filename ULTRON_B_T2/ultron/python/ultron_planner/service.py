"""PlannerService — WS owner of goals/outcomes/blocks/events.

Subscribes:
  - ``goal_set_request``           — payload: Goal dict (id optional)
  - ``outcome_set_request``        — payload: Outcome dict
  - ``block_schedule_request``     — payload: Block dict
  - ``event_schedule_request``     — payload: Event dict
  - ``plan_query_request``         — payload: ``{kind, ...}``
  - ``event_delete_request`` / ``block_delete_request`` / ``goal_delete_request``

Publishes:
  - ``goal_set`` / ``outcome_set`` / ``block_scheduled`` / ``event_scheduled``
  - ``plan_query_result``
  - ``upcoming_event``   — emitted ``upcoming_horizon_seconds`` before fire
  - ``alarm_fire``       — emitted at fire time
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import PlannerConfig
from .models import Block, Event, Goal, Outcome
from .planner import Planner
from .store import PlannerStore

logger = logging.getLogger("ultron.planner.service")


class PlannerService:
    def __init__(self, config: PlannerConfig) -> None:
        self._cfg = config
        self._store = PlannerStore(config)
        self._planner = Planner(self._store, config)
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()
        self._tick_task: Optional[asyncio.Task[None]] = None
        # Event ids we've already heralded with upcoming_event so we
        # don't spam the bus every tick.
        self._heralded: set[int] = set()
        self._stop = asyncio.Event()

    @property
    def planner(self) -> Planner:
        return self._planner

    @property
    def store(self) -> PlannerStore:
        return self._store

    # ── Write API ──────────────────────────────────────────────────────

    async def set_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            g = Goal(
                title=str(payload["title"]),
                dream_kind=str(payload.get("dream_kind") or "personal"),
                target_date=payload.get("target_date"),
                status=str(payload.get("status") or "active"),
                note=str(payload.get("note") or ""),
                id=payload.get("id"),
            )
            gid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.upsert_goal(g)
            )
            g.id = gid
            result = {"goal": g.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("goal_set", result)
        return result

    async def set_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            o = Outcome(
                goal_id=int(payload["goal_id"]),
                title=str(payload["title"]),
                status=str(payload.get("status") or "pending"),
                weight=float(payload.get("weight") or 1.0),
                note=str(payload.get("note") or ""),
                id=payload.get("id"),
            )
            oid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.upsert_outcome(o)
            )
            o.id = oid
            result = {"outcome": o.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("outcome_set", result)
        return result

    async def schedule_block(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            b = Block(
                ts_start=float(payload["ts_start"]),
                ts_end=float(payload["ts_end"]),
                title=str(payload["title"]),
                kind=str(payload.get("kind") or "focus"),
                outcome_id=(int(payload["outcome_id"]) if payload.get("outcome_id") is not None else None),
                note=str(payload.get("note") or ""),
                id=payload.get("id"),
            )
            bid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.schedule_block(b)
            )
            b.id = bid
            result = {"block": b.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("block_scheduled", result)
        return result

    async def schedule_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            e = Event(
                ts=float(payload["ts"]),
                title=str(payload["title"]),
                kind=str(payload.get("kind") or "alarm"),
                payload=str(payload.get("payload") or ""),
            )
            eid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.schedule_event(e)
            )
            e.id = eid
            result = {"event": e.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("event_scheduled", result)
        return result

    # ── Query API ──────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "today_summary"))
        loop = asyncio.get_running_loop()
        if kind == "today_summary":
            result = {"kind": kind, "summary": await loop.run_in_executor(
                None, lambda: self._planner.today_summary()
            )}
        elif kind == "upcoming_blocks":
            rows = await loop.run_in_executor(
                None, lambda: self._planner.upcoming_blocks(
                    horizon_seconds=int(payload.get("horizon_seconds", 86400)),
                    limit=int(payload.get("limit", 20)),
                ),
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "upcoming_events":
            rows = await loop.run_in_executor(
                None, lambda: self._planner.upcoming_events(
                    horizon_seconds=int(payload.get("horizon_seconds", 86400)),
                    limit=int(payload.get("limit", 20)),
                ),
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_goals":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_goals(status=payload.get("status"))
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_outcomes":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_outcomes(
                    goal_id=payload.get("goal_id"),
                    status=payload.get("status"),
                )
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "goal_progress":
            result = {"kind": kind, "progress": await loop.run_in_executor(
                None, lambda: self._planner.goal_progress(int(payload["goal_id"]))
            )}
        elif kind == "all_goal_progress":
            rows = await loop.run_in_executor(
                None, lambda: self._planner.all_goal_progress()
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "outcome_time_spent":
            result = {"kind": kind, "spent": await loop.run_in_executor(
                None, lambda: self._planner.outcome_time_spent(
                    int(payload["outcome_id"]), days=int(payload.get("days", 30))
                )
            )}
        elif kind == "list_blocks":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_blocks(
                    since_ts=payload.get("since_ts"),
                    until_ts=payload.get("until_ts"),
                    outcome_id=payload.get("outcome_id"),
                    limit=int(payload.get("limit", 100)),
                )
            )
            result = {"kind": kind, "rows": rows}
        elif kind == "list_events":
            rows = await loop.run_in_executor(
                None, lambda: self._store.list_events(
                    since_ts=payload.get("since_ts"),
                    until_ts=payload.get("until_ts"),
                    only_pending=bool(payload.get("only_pending", False)),
                    limit=int(payload.get("limit", 100)),
                )
            )
            result = {"kind": kind, "rows": rows}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("plan_query_result", result)
        return result

    # ── Delete API ─────────────────────────────────────────────────────

    async def delete(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if kind == "event":
            ok = await loop.run_in_executor(None, lambda: self._store.delete_event(int(payload["id"])))
        elif kind == "block":
            ok = await loop.run_in_executor(None, lambda: self._store.delete_block(int(payload["id"])))
        elif kind == "goal":
            ok = await loop.run_in_executor(None, lambda: self._store.delete_goal(int(payload["id"])))
        else:
            ok = False
        return {"kind": kind, "id": payload.get("id"), "deleted": ok}

    # ── Scheduler tick ─────────────────────────────────────────────────

    async def _scheduler_loop(self) -> None:
        logger.info("scheduler tick=%ss horizon=%ss", self._cfg.tick_seconds,
                    self._cfg.upcoming_horizon_seconds)
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick_once(self) -> None:
        if self._bridge is None:
            return
        now = time.time()
        loop = asyncio.get_running_loop()
        # 1) Fire events whose ts is in the past and haven't fired yet.
        due = await loop.run_in_executor(
            None, lambda: self._store.pending_events(until_ts=now)
        )
        for ev in due:
            await self._bridge.publish("alarm_fire", {"event": ev})
            await loop.run_in_executor(
                None, lambda eid=int(ev["id"]): self._store.mark_event_fired(eid, now)
            )
            self._heralded.discard(int(ev["id"]))
        # 2) Herald upcoming events within the horizon (once each).
        horizon_end = now + self._cfg.upcoming_horizon_seconds
        upcoming = await loop.run_in_executor(
            None, lambda: self._store.list_events(
                since_ts=now, until_ts=horizon_end, only_pending=True, limit=20,
            )
        )
        for ev in upcoming:
            eid = int(ev["id"])
            if eid in self._heralded:
                continue
            await self._bridge.publish("upcoming_event", {
                "event": ev,
                "in_seconds": int(ev["ts"] - now),
            })
            self._heralded.add(eid)

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start planner service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "goal_set_request", "outcome_set_request",
                "block_schedule_request", "event_schedule_request",
                "plan_query_request",
                "event_delete_request", "block_delete_request", "goal_delete_request",
            ],
            role="dream-scheduler",
        )
        self._tick_task = asyncio.create_task(self._scheduler_loop())
        logger.info("PlannerService starting — db=%s", self._cfg.db_path)
        try:
            await self._bridge.run_forever()
        finally:
            self._stop.set()
            if self._tick_task:
                self._tick_task.cancel()
                try:
                    await self._tick_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "goal_set_request":
                await self.set_goal(payload)
            elif kind == "outcome_set_request":
                await self.set_outcome(payload)
            elif kind == "block_schedule_request":
                await self.schedule_block(payload)
            elif kind == "event_schedule_request":
                await self.schedule_event(payload)
            elif kind == "plan_query_request":
                await self.query(payload)
            elif kind == "event_delete_request":
                await self.delete("event", payload)
            elif kind == "block_delete_request":
                await self.delete("block", payload)
            elif kind == "goal_delete_request":
                await self.delete("goal", payload)
        except Exception:  # noqa: BLE001
            logger.exception("handler failed for kind=%s", kind)
