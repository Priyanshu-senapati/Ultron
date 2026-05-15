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
├── Cargo.toml                          (workspace — 4 crates)
├── crates/
│   ├── ultron-types/                   (shared events, messages, tension)
│   │   └── src/
│   │       ├── events.rs               (+ InputMetricsUpdated, WindowChanged, ScreenshotCaptured)
│   │       ├── perception.rs           (InputMetrics, WindowInfo, ScreenshotReason, AppCategory)
│   │       ├── insight.rs              (InsightSnapshot, CadenceBand, CircadianPhase, compute_cognitive_load)
│   │       ├── ghost.rs                (Module Q wire types — inert; runtime pending)
│   │       └── tension.rs              (TensionSnapshot, TensionBand)
│   ├── ultron-quantum-log/             (audit spine — BLAKE3 hash-chained SQLite)
│   ├── ultron-core/                    (daemon)
│   │   └── src/
│   │       ├── main.rs                 (entry, modes, runtime, JoinHandle shutdown)
│   │       ├── config.rs               ([perception] + [insight] + app_categories)
│   │       ├── event_bus.rs            (typed broadcast)
│   │       ├── tension.rs              (EWMA + decay + bands; fires request_screenshot)
│   │       ├── input_monitor.rs        (WinAPI hooks + forwarder, feeds metrics + tension)
│   │       ├── ws_server.rs            (axum WS bridge — Welcome/Subscribe/Publish)
│   │       ├── service.rs              (Windows Service plumbing)
│   │       └── perception/             (Phase 1, Module H)
│   │           ├── mod.rs
│   │           ├── metrics.rs          (InputMetricsAggregator + WPM trend + slope)
│   │           ├── window_tracker.rs   (foreground HWND poll + AppCategory + screenshot)
│   │           └── screenshot.rs       (multi-monitor BitBlt + GetDIBits + PNG + bus listener)
│   └── ultron-insight-pulse/           (NEW — Phase 1, Module O Rust sidecar)
│       └── src/
│           ├── main.rs                 (config load, WS pump, tick loop, signal handling)
│           ├── circadian.rs            (local-time → CircadianPhase wrapper)
│           ├── signal_state.rs         (Arc<Mutex<>> state container)
│           ├── fusion.rs               (pure assemble() + 10 unit tests)
│           └── ws_client.rs            (reconnecting WS client w/ exponential backoff)
├── service/
│   ├── install.ps1
│   └── uninstall.ps1
└── python/
    ├── requirements.txt                (NEW — pinned versions for sidecar + tests)
    ├── conftest.py                     (NEW — pytest-asyncio auto mode)
    ├── ultron_bridge.py                (shared reconnecting WS client)
    ├── insight_pulse.py                (NEW — Module O Python LLaVA sidecar)
    ├── test_insight_pulse.py           (NEW — 5 pytest tests, all passing)
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
python -m pip install -r python\requirements.txt
python python\bridge_test.py
```

You should see a stream of `heartbeat`, `input_activity`, `input_metrics_updated`,
`window_changed`, and `tension_changed` events. If you set
`perception.screenshot_interval_secs > 0` or trigger a focus change, you'll
also see `screenshot_captured`. With Module O running you'll see
`insight_snapshot` every 5 s; with the LLaVA sidecar running you'll see
`visual_label` too.

### Full Phase-1 stack (up to eight processes)

Module C lives in a separate repo on your box; the other six all run from this workspace.
Apply the diffs in **`MODULE_C_CHANGES.md`** to C before starting Module B, or B will time
out waiting for `llm_response` events.

```powershell
# Terminal 1 — the daemon (perception + tension + bridge)
.\target\release\ultron-core.exe

# Terminal 2 — Module O Rust sidecar (5s fusion tick, publishes insight_snapshot)
$env:ULTRON_LOG = "info"
.\target\release\ultron-insight-pulse.exe

# Terminal 3 — Module D Memory Engine (SQLite persistence + learned priors + patterns)
.\target\release\ultron-memory-engine.exe

# Terminal 4 — Module Q Ghost Network (LAN PUB/SUB sidecar)
# First run: copy ghost_secret from %APPDATA%\ULTRON\config.toml to other machines
# you want in the cluster, then run them all.
.\target\release\ultron-ghost.exe

# Terminal 5 — Module O Python sidecar (LLaVA visual labels)
# Requires: ollama serve  +  ollama pull llava:7b  in another window
$env:ULTRON_TOKEN = (Select-String 'token\s*=' "$env:APPDATA\ULTRON\config.toml" |
                    ForEach-Object { ($_.Line -split '"')[1] })
python python\insight_pulse.py

# Terminal 6 — Module C LLM Client (lives in a separate repo on your box)
# Apply the changes in MODULE_C_CHANGES.md first.
python <path-to-C>\llm_service.py

# Terminal 7 — Module B Voice Engine
# Press Ctrl+Shift+Space to talk; release or stay silent ~1.5s to end.
# Single-clap on the H-monitored mic = same as hotkey.
python python\voice_engine.py

# Terminal 8 — watch everything go past, including remote `ghost:*` events
python python\bridge_test.py --filter insight_snapshot,visual_label,voice_transcript,voice_state_changed,llm_response,ghost:insight_snapshot
```

### Two-machine LAN test for Module Q

1. Run `ultron-ghost.exe` on machine A. It auto-generates a `ghost_secret` in `%APPDATA%\ULTRON\config.toml`.
2. Copy that `ghost_secret` value into machine B's config.toml's `[ghost]` section. The `instance_id` should NOT be copied — it must differ per machine.
3. Start the full stack on both machines. Within ~5 s they discover each other via mDNS and start exchanging encrypted frames.
4. Each side's `bridge_test.py` shows the other's events with the `[GHOST]` prefix.

### Module B verification (post-install)

1. Press `Ctrl+Shift+Space`. `bridge_test.py` shows `voice_state_changed: idle → listening`.
2. Speak: *"What time is it in Bengaluru?"*
3. Wait for silence (or release the hotkey).
4. Watch the chain: `voice_state_changed: listening → processing` → `voice_transcript` event with your text → C's `llm_response` event → `voice_state_changed: processing → speaking` → audio plays → `voice_state_changed: speaking → idle`.
5. Double-clap (or `clap_count=2` via H) for a status report.
6. While audio is playing, press `Ctrl+Shift+Space` again — barge-in: audio stops, state goes back to listening.

Run the test suite:

```powershell
# Rust — workspace tests across all 6 crates
cargo test --workspace

# Python — Module O + Module B = 20 tests, ~1.4s
pytest python\
```

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
  - [x] **WPM trend ring buffer + linear-regression slope per hour** (Module-O prep, Fix 1)
  - [x] Active-window tracker (Win32 foreground HWND polling, title + exe name)
  - [x] **App-category classification** via configurable map (Module-O prep, Fix 4)
  - [x] Screenshot capture (GDI BitBlt → PNG, on-demand + periodic)
  - [x] **Multi-monitor capture** via virtual-screen coordinates (Module-O prep, Fix 7)
  - [x] **Screenshot-on-window-change** (Module-O prep, Fix 2)
  - [x] **Screenshot-on-high-tension** via `request_screenshot` bus event (Module-O prep, Fix 3)
  - [x] Bus listener for ad-hoc capture requests
  - [x] New event variants surfaced through WS bridge
  - [x] Quantum Log integration on every new event
  - [x] Tests for every new component
- [x] **Module-O preparatory pass (10 fixes)** — see `crates/ultron-types/src/insight.rs` for the new `InsightSnapshot` wire type
  - [x] Fix 1: WPM trend ring buffer + linear-regression slope
  - [x] Fix 2: WindowChange screenshot trigger
  - [x] Fix 3: HighTension screenshot trigger via custom event
  - [x] Fix 4: `AppCategory` enum + classification map in config
  - [x] Fix 5: `InsightFired` / `InsightSuppressed` / `InsightTick` `EntryKind` variants
  - [x] Fix 6: `python/ultron_bridge.py` shared client with exponential-backoff reconnect
  - [x] Fix 7: virtual-screen coordinates for multi-monitor screenshots
  - [x] Fix 8: explicit `JoinHandle` collect + abort + await on shutdown
  - [x] Fix 9: deduplicated `clamp01` into `ultron_types`
  - [x] Fix 10: raised `w_idle` from 0.10 → 0.20 with calibration comment
- [x] **Module O Rust sidecar** (`ultron-insight-pulse` crate) — assembled, 8 + 2 fusion unit tests passing
- [x] **Module O Python sidecar** (`insight_pulse.py` LLaVA inference) — assembled, 3 + 2 pytest integration tests passing
- [x] **Module D Memory Engine** (`ultron-memory-engine` crate) — **COMPLETE. Turn 1: SQLite store + ingest. Turn 2: productivity learner with EWMA smoothing + O integration. Turn 3: pattern detection (energy windows, app/tension correlations, day-of-week modifiers) published as `patterns_update` events.**
- [x] **Module Q Ghost Network** (`ultron-ghost` crate) — **COMPLETE. Turn 1: foundational primitives (AES-256-GCM crypto, BLAKE3 KDF, peer map, config, sensitive-field scrubber). Turn 2: mDNS discovery, TCP listener with framing + decrypt, publisher with per-peer reconnect + backoff + jitter, full orchestrator. 45 unit tests including end-to-end listener<->publisher integration.**
- [x] **Module B Voice Engine** (`python/ultron_voice/` package + `voice_engine.py`) — **COMPLETE. faster-whisper STT on GPU with CPU fallback, Piper TTS local with Edge-TTS cloud fallback, silero-VAD end-of-speech detection, sounddevice I/O, push-to-talk hotkey + clap activation (1=wake, 2=status, 3=clipboard, 4=replay), VoiceStateMachine with barge-in, full bus-based integration with C via voice_transcript / llm_response events. 15 pytest tests, all passing (12 spec + 3 bonus).**

Next: **Phase 2 — agents + tooling** (no Phase 1 modules pending).
