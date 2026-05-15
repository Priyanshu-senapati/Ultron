# ULTRON v5.1 — Priyanshu Build

> Persistent, system-level cognitive twin. Not a chatbot — an OS-level intelligence layer.

This repo ships **Phase 0 / Module A** + **Phase 1 / Module H**:

**Phase 0 — foundation:**
- `ultron-core` — Rust daemon (Tokio, axum, tracing)
- `ultron-quantum-log` — append-only, hash-chained audit log (BLAKE3 + SQLite)
- `ultron-types` — shared types (events, WS protocol, tension snapshot)
- Windows Service registration (`windows-service` crate, SCM contract)
- WinAPI low-level keyboard + mouse hooks (privacy-respecting)
- WebSocket bridge on `127.0.0.1:9420` for Python and the Tauri HUD
- Tension tracker (EWMA + decay, hysteresis bands)
- Heartbeat + lifecycle events, full Quantum Log integration

**Phase 1 — perception (new):**
- Input metrics aggregator: WPM, backspace storms, mouse velocity & hesitation, click rate, app-switch rate, typing rhythm variance
- Active-window tracker: foreground HWND poll → title + process_name via `OpenProcess` + `QueryFullProcessImageNameW`
- Screenshotter: GDI `BitBlt` + `GetDIBits` → PNG into `%APPDATA%\ULTRON\screenshots\`, on-demand or periodic
- New event variants on the bus: `input_metrics_updated`, `window_changed`, `screenshot_captured` — all surface through the existing WebSocket bridge automatically

## Hardware target

- LAPTOP-HM36HMQC — Windows 11 25H2
- Ryzen AI 9 HX 370 + Radeon 890M + RTX 5070 Ti Laptop (12 GB)
- 32 GB DDR5, 1.86 TB NVMe
- Bengaluru (Asia/Kolkata, UTC+5:30)

## Layout

```
ultron/
├── Cargo.toml                          (workspace)
├── crates/
│   ├── ultron-types/                   (shared events, messages, tension)
│   │   └── src/
│   │       ├── events.rs               (+ InputMetricsUpdated, WindowChanged, ScreenshotCaptured)
│   │       └── perception.rs           (NEW — InputMetrics, WindowInfo, ScreenshotReason)
│   ├── ultron-quantum-log/             (audit spine)
│   └── ultron-core/                    (daemon)
│       └── src/
│           ├── main.rs                 (entry, modes, runtime — wires perception)
│           ├── config.rs               (+ [perception] section)
│           ├── error.rs
│           ├── event_bus.rs            (typed broadcast)
│           ├── tension.rs              (EWMA + decay + bands)
│           ├── input_monitor.rs        (WinAPI hooks + forwarder, also feeds metrics)
│           ├── ws_server.rs            (axum WS bridge)
│           ├── service.rs              (Windows Service plumbing)
│           └── perception/             (NEW — Phase 1, Module H)
│               ├── mod.rs
│               ├── metrics.rs          (InputMetricsAggregator + ticker)
│               ├── window_tracker.rs   (foreground HWND poll)
│               └── screenshot.rs       (BitBlt + GetDIBits + PNG)
├── service/
│   ├── install.ps1
│   └── uninstall.ps1
└── python/
    └── bridge_test.py                  (UPDATED — pretty-prints all event kinds)
```

## Build

Prerequisite: Rust 1.78+ (`rustup default stable` on Windows). MSVC toolchain
(`x86_64-pc-windows-msvc`) is what `windows-service` and the `windows` crate
expect.

```powershell
# from the repo root
cargo build --release --workspace
```

Optimised binary lands at `target/release/ultron-core.exe`.

## Run modes

| Command | What it does |
| --- | --- |
| `ultron-core` | Foreground console daemon (dev). Ctrl-C to stop. |
| `ultron-core --service` | SCM-invoked entry point. Don't run this manually. |
| `ultron-core --install` | Register the Windows Service (auto-start). **Elevated.** |
| `ultron-core --uninstall` | Stop + remove the service. **Elevated.** |
| `ultron-core --verify` | Walk the Quantum Log and verify the hash chain. |
| `ultron-core --print-token` | Print the bridge token (for client config). |

### Install as a service

```powershell
# from the repo root, after cargo build --release
PowerShell -ExecutionPolicy Bypass -File .\service\install.ps1
```

(The script self-elevates via UAC. Or run from an already-admin shell.)

### Test the bridge from Python

```powershell
python -m pip install websockets tomli
python python\bridge_test.py
```

You should see a stream of `heartbeat`, `input_activity`, `input_metrics_updated`,
`window_changed`, and `tension_changed` events. If you set
`perception.screenshot_interval_secs > 0`, you'll also see `screenshot_captured`.

## On-disk layout (created on first run)

```
%APPDATA%\ULTRON\
├── config.toml         (auto-bootstrapped, has the bridge token)
├── data\
│   └── quantum.db      (append-only audit log)
└── logs\
    └── ultron-core.YYYY-MM-DD.log    (when running as service)
```

## WebSocket protocol — summary

```jsonc
// → core
{ "op": "hello", "token": "<from config.toml>", "role": "python-bridge" }
{ "op": "subscribe", "kinds": ["heartbeat", "tension_changed"] }   // empty = all
{ "op": "publish", "kind": "module_event", "payload": { "x": 1 } }
{ "op": "ping" }

// ← core
{ "op": "welcome", "server_version": "0.1.0", "session_id": "..." }
{ "op": "event", "kind": "heartbeat", "ts": "...", "payload": { "tension": 0.12, "uptime_secs": 60 } }
{ "op": "error", "code": "bad_token", "message": "..." }
```

## Quantum Log

Every entry is hash-chained:

```
hash[i] = blake3( hash[i-1] || ts || kind || module || parent_id || payload_json )
```

- `UPDATE` and `DELETE` are blocked by SQL triggers.
- `ultron-core --verify` walks the whole table and re-derives every hash.
- Any byte mutation in any prior row breaks the chain at that row.

Boot and shutdown both write entries, so the log records its own lifecycle.

## What it does **not** capture

- Actual keystroke characters. Only categorical metadata (letter / digit /
  symbol / whitespace / backspace / modifier / navigation / function /
  system) plus modifier mask and timing.
- Clipboard contents.

**What Phase 1 *does* now capture** (and exposes through the bus + Quantum Log):
- Active foreground window title + process name (raw, local-only — the
  Privacy Router from Phase 4 governs anything outbound; the Ghost Network,
  Phase 1 / Module Q, will hash titles before LAN sync).
- Periodic screenshots of the primary monitor (opt-in: set
  `perception.screenshot_interval_secs > 0` in `config.toml`).

## Tests

```powershell
cargo test --workspace
```

Includes:
- Quantum Log: append, tail, async append, append-only triggers, tampered-row detection
- Tension tracker: idle-decay-to-zero, backspace-storm raises score
- Bus: fan-out
- Types: serde round-trips on `InputSignal`, `WsClientMessage`, `InputMetrics`,
  `WindowInfo`, `ScreenshotReason`, and the new event variants
- **Perception (new):** WPM accuracy, backspace-storm detection (+ no false
  positives on spread-out backspaces), click-rate counting, idle growth,
  mouse-reversal hesitation, window-switch counting

## Phase 0 — DONE

- [x] Rust daemon scaffolded with workspace
- [x] Windows Service install / uninstall / run-as-service
- [x] Tokio event bus with typed events
- [x] WinAPI low-level keyboard + mouse hooks (privacy-respecting)
- [x] Tension tracker (EWMA + decay + hysteresis bands)
- [x] WebSocket bridge with token auth + filter subscribe + publish
- [x] Quantum Log: append-only, hash-chained, verifiable
- [x] Boot / Shutdown bookend entries
- [x] Tracing → stdout (console) or daily JSON file (service)
- [x] Python bridge test client
- [x] First-run config bootstrap with random token

## Phase 1 — IN PROGRESS

- [x] **Module H**: Screen + Enhanced Input Engine
  - [x] Input metrics aggregator (WPM, backspace storms, mouse velocity & hesitation, click rate, app-switch rate, rhythm variance)
  - [x] Active-window tracker (Win32 foreground HWND polling, title + exe name)
  - [x] Screenshot capture (GDI BitBlt → PNG, on-demand + periodic)
  - [x] New event variants surfaced through WS bridge
  - [x] Quantum Log integration on every new event
  - [x] Tests for every new component
- [ ] **Module Q**: Ghost Network (LAN PUB/SUB, mDNS, AES-GCM)
- [ ] **Module O**: Insight Pulse (5-signal fusion, 30 s tick)

Next after **Module Q + Module O** lands: **Phase 2 — agents + tooling**.
