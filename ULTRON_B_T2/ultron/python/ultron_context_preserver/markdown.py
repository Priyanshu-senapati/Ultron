"""Format a :class:`ContextSnapshot` as a Markdown context packet.

The packet is meant to be human-readable AND parseable by a future
session that wants to ingest just the headers. We use H2 sections so
``grep -E '^## '`` enumerates them.
"""
from __future__ import annotations

import time
from typing import Optional

from .config import ContextPreserverConfig
from .snapshot import ContextSnapshot


def _fmt_age(ts: float, now: Optional[float] = None) -> str:
    if ts <= 0:
        return "never"
    now = now if now is not None else time.time()
    age = max(0.0, now - ts)
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def _fmt_iso(ts: float) -> str:
    if ts <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_secs(secs: Optional[float]) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f} min"


def _truncate_sentence(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end > max_chars // 3:
        return text[:end + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",;:.") + "…"


def render_packet(snap: ContextSnapshot, cfg: ContextPreserverConfig,
                  now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    lines: list[str] = []
    lines.append("# ULTRON Context Packet")
    lines.append("")
    lines.append(f"_Saved: {_fmt_iso(snap.saved_ts)} ({_fmt_age(snap.saved_ts, now)})  "
                 f"· reason: **{snap.reason}**_")
    lines.append("")

    lines.append("## Session")
    lines.append("")
    lines.append(f"- User: **{snap.user_name}**")
    lines.append(f"- Packet generation: ULTRON Context Preserver (Roadmap #5)")
    lines.append("")

    lines.append("## Last focus")
    lines.append("")
    if snap.focus_app:
        lines.append(f"- App: **{snap.focus_app}**"
                     + (f" _(category: {snap.focus_category})_"
                        if snap.focus_category else ""))
        lines.append(f"- Last seen: {_fmt_age(snap.focus_app_ts, now)}")
    else:
        lines.append("- _No focus data captured._")
    if snap.visual_label:
        lines.append(f"- Vision label: \"{snap.visual_label}\""
                     f" ({_fmt_age(snap.visual_label_ts, now)})")
    lines.append("")

    lines.append("## Last conversation turn")
    lines.append("")
    if snap.last_user_transcript or snap.last_llm_response:
        if snap.last_user_transcript:
            user_quote = _truncate_sentence(snap.last_user_transcript,
                                            cfg.max_llm_quote_chars)
            lines.append(f"- **User** ({_fmt_age(snap.last_user_ts, now)}): "
                         f"{user_quote}")
        if snap.last_llm_response:
            llm_quote = _truncate_sentence(snap.last_llm_response,
                                           cfg.max_llm_quote_chars)
            shard = f" _(shard: {snap.last_llm_shard})_" if snap.last_llm_shard else ""
            lines.append(f"- **ULTRON**{shard} ({_fmt_age(snap.last_llm_ts, now)}): "
                         f"{llm_quote}")
    else:
        lines.append("- _No recent turn captured._")
    lines.append("")

    lines.append("## Flow")
    lines.append("")
    lines.append(f"- Current state: **{snap.flow_state}**")
    if snap.flow_state == "active" and snap.flow_session_start_ts > 0:
        dur_min = max(0.0, (now - snap.flow_session_start_ts) / 60.0)
        lines.append(f"- Active session duration so far: {dur_min:.1f} min")
    if snap.last_flow_break_ts > 0:
        lines.append(f"- Last completed session: **{snap.last_flow_break_minutes:.1f} min**"
                     f", broken by `{snap.last_flow_break_reason or 'unknown'}`"
                     f"{' on ' + snap.last_flow_break_app if snap.last_flow_break_app else ''}"
                     f" ({_fmt_age(snap.last_flow_break_ts, now)})")
    lines.append("")

    lines.append("## Readiness")
    lines.append("")
    if snap.readiness_total is not None:
        lines.append(f"- Score: **{snap.readiness_total:.0f}/100** "
                     f"_(bucket: {snap.readiness_bucket})_"
                     f"  · updated {_fmt_age(snap.readiness_ts, now)}")
        for c in snap.readiness_components or []:
            lines.append(f"  - {c.get('name', '?'):>14s} : "
                         f"{c.get('score', 0):>5.1f}/{c.get('max_score', 0):.0f}"
                         f" _({c.get('detail', '')})_")
    else:
        lines.append("- _No readiness score captured yet._")
    lines.append("")

    lines.append("## Interrupts today")
    lines.append("")
    if snap.interrupts_today_count:
        lines.append(f"- Count: **{snap.interrupts_today_count}**"
                     + (f" · top source: `{snap.interrupts_top_source}`"
                        if snap.interrupts_top_source else ""))
        lines.append(f"- Avg recovery: {_fmt_secs(snap.interrupts_avg_recovery_secs)}")
    else:
        lines.append("- _No interrupts logged today._")
    lines.append("")

    lines.append("## Git (recent commits)")
    lines.append("")
    if snap.recent_commits:
        for c in snap.recent_commits[:cfg.max_commits]:
            sha = str(c.get("sha", ""))[:8] or "????????"
            msg = str(c.get("subject") or c.get("message") or "").strip()
            ts = c.get("ts") or c.get("ts_unix_ms")
            age = ""
            if isinstance(ts, (int, float)) and ts > 0:
                tsf = float(ts)
                if tsf > 1e12:
                    tsf /= 1000.0
                age = f" _( {_fmt_age(tsf, now)} )_"
            lines.append(f"- `{sha}` {msg}{age}")
    else:
        lines.append("- _No recent commit activity captured._")
    lines.append("")

    lines.append("## Claude Code session")
    lines.append("")
    if snap.claude_session_snippet:
        snippet = _truncate_sentence(snap.claude_session_snippet,
                                     cfg.max_claude_snippet_chars)
        lines.append(f"_(updated {_fmt_age(snap.claude_session_ts, now)})_")
        lines.append("")
        lines.append("```")
        lines.append(snippet)
        lines.append("```")
    else:
        lines.append("- _No Claude Code session snippet captured._")
    lines.append("")

    return "\n".join(lines)
