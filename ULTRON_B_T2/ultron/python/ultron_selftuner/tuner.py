"""Heuristic threshold-tuning suggester.

The suggester is read-only — it surfaces structured suggestions with
rationale + evidence. The user (or a future CLI) applies them. We do
NOT auto-edit config.toml; that's reserved for an explicit opt-in flow
we haven't built yet (and the user has been clear that destructive
config writes need approval).
"""
from __future__ import annotations

from typing import Any

from .config import SelfTunerConfig


def suggest(facts: dict[str, Any], cfg: SelfTunerConfig) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    flow = facts.get("flow") or {}
    interrupts = facts.get("interrupts") or {}
    readiness = facts.get("readiness") or {}
    tools = facts.get("tools") or {}
    emotion = facts.get("emotion") or {}

    # ── 1. Tool reliability ───────────────────────────────────────────
    for name, st in tools.items():
        if (st.get("n", 0) >= cfg.tool_error_rate_min_calls
                and (1.0 - st.get("ok_rate", 1.0)) >= cfg.tool_error_rate_alert):
            out.append({
                "title": f"Tool `{name}` is failing too often",
                "rationale": (f"{st['errors']} errors in {st['n']} calls "
                              f"({(1.0 - st['ok_rate']) * 100:.0f}% failure rate). "
                              f"Investigate the handler or the upstream service."),
                "action": (f"Check logs for `{name}`; last error: "
                           f"'{st.get('last_error_reason', '')[:120]}'"),
                "evidence": f"n={st['n']} ok={st['ok']} errors={st['errors']}",
            })

    # ── 2. Flow break dominated by one source ─────────────────────────
    sessions = int(flow.get("sessions", 0))
    breakers = flow.get("top_breakers") or []
    if sessions >= 4 and breakers:
        top_name, top_n = breakers[0]
        if (top_n / max(1, sessions)) >= 0.6 and top_name not in ("idle", ""):
            out.append({
                "title": f"Flow keeps breaking from `{top_name}`",
                "rationale": (f"{top_n}/{sessions} sessions broke on the "
                              f"same reason today. Either the threshold for "
                              f"this signal is too aggressive or there's a "
                              f"workflow change worth blocking out."),
                "action": (f"If false-positive: raise the matching threshold "
                           f"in [flow] (e.g. max_app_switch_per_min for "
                           f"app_switch). If real: add a focus mode."),
                "evidence": f"top breaker {top_name} = {top_n}/{sessions}",
            })

    # ── 3. Most sessions are too short ────────────────────────────────
    if (sessions >= 4
            and float(flow.get("avg_minutes", 0)) < cfg.short_session_max_minutes):
        out.append({
            "title": "Flow sessions are very short on average",
            "rationale": (f"Avg session {flow.get('avg_minutes', 0)} min "
                          f"across {sessions} sessions today. The detector "
                          f"may be too eager to enter ACTIVE, or real "
                          f"work is being interrupted before flow takes hold."),
            "action": ("Consider raising [flow].samples_to_activate "
                       "(currently 3) to 4-5 ticks."),
            "evidence": f"avg={flow.get('avg_minutes', 0)} min, n={sessions}",
        })

    # ── 4. One interrupt source dominates ─────────────────────────────
    icount = int(interrupts.get("count", 0))
    if icount >= 6 and interrupts.get("by_source"):
        top = interrupts["by_source"][0]
        top_name, top_n = top[0], top[1]
        if (top_n / icount) >= cfg.interrupt_source_majority:
            out.append({
                "title": f"`{top_name}` dominates today's interruptions",
                "rationale": (f"{top_n}/{icount} interrupts came from "
                              f"`{top_name}`. The root cause might be "
                              f"upstream of ULTRON."),
                "action": (f"For wake_word: consider quiet hours. "
                           f"For wellness_nudge: shift to outside flow. "
                           f"For flow_break: see flow break suggestion."),
                "evidence": f"{top_name}={top_n}/{icount}",
            })

    # ── 5. Emotion: sustained frustration ─────────────────────────────
    em_avgs = emotion.get("averages") or {}
    if em_avgs.get("samples", 0) >= 6 and em_avgs.get("frustration", 0) >= 0.45:
        out.append({
            "title": "Frustration is elevated on average today",
            "rationale": (f"Avg frustration across {em_avgs['samples']} "
                          f"emotion samples = {em_avgs['frustration']:.2f}. "
                          f"That's not a single bad moment — it's the day's "
                          f"baseline."),
            "action": ("Check the interrupt + flow_break sections — they "
                       "usually correlate with the source. Tomorrow: try "
                       "blocking the top break reason."),
            "evidence": f"avg_frustration={em_avgs['frustration']:.2f}, "
                        f"samples={em_avgs['samples']}",
        })

    # ── 6. Readiness depleted but flow happened anyway ────────────────
    if (sessions >= 2
            and readiness.get("samples", 0) >= 2
            and float(readiness.get("latest", 100)) < 40):
        out.append({
            "title": "Readiness is depleted but flow still happened",
            "rationale": (f"Latest readiness {readiness['latest']}/100 "
                          f"(depleted) yet you logged {sessions} flow "
                          f"sessions ({flow.get('total_minutes', 0)} min). "
                          f"For this user the score may be over-weighting "
                          f"one signal (often sleep when sleep isn't logged)."),
            "action": "Consider lowering [readiness].weight_sleep from 40 "
                      "to 30 and redistributing.",
            "evidence": f"readiness={readiness['latest']}, "
                        f"flow_minutes={flow.get('total_minutes', 0)}",
        })

    # ── 7. Memory growing — celebrate quietly ─────────────────────────
    rc = facts.get("recall") or {}
    if rc.get("facts_today", 0) >= 3:
        out.append({
            "title": "New durable facts learned today",
            "rationale": (f"Auto-extractor wrote {rc['facts_today']} facts "
                          f"into recall.db. Memory is compounding."),
            "action": "No action — confirmation only.",
            "evidence": f"facts_today={rc['facts_today']}",
        })

    return out
