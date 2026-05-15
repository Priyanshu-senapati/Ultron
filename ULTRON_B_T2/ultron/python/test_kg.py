"""Tests for Module K (Knowledge Graph)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ultron_kg.config import KnowledgeGraphConfig
from ultron_kg.graph import KnowledgeGraph
from ultron_kg.store import KGStore


def _cfg(tmp_path: Path) -> KnowledgeGraphConfig:
    return KnowledgeGraphConfig(
        ws_url="ws://x", ws_token="t",
        db_path=tmp_path / "kg.db",
    )


@pytest.fixture
def store(tmp_path: Path) -> KGStore:
    return KGStore(_cfg(tmp_path))


@pytest.fixture
def kg(tmp_path: Path) -> tuple[KGStore, KnowledgeGraph]:
    cfg = _cfg(tmp_path)
    s = KGStore(cfg)
    return s, KnowledgeGraph(s, cfg)


# ── Store ─────────────────────────────────────────────────────────────


def test_upsert_entity_creates(store: KGStore) -> None:
    eid = store.upsert_entity(kind="person", name="Priyanshu", attrs={"role": "user"})
    assert eid > 0
    rows = store.list_entities()
    assert len(rows) == 1
    assert rows[0]["attrs"] == {"role": "user"}


def test_upsert_entity_dedupes_by_kind_name(store: KGStore) -> None:
    a = store.upsert_entity(kind="project", name="ULTRON")
    b = store.upsert_entity(kind="project", name="ULTRON", attrs={"phase": "3"})
    assert a == b
    rows = store.list_entities()
    assert len(rows) == 1
    assert rows[0]["attrs"] == {"phase": "3"}


def test_entity_requires_kind_and_name(store: KGStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_entity(kind="", name="x")
    with pytest.raises(ValueError):
        store.upsert_entity(kind="person", name="")


def test_search_entities_like(store: KGStore) -> None:
    for n in ("ULTRON", "Ulterior", "Vega"):
        store.upsert_entity(kind="project", name=n)
    rows = store.search_entities(like="Ult")
    names = {r["name"] for r in rows}
    assert {"ULTRON", "Ulterior"} <= names
    assert "Vega" not in names


def test_find_entity_kind_filter(store: KGStore) -> None:
    store.upsert_entity(kind="person", name="Alice")
    store.upsert_entity(kind="project", name="Alice")
    ent = store.find_entity(kind="project", name="Alice")
    assert ent is not None
    assert ent["kind"] == "project"


def test_upsert_edge(store: KGStore) -> None:
    a = store.upsert_entity(kind="person", name="P")
    b = store.upsert_entity(kind="project", name="ULTRON")
    eid = store.upsert_edge(src_id=a, dst_id=b, kind="works_on")
    assert eid > 0
    edges = store.list_edges()
    assert len(edges) == 1
    assert edges[0]["kind"] == "works_on"


def test_edge_dedupes_same_triple(store: KGStore) -> None:
    a = store.upsert_entity(kind="person", name="P")
    b = store.upsert_entity(kind="project", name="X")
    e1 = store.upsert_edge(src_id=a, dst_id=b, kind="works_on")
    e2 = store.upsert_edge(src_id=a, dst_id=b, kind="works_on", attrs={"since": "2026"})
    assert e1 == e2


def test_edge_self_loop_rejected(store: KGStore) -> None:
    a = store.upsert_entity(kind="person", name="P")
    with pytest.raises(ValueError):
        store.upsert_edge(src_id=a, dst_id=a, kind="knows")


def test_edge_requires_real_nodes(store: KGStore) -> None:
    a = store.upsert_entity(kind="person", name="P")
    with pytest.raises(ValueError):
        store.upsert_edge(src_id=a, dst_id=999, kind="knows")


def test_delete_entity_cascades_edges(store: KGStore) -> None:
    a = store.upsert_entity(kind="person", name="P")
    b = store.upsert_entity(kind="project", name="X")
    store.upsert_edge(src_id=a, dst_id=b, kind="works_on")
    assert store.delete_entity(b) is True
    assert store.list_edges() == []


# ── Graph ─────────────────────────────────────────────────────────────


def test_build_round_trips(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    a = store.upsert_entity(kind="person", name="P")
    b = store.upsert_entity(kind="project", name="X")
    store.upsert_edge(src_id=a, dst_id=b, kind="works_on")
    G = g.build()
    assert G.number_of_nodes() == 2
    assert G.number_of_edges() == 1
    assert G.nodes[a]["name"] == "P"


def test_neighbors_both_directions(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    a = store.upsert_entity(kind="person", name="A")
    b = store.upsert_entity(kind="person", name="B")
    c = store.upsert_entity(kind="person", name="C")
    store.upsert_edge(src_id=a, dst_id=b, kind="knows")
    store.upsert_edge(src_id=c, dst_id=a, kind="knows")
    out = g.neighbors(a, direction="both")
    dirs = {n["direction"] for n in out["neighbors"]}
    assert {"in", "out"} <= dirs
    assert out["count"] == 2


def test_egonet_radius_one(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    centre = store.upsert_entity(kind="person", name="hub")
    nbs = [store.upsert_entity(kind="person", name=f"n{i}") for i in range(3)]
    far = store.upsert_entity(kind="person", name="far")
    for n in nbs:
        store.upsert_edge(src_id=centre, dst_id=n, kind="knows")
    # far is connected to only one neighbour, not to centre.
    store.upsert_edge(src_id=nbs[0], dst_id=far, kind="knows")
    ego = g.egonet(centre, radius=1)
    ids = {n["id"] for n in ego["nodes"]}
    assert centre in ids
    assert all(n in ids for n in nbs)
    assert far not in ids  # radius 1 doesn't reach


def test_egonet_radius_two_reaches_far(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    centre = store.upsert_entity(kind="person", name="hub")
    near = store.upsert_entity(kind="person", name="near")
    far = store.upsert_entity(kind="person", name="far")
    store.upsert_edge(src_id=centre, dst_id=near, kind="knows")
    store.upsert_edge(src_id=near, dst_id=far, kind="knows")
    ego = g.egonet(centre, radius=2)
    ids = {n["id"] for n in ego["nodes"]}
    assert far in ids


def test_shortest_path(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    a = store.upsert_entity(kind="person", name="A")
    b = store.upsert_entity(kind="person", name="B")
    c = store.upsert_entity(kind="person", name="C")
    store.upsert_edge(src_id=a, dst_id=b, kind="knows")
    store.upsert_edge(src_id=b, dst_id=c, kind="knows")
    path = g.shortest_path(a, c)
    assert path["found"] is True
    assert path["length"] == 2
    names = [step["name"] for step in path["path"]]
    assert names == ["A", "B", "C"]


def test_shortest_path_no_path(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    a = store.upsert_entity(kind="person", name="A")
    b = store.upsert_entity(kind="person", name="B")
    path = g.shortest_path(a, b)
    assert path["found"] is True
    assert path["length"] == -1


def test_stats_counts_kinds(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    store.upsert_entity(kind="person", name="A")
    store.upsert_entity(kind="person", name="B")
    store.upsert_entity(kind="project", name="P")
    s = g.stats()
    assert s["nodes"] == 3
    kinds = {k["kind"]: k["count"] for k in s["entity_kinds"]}
    assert kinds["person"] == 2
    assert kinds["project"] == 1


def test_top_entities_by_degree(kg: tuple[KGStore, KnowledgeGraph]) -> None:
    store, g = kg
    hub = store.upsert_entity(kind="person", name="hub")
    for i in range(5):
        n = store.upsert_entity(kind="person", name=f"n{i}")
        store.upsert_edge(src_id=hub, dst_id=n, kind="knows")
    top = g.top_entities(limit=3)
    assert top[0]["name"] == "hub"
    assert top[0]["degree"] == 5


# ── Singleton ─────────────────────────────────────────────────────────


def test_kg_service_singleton(tmp_path: Path) -> None:
    from ultron_kg import get_service, init
    import ultron_kg
    ultron_kg._service = None  # noqa: SLF001
    cfg = _cfg(tmp_path)
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_kg._service = None  # noqa: SLF001
