"""Glue layer: embed the query, search turns + reflections, attach
neighbouring-turn context, format for LLM consumption.

The returned ``RecallBundle`` is shaped to be dropped into a system
prompt as the "long-term memory" block. Each hit carries a snippet
plus a ``why`` blurb (cosine score + recency) so callers can debug
why a given memory was surfaced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import RecallConfig
from .store import RecallStore, StoredReflection, StoredTurn


@dataclass
class TurnHit:
    turn: StoredTurn
    score: float
    neighbours_before: list[StoredTurn] = field(default_factory=list)
    neighbours_after: list[StoredTurn] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "turn",
            "id": self.turn.id,
            "ts": self.turn.ts,
            "role": self.turn.role,
            "content": self.turn.content,
            "conv_id": self.turn.conv_id,
            "score": round(self.score, 3),
            "neighbours_before": [
                {"id": t.id, "ts": t.ts, "role": t.role, "content": t.content}
                for t in self.neighbours_before
            ],
            "neighbours_after": [
                {"id": t.id, "ts": t.ts, "role": t.role, "content": t.content}
                for t in self.neighbours_after
            ],
        }


@dataclass
class ReflectionHit:
    reflection: StoredReflection
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "reflection",
            "id": self.reflection.id,
            "period_kind": self.reflection.period_kind,
            "period_start_ts": self.reflection.period_start_ts,
            "period_end_ts": self.reflection.period_end_ts,
            "summary": self.reflection.summary,
            "score": round(self.score, 3),
        }


@dataclass
class FactHit:
    fact: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        out = {"kind": "fact"}
        out.update(self.fact)
        return out


@dataclass
class RecallBundle:
    query: str
    turn_hits: list[TurnHit] = field(default_factory=list)
    reflection_hits: list[ReflectionHit] = field(default_factory=list)
    fact_hits: list[FactHit] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "turn_hits": [h.as_dict() for h in self.turn_hits],
            "reflection_hits": [h.as_dict() for h in self.reflection_hits],
            "fact_hits": [h.as_dict() for h in self.fact_hits],
            "counts": {
                "turns": len(self.turn_hits),
                "reflections": len(self.reflection_hits),
                "facts": len(self.fact_hits),
            },
        }


class RecallRetriever:
    """Owns the query path. The service holds one instance + an Embedder."""

    def __init__(self, store: RecallStore, cfg: RecallConfig) -> None:
        self._store = store
        self._cfg = cfg

    def search(self, query: str, query_emb, *,
               top_k: Optional[int] = None,
               include_reflections: bool = True,
               include_facts: bool = True,
               since_ts: Optional[float] = None) -> RecallBundle:
        cfg = self._cfg
        top_k = top_k or cfg.default_top_k
        top_k = max(1, min(top_k, cfg.max_top_k))

        bundle = RecallBundle(query=query)

        # Turns
        turn_results = self._store.search_turns(
            query_emb, top_k=top_k, min_score=cfg.min_score,
        )
        for turn, score in turn_results:
            if since_ts is not None and turn.ts < since_ts:
                continue
            neighbours = self._store.turns_around(turn.id, cfg.neighbour_window)
            before = [t for t in neighbours if t.ts < turn.ts]
            after = [t for t in neighbours if t.ts > turn.ts]
            bundle.turn_hits.append(TurnHit(
                turn=turn, score=score,
                neighbours_before=before, neighbours_after=after,
            ))

        # Reflections
        if include_reflections:
            refl_results = self._store.search_reflections(
                query_emb, top_k=max(2, top_k // 2),
                min_score=cfg.min_score,
            )
            for refl, score in refl_results:
                if since_ts is not None and refl.period_end_ts < since_ts:
                    continue
                bundle.reflection_hits.append(ReflectionHit(reflection=refl,
                                                           score=score))

        # Facts (Phase 2 — exact-match substring filter only; embedding
        # search over facts comes later).
        if include_facts:
            qlower = query.lower()
            for f in self._store.all_facts(limit=200):
                if (qlower in str(f.get("subject", "")).lower()
                        or qlower in str(f.get("object", "")).lower()
                        or qlower in str(f.get("predicate", "")).lower()):
                    bundle.fact_hits.append(FactHit(fact=f))
                    if len(bundle.fact_hits) >= top_k:
                        break

        return bundle

    def format_for_prompt(self, bundle: RecallBundle, *,
                          now: Optional[float] = None) -> str:
        """Render the bundle as a plain-text block for prompt injection."""
        now = now if now is not None else time.time()
        if not (bundle.turn_hits or bundle.reflection_hits or bundle.fact_hits):
            return ""
        lines: list[str] = ["# Long-term memory (recall)"]
        if bundle.fact_hits:
            lines.append("")
            lines.append("## Known facts")
            for h in bundle.fact_hits:
                f = h.fact
                lines.append(f"- {f.get('subject')} {f.get('predicate')} "
                             f"{f.get('object')}")
        if bundle.reflection_hits:
            lines.append("")
            lines.append("## Past session reflections")
            for h in bundle.reflection_hits[:3]:
                age = _fmt_age(h.reflection.period_end_ts, now)
                lines.append(f"- _{age}_: {h.reflection.summary}")
        if bundle.turn_hits:
            lines.append("")
            lines.append("## Past conversation turns")
            for h in bundle.turn_hits:
                age = _fmt_age(h.turn.ts, now)
                role = h.turn.role.upper()
                content = h.turn.content.strip().replace("\n", " ")
                if len(content) > 220:
                    content = content[:217].rstrip() + "…"
                lines.append(f"- _{age} · {role} · score {h.score:.2f}_: "
                             f"{content}")
        lines.append("")
        return "\n".join(lines)


def _fmt_age(ts: float, now: float) -> str:
    if ts <= 0:
        return "unknown"
    age = max(0.0, now - ts)
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"
