"""
context.py — ContextAssembler: builds the full prompt payload.

Combines:
1. System prompt (shard + addenda)
2. Cognitive state summary (from LiveState)
3. Recent visual labels (from memory.db — read-only)
4. Active behavioral patterns (from LiveState)
5. Conversation history
6. User message
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .personality import ShardSelection, build_system_prompt, select_shard
from .state import LiveState

logger = logging.getLogger("ultron.llm.context")

# User is in India. Hard-coding here is cheaper than threading a config
# field through every call site, and ULTRON is a personal twin -- not
# multi-tenant.
USER_TZ = ZoneInfo("Asia/Kolkata")


# When focus_app is one of these, we know the foreground is ULTRON itself
# (a sidecar terminal, the HUD, the REPL, etc.). Showing this to the LLM
# leads to useless "you're using ULTRON" answers when the user asks
# what they're doing. Replace with a placeholder so the model knows to
# ignore the focus signal for this turn.
_ULTRON_FOCUS_HINTS = (
    "python.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "conhost.exe",
    "cmd.exe",
    "ultron-core",
    "ultron-insight",
    "ultron-memory",
    "ultron-ghost",
    "ultron",
)


def _looks_like_ultron_focus(focus_app: str) -> bool:
    """Detect ULTRON's own terminal/sidecar windows by exe-name signal."""
    if not focus_app:
        return False
    low = focus_app.lower()
    return any(hint in low for hint in _ULTRON_FOCUS_HINTS)


class ContextAssembler:
    def __init__(
        self,
        memory_db_path: Path,
        max_context_memories: int = 5,
        high_load_threshold: float = 0.70,
        user_name: str = "",
        retriever: Optional["object"] = None,  # ultron_knowledge.KnowledgeRetriever
    ) -> None:
        self._db_path = memory_db_path
        self._max_memories = max_context_memories
        self._threshold = high_load_threshold
        self._user_name = user_name.strip()
        # The retriever is duck-typed (KnowledgeRetriever): only `retrieve(query)`
        # is called. Keeping the import optional means modules without the
        # `ultron_knowledge` package installed (or sentence-transformers
        # missing) still construct the assembler cleanly.
        self._retriever = retriever

    def assemble(
        self,
        user_message: str,
        state: LiveState,
        history: list[dict],
        mode: str = "default",
        forced_shard: Optional[str] = None,
    ) -> tuple[str, list[dict], ShardSelection]:
        """
        Returns:
          (system_prompt, messages_list, shard_selection)

        `messages_list` is in Ollama/Claude format:
          [{"role": "user"/"assistant", "content": "..."}]
        The caller prepends `system_prompt` appropriately for each API.
        """
        # 1. Select shard
        sel = select_shard(
            focus_category=state.focus_category,
            cognitive_load=state.cognitive_load,
            tension_band=state.tension_band,
            forced=forced_shard or state.forced_shard,
        )

        # 2. Build system prompt (with user identity line if known)
        system_prompt = build_system_prompt(
            sel.shard, mode, state.cognitive_load, self._threshold
        )
        if self._user_name:
            system_prompt = (
                f"The user you are addressing is named {self._user_name}. "
                f"Address them by name when natural.\n\n"
                + system_prompt
            )

        # 3. Assemble context block
        ctx_lines: list[str] = ["[CURRENT STATE]"]

        # Time in user's local zone (IST) -- so ULTRON can answer
        # "what time is it" and reason about morning/evening correctly.
        now_local = datetime.datetime.now(USER_TZ)
        # Explicit phase so the LLM picks "Good morning/afternoon/evening"
        # without having to do math on the hour itself.
        hour_local = now_local.hour
        if 5 <= hour_local < 12:
            phase = "morning"
        elif 12 <= hour_local < 17:
            phase = "afternoon"
        elif 17 <= hour_local < 21:
            phase = "evening"
        else:
            phase = "late night"
        ctx_lines.append(
            f"Local time: {now_local.strftime('%A, %d %B %Y, %H:%M:%S')} IST "
            f"({phase})"
        )

        # Cognitive. Focus selection cascade:
        #   1. If bridges' app_detail is fresh and non-ULTRON → use that.
        #   2. Else if raw focus_app is non-ULTRON → use that.
        #   3. Else mark as "[ULTRON itself]" so the model ignores it.
        # Showing ULTRON's own terminal as the user's focus leads to the
        # infuriating "you're using ULTRON" loop when they ask what
        # they're doing.
        focus_app: str
        if state.app_detail and state.app_detail_age_secs() < 300:
            exe_hint = (state.app_detail.get("exe") or "").strip()
            title_hint = (state.app_detail.get("title") or "").strip()
            if exe_hint and not _looks_like_ultron_focus(exe_hint):
                focus_app = f"{exe_hint} — {title_hint[:80]}" if title_hint else exe_hint
            elif not _looks_like_ultron_focus(state.focus_app):
                focus_app = state.focus_app or "[unknown]"
            else:
                focus_app = "[ULTRON's own terminal — IGNORE for activity context]"
        elif not _looks_like_ultron_focus(state.focus_app):
            focus_app = state.focus_app or "[unknown]"
        else:
            focus_app = "[ULTRON's own terminal — IGNORE for activity context]"

        ctx_lines.append(
            f"Cognitive load: {state.cognitive_load:.2f} | "
            f"Tension: {state.tension:.2f} ({state.tension_band}) | "
            f"Focus: {focus_app} ({state.focus_category})"
        )

        if state.fatigue_flag:
            ctx_lines.append("⚠ Fatigue flag: user has been coding for 3+ hours.")

        if state.wpm > 0:
            ctx_lines.append(
                f"Typing: {state.wpm:.0f} WPM | Circadian: {state.circadian_phase}"
            )

        if state.visual_label:
            ctx_lines.append(f"Currently on screen: {state.visual_label}")

        # Recent activity from memory (visual labels from last session) — only
        # inject when the snapshot is fresh enough to be representative.
        if state.snapshot_age_secs < 30:
            recent = self._recent_labels(limit=self._max_memories)
            if recent:
                ctx_lines.append("Recent activities: " + "; ".join(recent))

        # Integrations (Spotify, browser, GitHub, Google, app_detail).
        # Each bridge republishes on delta, so a "stale" timestamp can mean
        # "nothing changed for N minutes" rather than "data is wrong" — we
        # still cap at 5 min for most signals to avoid claiming the user
        # is listening to a song they finished long ago.
        for line in self._integration_lines(state):
            ctx_lines.append(line)

        # Self-awareness: ULTRON's own development signals (git, code edits,
        # boot reflection, claude session log). Same no-volunteer rule — the
        # personality prompt tells the model to surface these ONLY when the
        # user asks about them.
        for line in self._dev_awareness_lines(state):
            ctx_lines.append(line)

        # Behavioral patterns (high-confidence only)
        relevant = [p for p in state.patterns if p.confidence >= 0.65]
        if relevant:
            ctx_lines.append(
                "Detected patterns: "
                + "; ".join(
                    f"{p.summary} (conf {p.confidence:.2f})" for p in relevant[:3]
                )
            )

        # Productivity prior for current hour (IST)
        hour = now_local.hour
        prior = state.priors[hour] if 0 <= hour < 24 else None
        if prior is not None:
            ctx_lines.append(
                f"Learned productivity prior for hour {hour}: {prior:.2f}"
            )

        context_block = "\n".join(ctx_lines)

        # Retrieve relevant knowledge-graph chunks for this query.
        # Only injected on "default" mode — voice replies are short and the
        # retrieval block would balloon the prompt for no win.
        knowledge_block = ""
        if mode != "voice" and self._retriever is not None:
            try:
                hits = self._retriever.retrieve(user_message)
            except Exception as exc:  # noqa: BLE001
                logger.debug("retrieval raised (ignored): %s", exc)
                hits = []
            if hits:
                lines = ["[RELEVANT KNOWLEDGE]"]
                for h in hits:
                    # The LLM benefits from knowing where each snippet came from.
                    lines.append(f"### {h.heading_path}  (score {h.score:.2f})")
                    lines.append(h.text.strip())
                knowledge_block = "\n".join(lines)

        # 4. Build messages list. The full state context block is prepended
        # to the *current* user message on every turn so the model always
        # sees fresh focus_app / visual_label / patterns — state is mutable
        # and a stale value baked into history a few turns ago is worse
        # than no value at all.
        messages: list[dict] = list(history) if history else []
        user_content_parts = [context_block]
        if knowledge_block:
            user_content_parts.append(knowledge_block)
        user_content_parts.append(user_message)
        messages.append({
            "role": "user",
            "content": "\n\n".join(user_content_parts),
        })
        return system_prompt, messages, sel

    def _integration_lines(self, state: LiveState) -> list[str]:
        """Render fresh-enough bridge events into prompt lines.

        Each integration has its own freshness window because the underlying
        signal has different decay characteristics:
          - Spotify: a track is 3-4 min; 6 min cap stays safe past one song
          - browser_tab: tab focus is volatile; 3 min cap
          - app_detail: foreground app changes often; 3 min cap
          - calendar: data is about future events; 10 min is plenty
          - gmail: unread count is slow-moving; 15 min cap
          - gh_activity: recent-events feed; 15 min cap
        """
        out: list[str] = []

        sp = state.spotify
        if sp and state.spotify_age_secs() < 360:
            if sp.get("is_playing"):
                t = sp.get("track", "")
                a = sp.get("artist", "")
                al = sp.get("album", "")
                bits = [b for b in (t, a, al) if b]
                if bits:
                    out.append("Spotify (now playing): " + " — ".join(bits))
            else:
                # Only mention "paused" if we recently knew about playback
                # — otherwise the line is just noise.
                if sp.get("track"):
                    out.append(f"Spotify (paused): {sp.get('track', '')}")

        bt = state.browser_tab
        if bt and state.browser_tab_age_secs() < 180:
            title = (bt.get("title") or "").strip()
            url = (bt.get("url") or "").strip()
            if title or url:
                out.append(f"Active browser tab: {title or '(untitled)'} <{url}>")

        ad = state.app_detail
        if ad and state.app_detail_age_secs() < 180:
            app = ad.get("app", "")
            detail = ad.get("detail") or {}
            if app == "vscode" and detail.get("file"):
                folder = detail.get("folder", "")
                out.append(
                    f"VS Code: editing {detail['file']}"
                    + (f" in {folder}" if folder else "")
                    + (" (unsaved)" if detail.get("dirty") else "")
                )
            elif app == "jetbrains" and detail.get("file"):
                out.append(
                    f"{detail.get('ide', 'JetBrains IDE')}: editing "
                    f"{detail['file']} (project: {detail.get('project','')})"
                )
            elif app == "discord":
                if detail.get("kind") == "guild":
                    out.append(
                        f"Discord: #{detail.get('channel','')} in {detail.get('server','')}"
                    )
                elif detail.get("kind") == "dm":
                    out.append(f"Discord: DM with {detail.get('friend','')}")
            elif app == "office" and detail.get("document"):
                out.append(f"{detail.get('app','Office')}: {detail['document']}")

        cal = state.calendar
        if cal and state.calendar_age_secs() < 600:
            events = cal.get("events") or []
            if events:
                ev = events[0]
                summary = ev.get("summary", "(no title)")
                start = ev.get("start", "")
                out.append(f"Next calendar event: {summary} at {start}")
                if len(events) > 1:
                    out.append(f"Then {len(events)-1} more in the next 24h")

        gm = state.gmail
        if gm and state.gmail_age_secs() < 900:
            count = int(gm.get("count", 0))
            if count > 0:
                msgs = gm.get("messages") or []
                first_subjects = [m.get("subject", "") for m in msgs[:3] if m.get("subject")]
                line = f"Gmail: {count} unread"
                if first_subjects:
                    line += " — latest: " + "; ".join(first_subjects)
                out.append(line)

        gh = state.gh_activity
        if gh and state.gh_activity_age_secs() < 900:
            unread = int(gh.get("unread_count", 0))
            events = gh.get("events") or []
            if unread:
                out.append(f"GitHub: {unread} unread notifications")
            if events:
                top = events[0]
                out.append(
                    f"GitHub (recent): {top.get('summary', '')} on {top.get('repo', '')}"
                )

        return out

    def _dev_awareness_lines(self, state: LiveState) -> list[str]:
        """Lines about ULTRON's own development (git, file edits, claude session).

        Surfaced silently into context. The persona prompt keeps the model
        from volunteering unless the user asks ("what did I just change",
        "what is claude working on", "what's new since last boot").
        """
        out: list[str] = []

        # Boot reflection — only while still fresh (5 min) and not yet
        # acknowledged in conversation. Once mentioned, we don't repeat.
        br = state.boot_reflection
        if br and state.boot_reflection_age_secs() < 300 and not state.boot_reflection_acknowledged:
            commits = br.get("commits_since") or []
            if commits:
                lines = [f"[BOOT REFLECTION] {len(commits)} commit(s) since previous boot:"]
                for c in commits[:5]:
                    lines.append(f"  - {c.get('subject','')[:120]}  ({c.get('sha','')[:8]})")
                out.append("\n".join(lines))

        # Latest file change — useful for "what did I just edit".
        cc = state.code_change
        if cc and state.code_change_age_secs() < 120:
            out.append(
                f"Last edited file: {cc.get('path','')} "
                f"({cc.get('event','modified')}, "
                f"{int(state.code_change_age_secs())}s ago)"
            )

        # Recent commits — useful for "what did I commit", "recent changes".
        ga = state.git_activity
        if ga and state.git_activity_age_secs() < 600:
            commits = ga.get("commits") or []
            if commits:
                top = commits[0]
                out.append(
                    f"Latest git commit: \"{top.get('subject','')[:120]}\" "
                    f"({top.get('sha','')[:8]} by {top.get('author','')})"
                )

        # Claude Code session — useful for "what is claude doing", "what
        # did claude just say". The snippet is the LAST thing claude emitted.
        cs = state.claude_session
        if cs and state.claude_session_age_secs() < 300:
            kind = cs.get("kind", "")
            snippet = (cs.get("snippet") or "").strip()
            if snippet:
                short = snippet[:200] + ("…" if len(snippet) > 200 else "")
                out.append(f"Claude Code session ({kind}): {short}")

        return out

    def _recent_labels(self, limit: int = 5) -> list[str]:
        """Read the last N distinct visual labels from memory.db (read-only)."""
        if not self._db_path.exists():
            return []
        try:
            with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    "SELECT label FROM visual_labels ORDER BY ts_unix_ms DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            seen: set[str] = set()
            result: list[str] = []
            for (label,) in rows:
                if label not in seen:
                    seen.add(label)
                    result.append(label)
            return result
        except sqlite3.OperationalError as exc:
            logger.debug("could not read memory.db: %s", exc)
            return []
