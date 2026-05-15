"""KGService — WS owner of the knowledge graph.

Subscribes:
  - ``kg_entity_add_request``     — payload: {kind, name, attrs?}
  - ``kg_edge_add_request``       — payload: {src_id|src_name, dst_id|dst_name, kind, attrs?}
  - ``kg_entity_delete_request``  — payload: {id}
  - ``kg_edge_delete_request``    — payload: {id}
  - ``kg_query_request``          — payload: {kind, ...}

Publishes:
  - ``kg_entity_added`` / ``kg_edge_added``
  - ``kg_query_result``
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import KnowledgeGraphConfig
from .graph import KnowledgeGraph
from .store import KGStore

logger = logging.getLogger("ultron.kg.service")


class KGService:
    def __init__(self, config: KnowledgeGraphConfig) -> None:
        self._cfg = config
        self._store = KGStore(config)
        self._graph = KnowledgeGraph(self._store, config)
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()

    @property
    def store(self) -> KGStore:
        return self._store

    @property
    def graph(self) -> KnowledgeGraph:
        return self._graph

    # ── Write API ──────────────────────────────────────────────────────

    async def add_entity(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            eid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.upsert_entity(
                    kind=str(payload["kind"]),
                    name=str(payload["name"]),
                    attrs=payload.get("attrs") or {},
                ),
            )
        ent = self._store.get_entity(eid) or {"id": eid}
        result = {"entity": ent}
        if self._bridge is not None:
            await self._bridge.publish("kg_entity_added", result)
        return result

    async def add_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            src_id = await loop.run_in_executor(
                None, lambda: self._resolve_id(payload, "src")
            )
            dst_id = await loop.run_in_executor(
                None, lambda: self._resolve_id(payload, "dst")
            )
            edge_id = await loop.run_in_executor(
                None, lambda: self._store.upsert_edge(
                    src_id=src_id, dst_id=dst_id,
                    kind=str(payload["kind"]),
                    attrs=payload.get("attrs") or {},
                ),
            )
        result = {"edge": {
            "id": edge_id, "src_id": src_id, "dst_id": dst_id,
            "kind": payload["kind"], "attrs": payload.get("attrs") or {},
        }}
        if self._bridge is not None:
            await self._bridge.publish("kg_edge_added", result)
        return result

    def _resolve_id(self, payload: dict[str, Any], prefix: str) -> int:
        idk = f"{prefix}_id"
        if idk in payload and payload[idk] is not None:
            return int(payload[idk])
        name_key = f"{prefix}_name"
        kind_key = f"{prefix}_kind"
        if name_key not in payload:
            raise ValueError(f"{prefix}_id or {prefix}_name required")
        ent = self._store.find_entity(
            kind=payload.get(kind_key), name=str(payload[name_key]),
        )
        if not ent:
            # Auto-create using the supplied kind, defaulting to 'concept'.
            return self._store.upsert_entity(
                kind=str(payload.get(kind_key) or "concept"),
                name=str(payload[name_key]),
            )
        return int(ent["id"])

    async def delete(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if kind == "entity":
            ok = await loop.run_in_executor(None, lambda: self._store.delete_entity(int(payload["id"])))
        elif kind == "edge":
            ok = await loop.run_in_executor(None, lambda: self._store.delete_edge(int(payload["id"])))
        else:
            ok = False
        return {"kind": kind, "id": payload.get("id"), "deleted": ok}

    # ── Read API ───────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "stats"))
        loop = asyncio.get_running_loop()
        if kind == "stats":
            result = {"kind": kind, "stats": await loop.run_in_executor(
                None, lambda: self._graph.stats()
            )}
        elif kind == "search_entities":
            rows = await loop.run_in_executor(None, lambda: self._store.search_entities(
                like=str(payload.get("like", "")),
                kind=payload.get("entity_kind"),
                limit=int(payload.get("limit", 50)),
            ))
            result = {"kind": kind, "rows": rows}
        elif kind == "list_entities":
            rows = await loop.run_in_executor(None, lambda: self._store.list_entities(
                kind=payload.get("entity_kind"),
                limit=int(payload.get("limit", 200)),
            ))
            result = {"kind": kind, "rows": rows}
        elif kind == "find_entity":
            result = {"kind": kind, "entity": await loop.run_in_executor(
                None, lambda: self._store.find_entity(
                    kind=payload.get("entity_kind"),
                    name=str(payload.get("name", "")),
                )
            )}
        elif kind == "neighbors":
            result = {"kind": kind, "result": await loop.run_in_executor(
                None, lambda: self._graph.neighbors(
                    int(payload["entity_id"]),
                    direction=str(payload.get("direction", "both")),
                ),
            )}
        elif kind == "egonet":
            result = {"kind": kind, "result": await loop.run_in_executor(
                None, lambda: self._graph.egonet(
                    int(payload["entity_id"]),
                    radius=payload.get("radius"),
                    limit=int(payload.get("limit", 100)),
                ),
            )}
        elif kind == "shortest_path":
            result = {"kind": kind, "result": await loop.run_in_executor(
                None, lambda: self._graph.shortest_path(
                    int(payload["src"]), int(payload["dst"]),
                ),
            )}
        elif kind == "top_entities":
            rows = await loop.run_in_executor(None, lambda: self._graph.top_entities(
                limit=int(payload.get("limit", 10))
            ))
            result = {"kind": kind, "rows": rows}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("kg_query_result", result)
        return result

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start kg service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "kg_entity_add_request",
                "kg_edge_add_request",
                "kg_entity_delete_request",
                "kg_edge_delete_request",
                "kg_query_request",
            ],
            role="knowledge-graph",
        )
        logger.info("KGService starting — db=%s", self._cfg.db_path)
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "kg_entity_add_request":
                await self.add_entity(payload)
            elif kind == "kg_edge_add_request":
                await self.add_edge(payload)
            elif kind == "kg_entity_delete_request":
                await self.delete("entity", payload)
            elif kind == "kg_edge_delete_request":
                await self.delete("edge", payload)
            elif kind == "kg_query_request":
                await self.query(payload)
        except Exception:  # noqa: BLE001
            logger.exception("handler failed for kind=%s", kind)
