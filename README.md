<div align="center">

<img src="https://img.shields.io/badge/Rust-53%25-orange?style=flat-square&logo=rust" />
<img src="https://img.shields.io/badge/Python-46%25-blue?style=flat-square&logo=python" />
<img src="https://img.shields.io/badge/Processes-19-red?style=flat-square" />
<img src="https://img.shields.io/badge/Tests-337-green?style=flat-square" />
<img src="https://img.shields.io/badge/Privacy-Local%20First-black?style=flat-square" />
<img src="https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows" />

# ULTRON

**A personal cognitive twin. 19 cooperating processes. Runs entirely on your hardware.**

*Not a chatbot. Not a wrapper. A system that watches, learns, and acts.*

</div>

---

You say *"Hey Ultron, what am I working on?"* and it tells you — because it's been watching your screen, your keystrokes, and your focus patterns all day. You say *"play something calm"* and Spotify starts, bypassing the LLM entirely. You ask *"what's my runway?"* and it pulls from your personal finance ledger. It tracks your cognitive load in real time, adapts its voice to your tension level, and writes a morning brief before you wake up.

It does all of this locally. Nothing sensitive leaves your machine.

```
ultron start          # bring up all 19 processes
ultron spectacle      # fullscreen HUD
ultron chat           # text REPL
ultron stop           # tear it all down
```

---

## What it actually is

ULTRON is a **multi-process Rust + Python system** where every component talks over a single authenticated WebSocket bus at `ws://127.0.0.1:9420/ws`. The Rust core runs WinAPI input hooks and hosts the bus. Python sidecars connect as clients. Each process is an independent failure domain — if one crashes, the rest stay up.

```
WinAPI hooks → EventBus (Rust) → WS bridge :9420
                                        │
          ┌─────────────────────────────┤─────────────────────────┐
          │                             │                         │
   Rust sidecars                 Python services            Daily data
   insight-pulse                 voice, LLM, tools          weather, stocks
   memory-engine                 agents, memory             news, markets
   ghost (LAN sync)              code, finance, health
```

The wire protocol is intentionally tiny:

```json
{ "op": "hello",     "token": "…",  "role": "service-name" }
{ "op": "subscribe", "kinds": ["insight_snapshot", "…"] }
{ "op": "publish",   "kind":  "tool_call_request", "payload": {…} }
```

Every meaningful event is written to an **append-only BLAKE3-chained quantum log** in SQLite. Mutation triggers reject `UPDATE` and `DELETE` on the audit table. The hash chain is the only proof that ULTRON's view of your history hasn't been tampered with.

---

## The 19 processes

### Rust core (4)

| Process | What it does |
|---------|-------------|
| `ultron-core` | WinAPI input hooks → event bus → WS bridge. Window tracker, screenshots, tension EWMA. |
| `ultron-insight-pulse` | 5-signal fusion: typing cadence, click rate, window focus, tension, circadian phase. Publishes `InsightSnapshot` every 5s. |
| `ultron-memory-engine` | Productivity learner. Builds your circadian prior from 14 days of behavioral history. Detects energy windows, app/tension correlations. |
| `ultron-ghost` | LAN sync. mDNS peer discovery, AES-256-GCM encryption, BLAKE3-keyed channels. |

### Python services (15)

| Service | What it does |
|---------|-------------|
| `voice_engine` | Wake word (*Hey Ultron*) + hotkey (`Ctrl+Shift+Space`) + Whisper STT + Kokoro/Piper TTS + Silero VAD. |
| `llm-service` | LLM router — Ollama local primary, Claude API fallback. Intent router bypasses the LLM for 18+ common actions. |
| `privacy-service` | Classifies every outbound byte: `LOCAL_ONLY` / `ANONYMIZED` / `SHAREABLE`. Strips paths, secrets, names before any API call. |
| `tool-service` | 19 built-in tools. Confirm-required gate for destructive actions (write, delete, shell). |
| `agent-service` | 5-role agent mesh: coordinator, researcher, coder, reviewer, sysadmin. Per-role tool allow-lists. |
| `code-service` | Indexes `C:\dev` (531 files, 234 Rust + 225 Python). Symbol search, semantic search, file stats. |
| `money-service` | Personal finance ledger — transactions, categories, budgets, monthly summaries, runway calculation. |
| `trainer-service` | Wellness ledger — workouts, sleep, body metrics, streaks, weekly rollups. |
| `planner-service` | Goals → outcomes → time blocks → calendar events. Built-in scheduler with alarms. |
| `kg-service` | NetworkX knowledge graph — entities, edges, egonets, shortest path, top entities. |
| `dopamine-service` | Pattern-matches focus events against rewarding/wasteful rules. Rolling EWMA score with flow alerts. |
| `hud-service` | Aggregates 6 live sections into one `hud_status_tick` every 5s. Optional system tray. |
| `dailydata-service` | Weather (Open-Meteo), Sensex/Nifty (yfinance), India news (Google News RSS). All free, no API keys. |
| `bridges_service` | Spotify, browser tab, GitHub, Google Calendar, Gmail, VS Code, Discord, dev filesystem watcher. |
| `claude-feed` | Sinks every ULTRON failure to `C:\dev\.ultron-feed\<date>.md` for Claude Code to read directly. |

---

## Voice

Two activation paths. Both feed the same pipeline.

**Wake word** — *"Hey Ultron, …"* — continuous listener, 60s cap, 7s silence timeout. Tolerates natural mid-sentence pauses.

**Hotkey** — `Ctrl+Shift+Space` — same recorder, same limits.

**What it knows when you talk to it:**

Every query arrives with a full context block injected silently — the LLM sees it but never recites it:

```
Time: 14:23 IST  Phase: afternoon  Battery: 87% (plugged)
Focus: VS Code — auth/token.rs  Visual: "writing rust code"
Tension: 0.41 (calm)  WPM: 65  Cognitive load: 0.38
Spotify: Daft Punk — Get Lucky
GitHub: 2 unread notifications
Next event: Team sync in 47 minutes
Weather: 31°C, partly cloudy
Sensex: 82,143 ▲ 0.4%
```

The model adapts tone to your tension level. High tension → shorter, simpler responses. Deep work → ARCHITECT shard. When you're stuck → COACH shard.

**Persona rules:** Addresses you as *"sir"* or *"commander"*. Strips surveillance preambles (*"I see you're working on…"*). Never volunteers your cognitive metrics unless you ask. TTS post-processor strips all markdown before synthesis.

---

## The intent router

Before any query reaches the LLM, a deterministic regex router catches 18+ common phrasings and dispatches directly — **no LLM latency, no narration, just the action:**

| You say | What happens |
|---------|-------------|
| *"play Ocean Eyes on Spotify"* | `spotify_play` → "Playing ocean eyes." |
| *"pause the music"* | `media_control{play_pause}` → "Paused." |
| *"open Chrome"* | `open_app{chrome}` → "Opening." |
| *"set brightness to 40"* | `brightness{set, 40}` |
| *"search rust async on YouTube"* | `web_open{youtube.com, query}` |
| *"volume up"* | `media_control{volume_up}` |
| *"dim the screen"* | `brightness{down, step:15}` |

Novel and open-ended queries fall through to the LLM with full context.

---

## Tools (19)

All tools dispatch through Module E. The LLM emits a fenced ` ```tool ` block; tool-service executes and returns the result.

**System:** `open_app` · `web_open` · `spotify_play` · `media_control` · `brightness` · `screenshot`

**Query (cross-process via WS):** `code_query` · `money_query` · `wellness_query` · `plan_query` · `kg_query` · `dopamine_query` · `memory_query` · `knowledge_search` · `web_search`

**Filesystem** (sandboxed to `C:\dev`):

| Tool | Confirm required |
|------|-----------------|
| `read_file` | No |
| `write_file` | **Yes** |
| `delete_file` | **Yes** |
| `shell` | **Yes** |

Confirm-required tools issue a single-use time-bounded approval token. The LLM cannot execute them unilaterally.

---

## What ULTRON sees

Injected into every LLM context, silently:

- **System** — clock, IST phase, battery, wifi SSID, Bluetooth count
- **Focus** — active app + window title, category, visual label from LLaVA
- **Cognitive state** — tension EWMA, cognitive load, circadian phase, fatigue flag, WPM
- **Activity** — recent visual labels, app rollups, detected behavioral patterns
- **Integrations** — Spotify, browser tab, GitHub, Calendar, Gmail, VS Code, Discord
- **Self-awareness** — git activity in `C:\dev`, recent file changes, boot reflection, Claude Code session
- **Daily data** — weather, Sensex/Nifty, top India headlines

Privacy classification governs everything. `LOCAL_ONLY` data (money, wellness, file paths, tokens) is available to local models but stripped before any remote API call by Module N.

---

## Privacy

Three guarantees:

**1. Module N gates every outbound byte.** `LOCAL_ONLY` data never reaches a remote LLM. The privacy router classifies, strips, redacts, or refuses before transmission.

**2. The quantum log is append-only.** SQLite triggers reject `UPDATE` and `DELETE` on the audit chain. Verify integrity anytime:
```
ultron logs    # walks the BLAKE3 chain end to end
```

**3. Vision is local.** Screen content is processed by LLaVA / llama3.2-vision via Ollama on your GPU. The only outbound calls without user prompt are weather / news / market polls — no user data included.

---

## The HUDs

```
ultron spectacle    # fullscreen HTML dashboard — cognitive score, wellness,
                    # weather, money, markets, headlines, schedule, activity
ultron hud          # terminal HUD — live voice events, mic level, focus app
```

System tray icon shows current dopamine score and workout streak. Right-click for quick commands.

---

## Tests

```
pytest python/          # 175 unit tests across 13 modules
cargo test --workspace  # 162 unit tests across 7 crates
python smoke_phase5.py  # 9 live integration smoke tests against running stack
```

---

## Stack

| Layer | Technology |
|-------|-----------|
| OS daemon | Rust (tokio async) |
| WS bridge | tokio-tungstenite |
| Persistence | SQLite (per-domain) + BLAKE3 audit chain |
| LLM local | Ollama (Llama 3 / Mistral / Qwen) |
| LLM cloud | Claude API (reasoning fallback, N-gated) |
| Vision | LLaVA / llama3.2-vision via Ollama |
| STT | faster-whisper (GPU) |
| TTS | Kokoro / Piper / Edge TTS |
| Wake word | openWakeWord |
| VAD | Silero |
| LAN sync | ZeroMQ + mDNS (mdns-sd) |
| Knowledge graph | NetworkX + SQLite |
| Code indexing | tree-sitter + FAISS |

**Hardware this was built and runs on:** AMD Ryzen AI 9 HX 370 · RTX 5070 Ti (12GB) · 32GB RAM · Windows 11 25H2

---

## Repo structure

```
ULTRON_B_T2/ultron/          ← active codebase
  crates/                    ← Rust workspace (4 crates)
  python/                    ← all Python services + shared bridge
    ultron_bridge.py         ← shared WS client (all sidecars import this)
    ultron_<module>/         ← one package per module
  python/tests/              ← 175 unit tests
start_ultron.ps1             ← process launcher
ultron.ps1                   ← CLI: start / stop / status / chat / logs
ULTRON.md                    ← full architecture reference
CLAUDE.md                    ← instructions for Claude Code sessions
```

---

## What's next

- Spotify Web API (OAuth) — play a specific track, not just open search
- Gmail send — OAuth scope already partially in place
- Windows toast notifications
- Flow State Protector — detect deep work, silence all interruptions automatically
- Re-entry Protocol — 10-second context brief when you return after an absence
- Tauri HUD — native binary replacing the chromeless browser approach

---

<div align="center">

**Built by a solo developer. Runs entirely on local hardware. Learns every day.**

*Rust · Python · Ollama · Windows · Local-first · No subscriptions · No cloud lock-in*

</div>
