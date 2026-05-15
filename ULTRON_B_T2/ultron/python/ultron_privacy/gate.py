"""OutboundGate — the gatekeeper. Every outbound call goes through here.

Two principal entry points:
  - `approve_claude_call(system_prompt, messages)`: redacts LOCAL_ONLY
    patterns found in the prompt/messages, returns cleaned versions.
  - `approve_ghost_export(kind, payload)`: hashes LOCAL_ONLY fields like
    focus_app and window titles, lets SHAREABLE fields through.

A generic `check(destination, payload)` exists for future destinations
(arbitrary HTTP, future cloud services).

Every Nth decision is appended to an audit JSONL file for forensic review.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .anonymizer import HashAnonymizer
from .classifier import DataClass, DataClassifier

logger = logging.getLogger("ultron.privacy.gate")

# Fields treated as LOCAL_ONLY when found at the top level of a payload
# regardless of content. Mirrors classify_payload's policy in classifier.py.
_LOCAL_ONLY_KEYS = frozenset({
    "focus_app", "window_title", "file_path", "path",
    "exe", "exe_path", "cmdline",
})

# Redaction placeholder visible to the model — clear about what happened
# so it doesn't try to "fill in" the redacted text.
_REDACT_TOKEN = "[REDACTED]"


@dataclass
class GateDecision:
    allowed: bool
    data_class: DataClass
    reason: str
    destination: str
    redacted_payload: Optional[dict] = None
    redaction_count: int = 0
    ts_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_class"] = self.data_class.value
        return d


class OutboundGate:
    def __init__(
        self,
        classifier: DataClassifier,
        anonymizer: HashAnonymizer,
        audit_log_path: Path,
        log_every_n: int = 100,
    ) -> None:
        self._classifier = classifier
        self._anonymizer = anonymizer
        self._audit_path = audit_log_path
        self._log_every_n = max(1, log_every_n)
        self._counter = 0
        # Pre-compile the redaction substitution patterns once.
        self._redact_patterns: list[re.Pattern[str]] = list(classifier._patterns)  # noqa: SLF001

    # ── Generic ─────────────────────────────────────────────────────────

    def check(self, destination: str, payload: dict) -> GateDecision:
        """Top-level gate. Routes to the right specific gate or runs the
        generic one (redact LOCAL_ONLY keys, allow rest)."""
        classes = self._classifier.classify_payload(payload)
        worst = DataClass.most_restrictive(*classes.values()) if classes else DataClass.SHAREABLE

        # LOCAL_ONLY → redact and allow with hashed values
        redacted = self._anonymizer.redact_dict(payload, _LOCAL_ONLY_KEYS)
        # Also scrub free-text strings for LOCAL_ONLY patterns.
        redacted, redaction_count = self._scrub_strings(redacted)

        decision = GateDecision(
            allowed=True,
            data_class=worst,
            reason="ok",
            destination=destination,
            redacted_payload=redacted,
            redaction_count=redaction_count,
        )
        self._maybe_audit(decision)
        return decision

    # ── Claude API ──────────────────────────────────────────────────────

    def approve_claude_call(
        self, system_prompt: str, messages: list[dict]
    ) -> GateDecision:
        """Redact LOCAL_ONLY patterns from the prompt + every message.

        Allowed = True always (we redact, we don't refuse) unless every
        message becomes empty after redaction. That'd indicate the call
        was entirely LOCAL_ONLY content with nothing left to send.
        """
        clean_system, sys_count = self._scrub_text(system_prompt or "")
        clean_messages: list[dict] = []
        msg_count = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                cleaned, n = self._scrub_text(content)
                msg_count += n
                if cleaned.strip() == _REDACT_TOKEN:
                    # Message was 100% LOCAL_ONLY — drop entirely.
                    continue
                clean_messages.append({**m, "content": cleaned})
            else:
                clean_messages.append(m)
        total_redactions = sys_count + msg_count
        has_substance = bool(clean_system.strip()) or any(
            m.get("content", "").strip() for m in clean_messages if isinstance(m.get("content"), str)
        )
        decision = GateDecision(
            allowed=has_substance,
            data_class=DataClass.LOCAL_ONLY if total_redactions else DataClass.SHAREABLE,
            reason=(
                "claude_call_redacted" if total_redactions and has_substance
                else "claude_call_clean" if has_substance
                else "claude_call_all_local_only"
            ),
            destination="claude_api",
            redacted_payload={"system_prompt": clean_system, "messages": clean_messages},
            redaction_count=total_redactions,
        )
        self._maybe_audit(decision)
        return decision

    # ── Ghost Network ───────────────────────────────────────────────────

    def approve_ghost_export(self, kind: str, payload: dict) -> GateDecision:
        """Gate for Q's Ghost Network exports.

        - insight_snapshot tension/cognitive_load → SHAREABLE pass-through
        - focus_app, window titles → hash before export
        - Anything classified LOCAL_ONLY but not in known whitelist → drop
        """
        classes = self._classifier.classify_payload(payload)
        # Hash all LOCAL_ONLY string fields.
        redacted = self._anonymizer.redact_dict(payload, _LOCAL_ONLY_KEYS)
        # Free-text strings still need pattern scrubbing.
        redacted, scrubbed = self._scrub_strings(redacted)
        decision = GateDecision(
            allowed=True,  # we always allow with redaction for ghost
            data_class=DataClass.most_restrictive(*classes.values()) if classes else DataClass.SHAREABLE,
            reason=f"ghost_export[{kind}]_redacted" if scrubbed else f"ghost_export[{kind}]_clean",
            destination="ghost_network",
            redacted_payload=redacted,
            redaction_count=scrubbed,
        )
        self._maybe_audit(decision)
        return decision

    # ── Helpers ─────────────────────────────────────────────────────────

    def _scrub_text(self, text: str) -> tuple[str, int]:
        """Replace each match of a LOCAL_ONLY pattern with the redact token.

        Returns (scrubbed_text, replacement_count).
        """
        out = text
        count = 0
        for pat in self._redact_patterns:
            out, n = pat.subn(_REDACT_TOKEN, out)
            count += n
        return out, count

    def _scrub_strings(self, data: dict) -> tuple[dict, int]:
        """Recursively scrub every string value in a dict."""
        count = 0

        def walk(node):
            nonlocal count
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(x) for x in node]
            if isinstance(node, str):
                scrubbed, n = self._scrub_text(node)
                count += n
                return scrubbed
            return node

        return walk(data), count

    def _maybe_audit(self, decision: GateDecision) -> None:
        """Append every Nth decision to the audit JSONL."""
        self._counter += 1
        if self._counter % self._log_every_n != 0:
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as f:
                # Don't write the (possibly large) redacted_payload to disk;
                # log only the decision metadata.
                d = decision.to_dict()
                d.pop("redacted_payload", None)
                f.write(json.dumps(d) + "\n")
        except OSError as exc:
            logger.warning("could not append to audit log: %s", exc)
