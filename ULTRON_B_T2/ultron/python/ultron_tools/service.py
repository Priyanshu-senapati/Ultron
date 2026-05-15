"""ToolService — WS-facing owner of the registry + executor.

Subscribes to:
  - ``tool_call_request``    — C (LLM) wants to invoke a tool
  - ``tool_confirm_response`` — user has approved/rejected a pending call

Publishes:
  - ``tool_call_result``        — final outcome of a call
  - ``tool_confirm_required``   — call is parked until user approves
  - ``tool_call_audit``         — every terminal event (Z subscribes)
  - ``tool_catalog``            — emitted on bridge connect so C can refresh
                                  its tool list without restart
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import ToolsConfig
from .executor import ExecutionResult, ToolExecutor
from .registry import ToolRegistry

logger = logging.getLogger("ultron.tools.service")


class ToolService:
    def __init__(self, config: ToolsConfig, registry: ToolRegistry) -> None:
        self._cfg = config
        self._registry = registry
        self._executor = ToolExecutor(registry, config)
        self._bridge: Optional[UltronBridge] = None
        # Background tasks for tool_call_request dispatch. Holding refs
        # keeps the tasks alive and lets us clean them up on shutdown.
        self._inflight: set[asyncio.Task[Any]] = set()

    # ── Public Python API ───────────────────────────────────────────────

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        request_id: Optional[str] = None,
        confirm_token: Optional[str] = None,
    ) -> ExecutionResult:
        result = await self._executor.execute(
            name=name,
            args=args,
            request_id=request_id,
            confirm_token=confirm_token,
        )
        await self._publish_result(result)
        return result

    def descriptors(self) -> list[dict[str, Any]]:
        """Compact tool descriptors for C's system prompt."""
        return [t.to_descriptor() for t in self._registry.list()]

    # ── WS subscriber ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start tool service")
        from . import bridge_rpc
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["tool_call_request", "tool_confirm_response",
                          *bridge_rpc.RESULT_KINDS],
            role="tools",
        )
        bridge_rpc.set_bridge(self._bridge)
        logger.info(
            "ToolService starting — %d tools registered: %s",
            len(self._registry),
            self._registry.names(),
        )
        # Best-effort: publish the catalogue once on startup. If the bridge
        # isn't connected yet the publish silently no-ops; C will refresh
        # next time we receive a request.
        try:
            await self._publish_catalog()
        except Exception:  # noqa: BLE001
            pass
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        from . import bridge_rpc
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        if kind == "tool_call_request":
            # Dispatch the request in a background task — the handler may
            # await a *_query_result that arrives via this same loop. Doing
            # it inline would deadlock the bridge's recv coroutine.
            task = asyncio.create_task(self._on_request(payload))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        elif kind == "tool_confirm_response":
            await self._on_confirm_response(payload)
        elif kind in bridge_rpc.RESULT_KINDS:
            bridge_rpc.deliver_result(kind, payload)

    async def _on_request(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name", ""))
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        request_id = str(payload.get("request_id", "")) or None
        confirm_token = payload.get("confirm_token")
        if confirm_token is not None:
            confirm_token = str(confirm_token)
        result = await self._executor.execute(
            name=name,
            args=args,
            request_id=request_id,
            confirm_token=confirm_token,
        )
        await self._publish_result(result)

    async def _on_confirm_response(self, payload: dict[str, Any]) -> None:
        """User answered a pending confirm. ``approved`` true → C re-issues
        the call with the confirm_token. ``approved`` false → we cancel."""
        approved = bool(payload.get("approved", False))
        token = str(payload.get("confirm_token", ""))
        if not token:
            return
        if not approved:
            cancelled = self._executor.cancel_pending(token)
            if self._bridge is not None and cancelled:
                await self._bridge.publish(
                    "tool_call_audit",
                    {
                        "kind": "confirm_rejected",
                        "confirm_token": token,
                        "ts_unix_ms": int(time.time() * 1000),
                    },
                )
        # If approved, the executor still holds the pending entry; the
        # client (C) is responsible for republishing the tool_call_request
        # with that token in the payload. We don't auto-execute here
        # because we don't have the original payload anymore — keeping
        # it would mean the executor stores user-provided args after a
        # privacy check from a different process.

    # ── Publish helpers ─────────────────────────────────────────────────

    async def _publish_result(self, result: ExecutionResult) -> None:
        if self._bridge is None:
            return
        if result.pending_confirm:
            await self._bridge.publish(
                "tool_confirm_required",
                {
                    "request_id": result.request_id,
                    "name": result.name,
                    "confirm_token": result.confirm_token,
                    "confirm_reason": result.confirm_reason,
                    "expires_at_unix": result.confirm_expires_at,
                },
            )
        else:
            await self._bridge.publish(
                "tool_call_result",
                result.to_dict(),
            )
        # Z (quantum log) subscribes to tool_call_audit specifically.
        try:
            await self._bridge.publish("tool_call_audit", result.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool_call_audit publish dropped: %s", exc)

    async def _publish_catalog(self) -> None:
        if self._bridge is None:
            return
        try:
            await self._bridge.publish(
                "tool_catalog",
                {
                    "ts_unix_ms": int(time.time() * 1000),
                    "tools": self.descriptors(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool_catalog publish dropped: %s", exc)
