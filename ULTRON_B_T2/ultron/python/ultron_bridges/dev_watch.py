"""dev_watch.py — ULTRON watches its own development.

Three signals folded into one bridge because they all observe the same
codebase at `C:\\dev`:

1. **Git activity** — periodic `git log --oneline -10` so ULTRON knows
   what was committed when. Useful for "what did I just commit" and
   for the startup reflection (below).

2. **Filesystem changes** — watchdog observer on the python/ rust/
   directories. Publishes `code_change` events when source files are
   touched, so ULTRON can answer "what file did I just edit".

3. **Boot reflection** — on bridge startup, compare the current git
   HEAD against the value stored from the previous boot. If they
   differ, publish a `boot_reflection` event summarising the commits
   between the two — the LLM can use this to greet the user with
   "Since last boot, sir, you've made these changes: ..."

State file: `%APPDATA%/ULTRON/data/dev_watch_state.json`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .base import Bridge, BridgePublishFn

logger = logging.getLogger("ultron.bridges.dev_watch")


@dataclass
class DevWatchConfig:
    enabled: bool = True
    repo_path: str = r"C:\dev"
    git_poll_secs: float = 60.0
    # Glob patterns considered code-relevant (filter watchdog noise).
    watch_extensions: tuple[str, ...] = (".py", ".rs", ".toml", ".ps1", ".md", ".js", ".ts", ".json")
    # Directories within repo_path that we DO NOT watch (build artefacts).
    skip_dirs: tuple[str, ...] = ("target", "__pycache__", ".venv", "node_modules", ".git")
    state_path: str = ""  # resolved at construction


class DevWatchBridge(Bridge):
    """Combined git + filesystem + boot reflection bridge."""

    name = "dev_watch"

    def __init__(
        self,
        publish: BridgePublishFn | None,
        cfg: DevWatchConfig,
        data_dir: Path,
    ) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self.repo = Path(cfg.repo_path)
        self.state_path = Path(cfg.state_path) if cfg.state_path else (data_dir / "dev_watch_state.json")
        self._observer: Optional[Any] = None  # watchdog Observer
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def run(self) -> None:
        if not self.repo.exists():
            self.log.warning("repo path %s does not exist; bridge idling", self.repo)
            await self._stop_event.wait()
            return

        self._loop = asyncio.get_event_loop()

        # 1. Boot reflection — compare last-known HEAD to current.
        await self._publish_boot_reflection()

        # 2. Start filesystem watcher.
        try:
            self._start_watchdog()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("watchdog observer failed to start: %s", exc)

        # 3. Loop: poll git log + check for stop.
        last_log_sig: Optional[str] = None
        while not self._stop_event.is_set():
            commits = self._git_log(limit=10)
            sig = "|".join(c["sha"] for c in commits)
            if sig and sig != last_log_sig:
                last_log_sig = sig
                await self.publish("git_activity", {
                    "commits": commits,
                    "head": commits[0]["sha"] if commits else "",
                    "ts_unix_ms": int(time.time() * 1000),
                })
                self.log.info("git_activity: %d commits (HEAD=%s)", len(commits), commits[0]["sha"][:8] if commits else "")
            # Sleep responsively.
            if not await self.sleep(self.cfg.git_poll_secs):
                break

        # Stop watchdog cleanly.
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

    # ── Git helpers ──────────────────────────────────────────────────────

    def _git(self, *args: str) -> Optional[str]:
        """Run a git command in the repo, return stdout (or None on failure)."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                return None
            return proc.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            self.log.debug("git %s failed: %s", args, exc)
            return None

    def _git_log(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent commits as [{sha, author, ts, subject}, ...]."""
        # `--no-pager` defends against terminal-aware pagers; format with sep.
        out = self._git("--no-pager", "log", f"-{limit}", "--pretty=format:%H|%an|%aI|%s")
        if not out:
            return []
        commits: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "sha": parts[0], "author": parts[1],
                    "iso_time": parts[2], "subject": parts[3],
                })
        return commits

    def _git_head(self) -> Optional[str]:
        out = self._git("rev-parse", "HEAD")
        return out.strip() if out else None

    def _git_log_since(self, ref: str, limit: int = 30) -> list[dict[str, Any]]:
        """Commits between `ref` and HEAD (exclusive of `ref`). Empty if same."""
        out = self._git("--no-pager", "log", f"{ref}..HEAD", f"-{limit}", "--pretty=format:%H|%an|%aI|%s")
        if not out:
            return []
        commits: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "sha": parts[0], "author": parts[1],
                    "iso_time": parts[2], "subject": parts[3],
                })
        return commits

    # ── State file ───────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log.warning("could not write state: %s", exc)

    # ── Boot reflection ──────────────────────────────────────────────────

    async def _publish_boot_reflection(self) -> None:
        head = self._git_head()
        state = self._load_state()
        last_head = state.get("last_boot_head")
        last_boot_ts = state.get("last_boot_ts")
        commits_since: list[dict[str, Any]] = []
        if head and last_head and head != last_head:
            commits_since = self._git_log_since(last_head, limit=30)

        await self.publish("boot_reflection", {
            "head": head or "",
            "previous_head": last_head or "",
            "previous_boot_iso": last_boot_ts or "",
            "commits_since": commits_since,
            "is_first_boot": not last_head,
            "ts_unix_ms": int(time.time() * 1000),
        })
        if commits_since:
            self.log.info(
                "boot_reflection: %d commit(s) since last boot",
                len(commits_since),
            )
        elif last_head and head == last_head:
            self.log.info("boot_reflection: no commits since last boot")
        else:
            self.log.info("boot_reflection: first boot (no prior state)")

        # Record current as the new "last boot".
        state["last_boot_head"] = head
        state["last_boot_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        self._save_state(state)

    # ── Filesystem watcher (watchdog) ────────────────────────────────────

    def _start_watchdog(self) -> None:
        from watchdog.observers import Observer  # type: ignore[import-not-found]
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]

        outer = self

        class Handler(FileSystemEventHandler):
            def _publish_change(self, event_type: str, path: str) -> None:
                if outer._loop is None:
                    return
                # Filter: must be a code-relevant extension, must not be in a skip dir.
                p = Path(path)
                if any(seg in outer.cfg.skip_dirs for seg in p.parts):
                    return
                if p.suffix.lower() not in outer.cfg.watch_extensions:
                    return
                rel = ""
                try:
                    rel = str(p.relative_to(outer.repo))
                except ValueError:
                    rel = str(p)
                asyncio.run_coroutine_threadsafe(
                    outer.publish("code_change", {
                        "event": event_type,
                        "path": rel,
                        "ts_unix_ms": int(time.time() * 1000),
                    }),
                    outer._loop,
                )

            def on_modified(self, event):
                if not event.is_directory:
                    self._publish_change("modified", event.src_path)

            def on_created(self, event):
                if not event.is_directory:
                    self._publish_change("created", event.src_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    self._publish_change("deleted", event.src_path)

        obs = Observer()
        obs.schedule(Handler(), str(self.repo), recursive=True)
        obs.start()
        self._observer = obs
        self.log.info("watchdog observing %s", self.repo)


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False
