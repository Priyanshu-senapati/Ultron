"""NetworkX-backed graph view over the SQLite store.

The store is the source of truth; ``KnowledgeGraph`` rebuilds the
in-memory ``nx.DiGraph`` from the store on demand. For a single-user
twin the graph stays tiny (hundreds → low-thousands of nodes), so a
full rebuild on every query is cheap and avoids cache-invalidation
bugs.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import networkx as nx

from .config import KnowledgeGraphConfig
from .store import KGStore

logger = logging.getLogger("ultron.kg.graph")


class KnowledgeGraph:
    def __init__(self, store: KGStore, config: KnowledgeGraphConfig) -> None:
        self._store = store
        self._cfg = config

    # ── Build ──────────────────────────────────────────────────────────

    def build(self) -> nx.DiGraph:
        g: nx.DiGraph = nx.DiGraph()
        for ent in self._store.list_entities(limit=self._cfg.max_query_rows):
            g.add_node(
                int(ent["id"]),
                kind=ent["kind"],
                name=ent["name"],
                attrs=ent["attrs"],
            )
        for edge in self._store.list_edges():
            if edge["src_id"] in g.nodes and edge["dst_id"] in g.nodes:
                g.add_edge(
                    int(edge["src_id"]),
                    int(edge["dst_id"]),
                    edge_id=edge["id"],
                    kind=edge["kind"],
                    attrs=edge["attrs"],
                )
        return g

    # ── Queries ────────────────────────────────────────────────────────

    def neighbors(self, eid: int, *, direction: str = "both") -> dict[str, Any]:
        """Direct neighbours of ``eid``. direction: out / in / both."""
        g = self.build()
        if eid not in g:
            return {"entity_id": eid, "found": False}
        out: list[dict[str, Any]] = []
        if direction in ("out", "both"):
            for _, dst, ed in g.out_edges(eid, data=True):
                node = g.nodes[dst]
                out.append({
                    "direction": "out",
                    "id": int(dst),
                    "kind": node.get("kind"),
                    "name": node.get("name"),
                    "edge_kind": ed.get("kind"),
                })
        if direction in ("in", "both"):
            for src, _, ed in g.in_edges(eid, data=True):
                node = g.nodes[src]
                out.append({
                    "direction": "in",
                    "id": int(src),
                    "kind": node.get("kind"),
                    "name": node.get("name"),
                    "edge_kind": ed.get("kind"),
                })
        return {"entity_id": eid, "found": True, "neighbors": out, "count": len(out)}

    def egonet(self, eid: int, *, radius: Optional[int] = None,
               limit: int = 100) -> dict[str, Any]:
        """k-hop neighbourhood as a node/edge list."""
        g = self.build()
        if eid not in g:
            return {"entity_id": eid, "found": False}
        r = max(1, radius or self._cfg.default_radius)
        ug = g.to_undirected(as_view=True)
        nodes: set[int] = {eid}
        frontier: set[int] = {eid}
        for _ in range(r):
            new_frontier: set[int] = set()
            for n in frontier:
                for nb in ug.neighbors(n):
                    if nb not in nodes:
                        new_frontier.add(nb)
            nodes |= new_frontier
            frontier = new_frontier
            if not frontier:
                break
        # Cap to ``limit`` nodes for safety.
        nodes_capped = set(list(nodes)[: max(1, min(limit, self._cfg.max_query_rows))])
        sub = g.subgraph(nodes_capped)
        node_list = [{
            "id": int(n),
            "kind": d.get("kind"),
            "name": d.get("name"),
        } for n, d in sub.nodes(data=True)]
        edge_list = [{
            "src_id": int(u), "dst_id": int(v),
            "kind": ed.get("kind"),
        } for u, v, ed in sub.edges(data=True)]
        return {
            "entity_id": eid,
            "found": True,
            "radius": r,
            "nodes": node_list,
            "edges": edge_list,
            "truncated": len(nodes) > len(nodes_capped),
        }

    def shortest_path(self, src: int, dst: int) -> dict[str, Any]:
        g = self.build()
        if src not in g or dst not in g:
            return {"src": src, "dst": dst, "found": False}
        ug = g.to_undirected(as_view=True)
        try:
            path = nx.shortest_path(ug, source=src, target=dst)
        except nx.NetworkXNoPath:
            return {"src": src, "dst": dst, "found": True, "path": [], "length": -1}
        return {
            "src": src,
            "dst": dst,
            "found": True,
            "path": [
                {"id": int(n), "name": g.nodes[n].get("name"), "kind": g.nodes[n].get("kind")}
                for n in path
            ],
            "length": len(path) - 1,
        }

    def top_entities(self, *, limit: int = 10) -> list[dict[str, Any]]:
        g = self.build()
        deg = sorted(g.degree, key=lambda kv: kv[1], reverse=True)[: max(1, limit)]
        out: list[dict[str, Any]] = []
        for nid, d in deg:
            node = g.nodes[nid]
            out.append({
                "id": int(nid),
                "name": node.get("name"),
                "kind": node.get("kind"),
                "degree": int(d),
            })
        return out

    def stats(self) -> dict[str, Any]:
        g = self.build()
        kind_count: dict[str, int] = {}
        for _, d in g.nodes(data=True):
            kind_count[d.get("kind", "?")] = kind_count.get(d.get("kind", "?"), 0) + 1
        edge_count: dict[str, int] = {}
        for _, _, ed in g.edges(data=True):
            edge_count[ed.get("kind", "?")] = edge_count.get(ed.get("kind", "?"), 0) + 1
        return {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "entity_kinds": sorted(
                ({"kind": k, "count": v} for k, v in kind_count.items()),
                key=lambda r: r["count"], reverse=True,
            ),
            "edge_kinds": sorted(
                ({"kind": k, "count": v} for k, v in edge_count.items()),
                key=lambda r: r["count"], reverse=True,
            ),
        }
