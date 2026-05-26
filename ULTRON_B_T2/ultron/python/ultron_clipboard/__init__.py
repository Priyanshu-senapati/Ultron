"""Clipboard intelligence -- watch, classify, and expose clipboard content."""
from .config import ClipboardConfig
from .watcher import ClipboardWatcher

__all__ = ["ClipboardConfig", "ClipboardWatcher"]
