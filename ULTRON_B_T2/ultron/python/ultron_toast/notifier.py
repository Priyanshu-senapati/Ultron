"""Windows toast notification — built on the WinRT Toast API via
PowerShell so we don't drag a Python dependency in. Works on every
Windows 10 / 11 box without an Install-Module step.

The notifier is fire-and-forget: it spawns a detached PowerShell
process and returns immediately. We don't wait for the user to dismiss.
Failures are logged but never raised — a missing toast is never worth
crashing a sidecar.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import sys
import xml.sax.saxutils as _xs
from typing import Optional

logger = logging.getLogger("ultron.toast.notifier")


# WinRT XML toast template. Title is required; body and footer optional.
# Image / action buttons could be added later — kept minimal here so
# the script runs fast and is easy to debug.
_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
__TEMPLATE__
'@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ULTRON').Show($toast)
"""


def _build_xml(title: str, body: str, footer: Optional[str] = None) -> str:
    parts = [f"<text>{_xs.escape(title)}</text>",
             f"<text>{_xs.escape(body)}</text>"]
    if footer:
        parts.append(f"<text placement='attribution'>"
                     f"{_xs.escape(footer)}</text>")
    text_nodes = "\n".join(parts)
    return ("<toast><visual><binding template='ToastGeneric'>"
            + text_nodes
            + "</binding></visual></toast>")


def show(title: str, body: str, footer: Optional[str] = None) -> bool:
    """Show a Windows toast. Returns True if PowerShell was launched.

    Title is required; body and footer are optional but recommended —
    Windows clips the toast if only one line.
    """
    if sys.platform != "win32":
        logger.debug("toast skipped: not on Windows")
        return False
    title = (title or "ULTRON").strip()
    body = (body or "").strip()
    if not title and not body:
        return False
    xml_body = _build_xml(title, body, footer)
    # Closing here-doc terminator must sit at column 0 inside the
    # final script — _PS_TEMPLATE already does that. Just substitute.
    script = _PS_TEMPLATE.replace("__TEMPLATE__", xml_body)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        logger.info("toast queued: %s — %s", title[:40], body[:60])
        return True
    except OSError as exc:
        logger.warning("toast launch failed: %s", exc)
        return False


def show_blocking_for_test() -> str:
    """Sanity helper — returns the rendered PS script without running.
    Lets tests assert the script we'd run, without popping a real toast."""
    return _PS_TEMPLATE.replace("__TEMPLATE__",
                                _build_xml("hello", "world", "ULTRON"))
