# ULTRON Tab Reporter (browser extension)

Reports the currently-active tab's URL and title to ULTRON's local
bridges sidecar over HTTP, so the cognitive twin knows what page you're
on (not just that "Chrome" is focused).

## Install (Chrome / Edge)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Toggle on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `browser_extension/` folder.
4. The extension is now active — no UI; it works silently in the background.

## Verify it's working

With ULTRON running (`ultron start`), switch tabs in your browser. In
the bridges sidecar window you should see log lines like:

    ultron.bridges.browser_tab: browser_tab https://github.com/... | "octocat/Hello-World"

And the HUD will show the page title in its bridges line.

## Config

The extension's endpoint is hard-coded to `http://127.0.0.1:8766/ingest`.
If you change `bridges.browser_tab.bind_port` in `config.toml`, update
`ENDPOINT` in `background.js` to match, then reload the extension.

## Privacy

The extension only POSTs to **localhost** — nothing leaves your machine.
The bridge requires no auth because it only binds to 127.0.0.1. Other
processes on the same machine could in principle POST to it; if that
matters to you, set `enabled = false` in the `[bridges.browser_tab]`
section.
