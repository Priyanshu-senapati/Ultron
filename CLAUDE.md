# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

`C:\dev` contains 8 ULTRON variants representing successive development phases. **ULTRON_Q_COMPLETE** is the canonical reference (all Phase 1 modules). **ULTRON_B_T2** adds the Voice Engine (Module B) on top of Phase 1.

| Directory | Contents |
|-----------|----------|
| ULTRON_Q_COMPLETE | Full Phase 1 stack (H+O+D+Q modules) — use this as reference |
| ULTRON_B_T2 | Phase 1 + Voice Engine (Module B) |
| ULTRON_C | Python-only LLM client sidecar |
| ULTRON_B | Phase 1 early iteration |
| ULTRON_D, ULTRON_H, ULTRON_O, ULTRON_A | Intermediate phases |

All variants share the same internal layout: `<VARIANT>/ultron/` is the Rust workspace root; `<VARIANT>/ultron/python/` holds Python sidecars.

## Build & Run

```powershell
# Build all Rust crates (from workspace root, e.g. ULTRON_Q_COMPLETE\ultron\)
cargo build --release --workspace

# Single crate
cargo build --release -p ultron-core
```

**Running the full stack** (ULTRON_Q_COMPLETE — each in a separate terminal):
```powershell
# 1. Core daemon (WS bridge on 127.0.0.1:9420, generates config on first run)
.\target\release\ultron-core.exe

# 2. Insight fusion sidecar (5-second ticks)
$env:ULTRON_LOG = "info"; .\target\release\ultron-insight-pulse.exe

# 3. Memory engine
.\target\release\ultron-memory-engine.exe

# 4. Ghost network (LAN sync)
.\target\release\ultron-ghost.exe

# 5. Python LLaVA sidecar (visual labels for screenshots)
$env:ULTRON_TOKEN = (Select-String 'token\s*=' "$env:APPDATA\ULTRON\config.toml" | ForEach-Object { ($_.Line -split '"')[1] })
python python\insight_pulse.py

# 6. Event monitor (optional)
python python\bridge_test.py --filter insight_snapshot,visual_label,ghost:*
```

**Install as Windows Service:**
```powershell
PowerShell -ExecutionPolicy Bypass -File .\service\install.ps1
# or: .\target\release\ultron-core.exe --install  (requires admin)
```

**Print the bridge token** (needed by Python sidecars):
```powershell
.\target\release\ultron-core.exe --print-token
```

## Testing

```powershell
# Rust — ~50 tests across all crates
cargo test --workspace

# Single crate
cargo test -p ultron-ghost

# Python — ~5 pytest tests
cd python
pytest

# Single test file
pytest test_insight_pulse.py -v
```

## Architecture

ULTRON is a **multi-process cognitive twin** (not a chatbot). Modules communicate exclusively over the authenticated WebSocket bridge at `127.0.0.1:9420`. No shared memory, no direct crate-to-crate IPC between sidecars.

### Rust Crates

| Crate | Role |
|-------|------|
| `ultron-types` | Shared types: `UltronEvent` enum, `WsClientMessage`/`WsServerMessage`, `InsightSnapshot`, `AppCategory`, ghost peer types |
| `ultron-quantum-log` | Append-only SQLite audit spine with BLAKE3 hash chain; `UPDATE`/`DELETE` blocked by DB triggers |
| `ultron-core` | Main daemon: WinAPI input hooks → `EventBus` (typed broadcast) → WS bridge; also hosts perception (metrics, window tracker, screenshotter) |
| `ultron-insight-pulse` | Signal fusion sidecar: 5 signals (typing, clicking, window, tension, time-of-day) → circadian phase + cognitive load every 5 s |
| `ultron-memory-engine` | SQLite persistence: EWMA productivity learner + pattern detection (energy windows, app/tension correlations) |
| `ultron-ghost` | LAN peer sync: mDNS discovery, AES-256-GCM encryption (BLAKE3 KDF), TCP framing with reconnect/jitter |

### Python Sidecars

| File/Package | Role |
|--------------|------|
| `ultron_bridge.py` | Shared reconnecting WS client (exponential backoff) — all Python sidecars import this |
| `insight_pulse.py` | Module O: receives `screenshot_captured` events, runs LLaVA via Ollama, publishes `visual_label` |
| `ultron_llm/` | Module C: LLM router — Ollama (default) + Claude API (fallback); conversation history, personality shards, preference learning |
| `ultron_voice/` | Module B (ULTRON_B_T2 only): Faster-Whisper STT, Piper/Edge-TTS, Silero VAD, state machine (IDLE→LISTENING→TRANSCRIBING→SENDING) |

### Event Flow

```
WinAPI hooks → EventBus (Rust broadcast) → ultron-core WS server
                                                     │
                   ┌─────────────────────────────────┤
                   │                                  │
          Python sidecars                    Rust sidecars
      (insight_pulse, llm, voice)    (insight-pulse, memory-engine, ghost)
```

All events are `UltronEvent` variants (defined in `ultron-types/src/events.rs`). The WS protocol uses `WsClientMessage` (subscribe/unsubscribe/publish) and `WsServerMessage` (event/error).

## Configuration

First run of `ultron-core` creates `%APPDATA%\ULTRON\config.toml` with a random bridge token and ghost secret. Sections: `[daemon]`, `[perception]`, `[insight]`, `[memory]`, `[ghost]`, `[llm]`, `[voice]`.

Data lives in `%APPDATA%\ULTRON\data\quantum.db` (append-only; use `ultron-core --verify` to walk the BLAKE3 hash chain).

## Key Constraints

- **Windows-only**: WinAPI hooks (`windows` crate), HWND polling, Windows Service registration. Python sidecars are cross-platform.
- **Privacy**: Input hooks capture *categorical* metadata only (no keystroke text). Window titles are hashed before LAN sync.
- **Immutable audit log**: Any row mutation breaks hash-chain verification. Never attempt to modify `quantum.db` directly.
- **Independent failure domains**: Each sidecar process fails independently. Core daemon continues if any sidecar crashes.
- **Toolchain**: Rust 1.78+ with `x86_64-pc-windows-msvc` target; Python 3.10+; Ollama must be running locally for LLaVA/LLM features.
