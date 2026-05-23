"""Daily-reflection synthesizer.

Reads the live SQLite stores (flow, interrupts, readiness, recall) +
the in-memory observers, then writes a markdown document summarising
the day. Pure — no I/O beyond opening read-only SQLite connections and
writing the output file.

The output is meant to be read by ULTRON in the next session (via the
recall service or context_packet) AND by the user as a debrief.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .config import SelfTunerConfig
from .observer import EmotionObserver, ToolUsageObserver
from .tuner import suggest

logger = logging.getLogger("ultron.selftuner.reflector")


def _read_flow_stats(db_path: Path, since_ts: float) -> dict[str, Any]:
    if not db_path.exists():
        return {"sessions": 0}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT duration_secs, broken_by, last_focus_app "
                "FROM flow_sessions WHERE start_ts >= ?",
                (since_ts,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("flow db read failed")
        return {"sessions": 0}
    if not rows:
        return {"sessions": 0, "total_minutes": 0.0, "longest_minutes": 0.0}
    durations = [float(r["duration_secs"]) for r in rows]
    breakers: dict[str, int] = {}
    apps: dict[str, int] = {}
    for r in rows:
        b = (r["broken_by"] or "unknown").strip()
        breakers[b] = breakers.get(b, 0) + 1
        a = (r["last_focus_app"] or "").strip()
        if a:
            apps[a] = apps.get(a, 0) + 1
    return {
        "sessions": len(rows),
        "total_minutes": round(sum(durations) / 60.0, 1),
        "avg_minutes": round((sum(durations) / 60.0) / len(rows), 1),
        "longest_minutes": round(max(durations) / 60.0, 1),
        "top_breakers": sorted(breakers.items(), key=lambda kv: kv[1],
                               reverse=True)[:5],
        "top_apps": sorted(apps.items(), key=lambda kv: kv[1],
                           reverse=True)[:5],
    }


def _read_interrupt_stats(db_path: Path, since_ts: float) -> dict[str, Any]:
    if not db_path.exists():
        return {"count": 0}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source, recovery_secs FROM interrupts WHERE ts >= ?",
                (since_ts,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("interrupt db read failed")
        return {"count": 0}
    if not rows:
        return {"count": 0}
    by_source: dict[str, int] = {}
    recoveries: list[float] = []
    for r in rows:
        s = (r["source"] or "unknown").strip()
        by_source[s] = by_source.get(s, 0) + 1
        if r["recovery_secs"] is not None:
            recoveries.append(float(r["recovery_secs"]))
    return {
        "count": len(rows),
        "by_source": sorted(by_source.items(), key=lambda kv: kv[1],
                            reverse=True),
        "avg_recovery_secs": round(sum(recoveries) / len(recoveries), 1)
                              if recoveries else None,
        "longest_recovery_secs": round(max(recoveries), 1)
                                  if recoveries else None,
    }


def _read_readiness(db_path: Path, since_ts: float) -> dict[str, Any]:
    if not db_path.exists():
        return {"samples": 0}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT total, bucket FROM readiness_snapshots "
                "WHERE ts >= ? ORDER BY ts DESC",
                (since_ts,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("readiness db read failed")
        return {"samples": 0}
    if not rows:
        return {"samples": 0}
    totals = [float(r["total"]) for r in rows]
    return {
        "samples": len(rows),
        "latest": round(totals[0], 1),
        "avg": round(sum(totals) / len(totals), 1),
        "min": round(min(totals), 1),
        "max": round(max(totals), 1),
        "buckets": list({r["bucket"] for r in rows}),
    }


def _read_recall_counts(db_path: Path, since_ts: float) -> dict[str, Any]:
    if not db_path.exists():
        return {"turns_today": 0}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            t = conn.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE ts >= ?",
                (since_ts,),
            ).fetchone()["c"]
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM reflections "
                "WHERE created_ts >= ?",
                (since_ts,),
            ).fetchone()["c"]
            f = conn.execute(
                "SELECT COUNT(*) AS c FROM facts WHERE created_ts >= ?",
                (since_ts,),
            ).fetchone()["c"]
    except Exception:  # noqa: BLE001
        logger.exception("recall db read failed")
        return {"turns_today": 0}
    return {"turns_today": int(t), "reflections_today": int(r),
            "facts_today": int(f)}


def _start_of_day(now: float) -> float:
    tm = time.localtime(now)
    midnight = time.mktime(time.struct_time((
        tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0,
        tm.tm_wday, tm.tm_yday, tm.tm_isdst,
    )))
    return midnight


def gather_facts(cfg: SelfTunerConfig,
                 tool_obs: ToolUsageObserver,
                 emotion_obs: EmotionObserver,
                 now: Optional[float] = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    since = _start_of_day(now)
    return {
        "now": now,
        "since": since,
        "flow": _read_flow_stats(cfg.flow_db, since),
        "interrupts": _read_interrupt_stats(cfg.interrupt_db, since),
        "readiness": _read_readiness(cfg.readiness_db, since),
        "recall": _read_recall_counts(cfg.recall_db, since),
        "tools": tool_obs.stats(now=now),
        "emotion": {
            "averages": emotion_obs.averages(),
            "histogram": emotion_obs.histogram(),
            "peak_frustration": emotion_obs.peak_frustration(),
        },
    }


def render_markdown(facts: dict[str, Any],
                    suggestions: list[dict[str, str]]) -> str:
    now = facts["now"]
    date_str = time.strftime("%Y-%m-%d", time.localtime(now))
    lines: list[str] = []
    lines.append(f"# ULTRON Self-Reflection — {date_str}")
    lines.append("")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} "
                 f"covering today since midnight local._")
    lines.append("")

    # Flow.
    f = facts["flow"]
    lines.append("## Flow")
    if f.get("sessions", 0) == 0:
        lines.append("- No completed flow sessions today.")
    else:
        lines.append(f"- Sessions: **{f['sessions']}**  "
                     f"total **{f['total_minutes']:.1f} min**  "
                     f"avg {f.get('avg_minutes', 0)} min  "
                     f"longest {f.get('longest_minutes', 0)} min")
        if f.get("top_breakers"):
            tb = ", ".join(f"{name} ({n})" for name, n in f["top_breakers"])
            lines.append(f"- Top break reasons: {tb}")
        if f.get("top_apps"):
            ta = ", ".join(f"{name} ({n})" for name, n in f["top_apps"][:3])
            lines.append(f"- Sessions ended in: {ta}")
    lines.append("")

    # Interrupts.
    i = facts["interrupts"]
    lines.append("## Interrupts")
    if i.get("count", 0) == 0:
        lines.append("- No interrupts logged today.")
    else:
        lines.append(f"- Count: **{i['count']}**"
                     + (f"  ·  avg recovery {i['avg_recovery_secs']}s"
                        if i.get("avg_recovery_secs") is not None else ""))
        if i.get("by_source"):
            bs = ", ".join(f"{name} ({n})" for name, n in i["by_source"])
            lines.append(f"- By source: {bs}")
    lines.append("")

    # Readiness.
    r = facts["readiness"]
    lines.append("## Readiness")
    if r.get("samples", 0) == 0:
        lines.append("- No readiness snapshots today.")
    else:
        lines.append(f"- Latest **{r['latest']}/100**  "
                     f"avg {r['avg']}  range {r['min']}–{r['max']}  "
                     f"({r['samples']} snapshots)")
    lines.append("")

    # Memory growth.
    rc = facts["recall"]
    lines.append("## Memory growth")
    lines.append(f"- New conversation turns indexed: **{rc.get('turns_today', 0)}**")
    if rc.get("reflections_today"):
        lines.append(f"- Reflections written: {rc['reflections_today']}")
    if rc.get("facts_today"):
        lines.append(f"- Facts auto-extracted: {rc['facts_today']}")
    lines.append("")

    # Tools.
    tools = facts.get("tools") or {}
    lines.append("## Tools")
    if not tools:
        lines.append("- No tool calls recorded today.")
    else:
        ranked = sorted(tools.items(), key=lambda kv: kv[1]["n"], reverse=True)
        for name, st in ranked[:8]:
            err_note = ""
            if st["errors"] > 0:
                err_note = f"  ·  errors {st['errors']}  ({st['ok_rate']*100:.0f}% ok)"
                if st.get("last_error_reason"):
                    err_note += f"  ·  last: \"{st['last_error_reason'][:80]}\""
            lines.append(f"- `{name}`  n={st['n']}{err_note}")
    lines.append("")

    # Emotion.
    em = facts.get("emotion") or {}
    avgs = em.get("averages") or {}
    hist = em.get("histogram") or {}
    lines.append("## Emotion")
    if not avgs or avgs.get("samples", 0) == 0:
        lines.append("- No emotion samples today.")
    else:
        lines.append(f"- {avgs['samples']} readings  ·  "
                     f"avg v={avgs['valence']:+.2f}  "
                     f"a={avgs['arousal']:.2f}  "
                     f"f={avgs['frustration']:.2f}")
        if hist:
            top = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)
            lines.append("- Mood mix: "
                         + ", ".join(f"{lbl} ({n})" for lbl, n in top))
        peak = em.get("peak_frustration")
        if peak:
            t = time.strftime("%H:%M", time.localtime(peak["ts"]))
            lines.append(f"- Peak frustration at {t}: "
                         f"{peak['frustration']:.2f}")
    lines.append("")

    # Suggestions.
    lines.append("## Tuning suggestions")
    if not suggestions:
        lines.append("- _Nothing to tune today — keep going._")
    else:
        for i_, s in enumerate(suggestions, 1):
            lines.append(f"{i_}. **{s.get('title', 'suggestion')}**")
            if s.get("rationale"):
                lines.append(f"   - {s['rationale']}")
            if s.get("action"):
                lines.append(f"   - Action: `{s['action']}`")
            if s.get("evidence"):
                lines.append(f"   - Evidence: {s['evidence']}")
    lines.append("")

    return "\n".join(lines)
