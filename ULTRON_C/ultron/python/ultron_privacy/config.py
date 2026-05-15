"""Load `[privacy]` section from %APPDATA%/ULTRON/config.toml."""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


# Default LOCAL_ONLY patterns. Each is a regex tested against any string
# in an outbound payload. If any matches, the field is treated as
# LOCAL_ONLY and either redacted or blocks the whole call.
DEFAULT_LOCAL_ONLY_PATTERNS: tuple[str, ...] = (
    r"[A-Za-z]:\\Users\\[^\\/\s]+(?:[\\/][^\s]*)?",  # Windows user paths incl. username + rest
    r"/home/[^/\s]+(?:/[^\s]*)?",                     # Linux user paths
    r"/Users/[^/\s]+(?:/[^\s]*)?",                    # macOS user paths
    r"AppData[\\/][^\s]*",                            # Windows AppData subpaths
    r"\bAppData\b",                                   # bare AppData token
    r"\bpassword\b",
    r"\bpasswd\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"\bapi[._\- ]?key\b",
    r"\baccess[._\- ]?token\b",
    r"\bauth[._\- ]?token\b",
    r"\bprivate[._\- ]?key\b",
    r"\bBearer\s+[A-Za-z0-9_\-\.=]{20,}",
    r"\bsk-[A-Za-z0-9]{20,}",            # OpenAI-style keys
    r"\bsk-ant-[A-Za-z0-9_\-]{20,}",     # Anthropic keys
    r"\bghp_[A-Za-z0-9]{20,}",           # GitHub PAT
    r"\b[A-Za-z0-9+/]{40,}={0,2}\b",     # long base64 (keys/hashes)
)


@dataclass
class PrivacyConfig:
    # WS bridge
    ws_url: str
    ws_token: str

    # Classifier
    local_only_patterns: tuple[str, ...] = DEFAULT_LOCAL_ONLY_PATTERNS

    # Audit log every Nth gate decision (0 = log all, set high to throttle)
    log_every_n_gates: int = 100

    # Salt for anonymiser. Stable across sessions so the same value always
    # hashes the same way on this machine. Auto-generated on first run.
    anonymizer_salt: str = ""

    # Paths
    data_dir: Path = field(default_factory=_ultron_data_dir)
    audit_log_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "privacy_audit.jsonl")


def load_privacy_config(config_path: Path | None = None) -> PrivacyConfig:
    """Read the [privacy] section. Missing values use defaults."""
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    p = raw.get("privacy", {}) if isinstance(raw.get("privacy"), dict) else {}

    patterns = p.get("local_only_patterns") or DEFAULT_LOCAL_ONLY_PATTERNS
    if isinstance(patterns, list):
        patterns = tuple(str(x) for x in patterns)

    # Anonymizer salt: read from config; if missing or empty, generate one
    # and persist it back so subsequent runs are deterministic.
    salt = str(p.get("anonymizer_salt", "")).strip()
    if not salt:
        salt = secrets.token_urlsafe(24)
        _persist_salt(config_path, salt)

    return PrivacyConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        local_only_patterns=patterns,
        log_every_n_gates=int(p.get("log_every_n_gates", 100)),
        anonymizer_salt=salt,
        data_dir=data_dir,
    )


def _persist_salt(config_path: Path, salt: str) -> None:
    """Append anonymizer_salt to [privacy] section if not already present.

    We don't fully parse + re-emit TOML (would lose comments). Instead we
    detect [privacy] header and append; if no [privacy] header exists, we
    add a fresh one at the end. This is a one-shot operation per machine.
    """
    if not config_path.exists():
        return
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    if "anonymizer_salt" in text:
        return  # already set somehow
    section = f"\n\n[privacy]\nanonymizer_salt = \"{salt}\"\n"
    if "[privacy]" in text:
        # Insert the line right after the [privacy] header.
        text = text.replace("[privacy]", f"[privacy]\nanonymizer_salt = \"{salt}\"", 1)
    else:
        text += section
    try:
        config_path.write_text(text, encoding="utf-8")
    except OSError:
        pass
