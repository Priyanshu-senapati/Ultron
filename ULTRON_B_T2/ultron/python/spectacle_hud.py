"""spectacle_hud — open the fullscreen ULTRON HUD in a chromeless window.

We don't ship a dedicated GUI runtime (pywebview won't build on Python
3.14; Qt isn't installed). Instead we launch the HTML in Edge's
``--app=`` mode, which gives us a frameless single-page window. Token
goes in the URL fragment so it never reaches server access logs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _read_token_and_url() -> tuple[str, str]:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_path = Path(appdata) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    return bridge["token"], f"ws://{bridge['bind']}/ws"


def _find_browser() -> tuple[str, list[str]] | None:
    """Locate Edge or Chrome and return (exe, extra-args). Either supports
    ``--app=<url>`` to render a single page chromeless."""
    candidates: list[tuple[str, list[str]]] = []
    if sys.platform == "win32":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            (rf"{pfx}\Microsoft\Edge\Application\msedge.exe", ["--new-window"]),
            (rf"{pf}\Microsoft\Edge\Application\msedge.exe",  ["--new-window"]),
            (rf"{local}\Microsoft\Edge\Application\msedge.exe", ["--new-window"]),
            (rf"{pf}\Google\Chrome\Application\chrome.exe",  ["--new-window"]),
            (rf"{pfx}\Google\Chrome\Application\chrome.exe", ["--new-window"]),
            (rf"{local}\Google\Chrome\Application\chrome.exe", ["--new-window"]),
            (rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe", ["--new-window"]),
            (rf"{local}\BraveSoftware\Brave-Browser\Application\brave.exe", ["--new-window"]),
        ]
    # Fallback PATH lookup.
    for name in ("msedge.exe", "chrome.exe", "brave.exe"):
        p = shutil.which(name)
        if p:
            candidates.append((p, ["--new-window"]))
    for exe, args in candidates:
        if exe and Path(exe).exists():
            return exe, args
    return None


def main() -> int:
    html_path = Path(__file__).with_name("spectacle_hud") / "index.html"
    if not html_path.exists():
        print(f"missing UI file: {html_path}", file=sys.stderr)
        return 2
    try:
        token, ws_url = _read_token_and_url()
    except Exception as exc:  # noqa: BLE001
        print(f"could not read config.toml: {exc}", file=sys.stderr)
        return 2
    # Fragment params — never sent to a server, never logged.
    fragment = (
        f"token={urllib.parse.quote(token)}"
        f"&ws={urllib.parse.quote(ws_url)}"
    )
    url = html_path.resolve().as_uri() + "#" + fragment
    found = _find_browser()
    if found is None:
        # Last-resort: hand it to the user's default browser. Not chromeless,
        # but still functional.
        import webbrowser
        webbrowser.open(url, new=1)
        return 0
    exe, base_args = found
    cmd = [exe, *base_args, "--start-maximized", f"--app={url}"]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                      | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
