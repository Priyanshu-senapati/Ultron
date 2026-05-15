"""CodeService — WS-facing owner of the CodeIndex.

Subscribes:
  - ``code_index_rebuild_request`` — full or incremental rescan
  - ``code_query_request``         — runs a query (find_symbol, search,
                                     list_files, stats)
  - ``filesystem_change``          — heuristic re-index trigger from the
                                     existing bridges_service watcher
                                     (debounced via min-interval)

Publishes:
  - ``code_index_complete``  — payload: IndexStats
  - ``code_query_result``    — payload: rows + meta
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import CodeIntelConfig
from .index import CodeIndex

logger = logging.getLogger("ultron.code.service")


class CodeService:
    def __init__(self, config: CodeIntelConfig) -> None:
        self._cfg = config
        self._index = CodeIndex(config)
        self._bridge: Optional[UltronBridge] = None
        self._last_rescan_at: float = 0.0
        self._rescan_lock = asyncio.Lock()

    @property
    def index(self) -> CodeIndex:
        return self._index

    # ── Public Python API ───────────────────────────────────────────────

    async def rebuild(self, full: bool = False) -> dict[str, Any]:
        async with self._rescan_lock:
            stats = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._index.rebuild(full=full)
            )
            self._last_rescan_at = time.monotonic()
            if self._bridge is not None:
                await self._bridge.publish("code_index_complete", stats.as_dict())
            return stats.as_dict()

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "find_symbol"))
        if kind == "find_symbol":
            rows = self._index.find_symbol(
                name=str(payload.get("name", "")),
                kind=payload.get("symbol_kind"),
                limit=int(payload.get("limit", 50)),
            )
            result: dict[str, Any] = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "search_symbols":
            rows = self._index.search_symbols(
                like=str(payload.get("like", "")),
                limit=int(payload.get("limit", 50)),
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "list_files":
            rows = self._index.list_files(
                language=payload.get("language"),
                path_substring=payload.get("path_substring"),
                limit=int(payload.get("limit", 200)),
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "stats":
            result = {"kind": "stats", "stats": self._index.stats()}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("code_query_result", result)
        return result

    # ── WS subscriber ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start code service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "code_index_rebuild_request",
                "code_query_request",
                "filesystem_change",
            ],
            role="code-intel",
        )
        logger.info(
            "CodeService starting — db=%s roots=%s",
            self._cfg.db_path,
            [str(r) for r in self._cfg.roots],
        )
        # Boot rebuild runs *concurrently* with the bridge: queries that
        # arrive during the rebuild hit the existing on-disk index (which
        # is whatever the last run produced), so they're still usable.
        async def _boot_rebuild() -> None:
            try:
                stats = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._index.rebuild(full=False)
                )
                logger.info("startup index: %s", stats.as_dict())
                if self._bridge is not None:
                    try:
                        await self._bridge.publish("code_index_complete", stats.as_dict())
                    except Exception:  # noqa: BLE001
                        logger.exception("publish code_index_complete failed")
            except Exception:  # noqa: BLE001
                logger.exception("startup index failed — continuing")

        asyncio.create_task(_boot_rebuild())
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        if kind == "code_index_rebuild_request":
            full = bool(payload.get("full", False))
            await self.rebuild(full=full)
        elif kind == "code_query_request":
            await self.query(payload)
        elif kind == "filesystem_change":
            now = time.monotonic()
            if now - self._last_rescan_at < self._cfg.rescan_min_interval_seconds:
                return
            await self.rebuild(full=False)
