# ULTRON — a personal cognitive twin

> A multi-process, voice-driven, privacy-first AI that **acts** on your
> machine instead of just talking about it. Lives on `C:\dev`, runs
> entirely on your hardware (with optional Claude API fallback for
> hard questions), and learns your patterns over time.

ULTRON is **not** a chatbot. It is 19 cooperating processes that share
a single message bus and together give you:

- A voice you can talk to that recognises wake words, transcribes
  speech, and replies in a steady voice.
- An LLM (local Ollama by default, Claude API as fallback) with full
  awareness of what you're doing, what you've done recently, your
  schedule, finances, fitness, focus, mood, and the markets.
- A toolbox the LLM and your voice can drive — open apps, play
  music, search the web, change brightness, write files, run shell,
  query your own knowledge graph and code index.
- A spectacle HUD that surfaces all of the above on a single screen.
- A feed straight to Claude Code so I can debug ULTRON's own
  failures without you copy-pasting logs.

---

## 1. How it's built

Everything talks over a **single authenticated WebSocket bridge** at
`ws://127.0.0.1:9420/ws`. The wire protocol is intentionally tiny —
three ops:

```
{"op":"hello",   "token":"…", "role":"<service-name>"}
{"op":"subscribe","kinds":["focus_app","weather_update", …]}
{"op":"publish",  "kind":"tool_call_request", "payload":{…}}
```

Each service runs in its own OS process. Independent failure
domains: if `dopamine-service` crashes, the rest of the stack stays
up. The Rust core (`ultron-core`) hosts WinAPI input hooks and the
WS server itself; everything else is a Python sidecar that connects
back as a client.

Authoritative storage is **SQLite per-domain** (one DB per module)
plus an append-only **BLAKE3-chained quantum log** that records
every meaningful event. Mutation triggers reject `UPDATE` / `DELETE`
on the audit table — the hash chain is the only proof that ULTRON's
view of your history is intact.

```
WinAPI hooks ─→ EventBus (Rust) ─→ WS bridge :9420 ─┐
                                                     │
            ┌────────────────────────────────────────┘
            │
     ┌──────┴──────┐   ┌──────────────┐   ┌─────────────┐
     │ Rust core   │   │ Python       │   │ Daily-data  │
     │ + insight-  │   │ sidecars     │   │ bridges     │
     │ pulse,      │   │ (voice, LLM, │   │ (weather,   │
     │ memory,     │   │ tools,       │   │ stocks,     │
     │ ghost       │   │ agents …)    │   │ news …)     │
     └─────────────┘   └──────────────┘   └─────────────┘
```

---

## 2. The 19 processes

Run with `ultron start`. Stop with `ultron stop`. State via
`ultron status` shows every one.

### Rust core (4)

| Process | Role |
|---|---|
| `ultron-core` | WinAPI input hooks → EventBus → WS bridge. Also screenshots, window tracker. |
| `ultron-insight-pulse` | 5-signal fusion (typing, clicking, window, tension, time-of-day) → circadian phase + cognitive load every 5 s. |
| `ultron-memory-engine` | EWMA productivity learner, pattern detection (energy windows, app/tension correlations). |
| `ultron-ghost` | LAN sync — mDNS discovery, AES-256-GCM encryption, BLAKE3-keyed channels (single-user today, peer-to-peer ready). |

### Python services (15)

| Service | Module | What it does |
|---|---|---|
| `privacy-service` | **N** | Classifies every outbound text (LOCAL_ONLY / ANONYMIZED / SHAREABLE); strips paths, secrets, names. |
| `tool-service` | **E** | Tool registry + executor + confirm flow. 19 built-in tools. |
| `agent-service` | **F** | 5-role agent mesh (coordinator, researcher, coder, reviewer, sysadmin) with per-role tool allow-lists. |
| `code-service` | **G** | Indexes `C:\dev` (currently 531 files, 234 Rust + 225 Python). Find symbol / search / list / stats. |
| `money-service` | **P** | Personal ledger — transactions, accounts, categories, budgets, monthly summaries, budget alerts. |
| `trainer-service` | **TT** | Wellness ledger — workouts, sleep, body metrics, streaks, weekly rollups. |
| `planner-service` | **S+J** | Goals → outcomes → time blocks → events. Built-in scheduler tick fires alarms. |
| `kg-service` | **K** | NetworkX-backed knowledge graph — entities, edges, neighbours, egonets, shortest path, top entities. |
| `dopamine-service` | **Y** | Pattern-matches every focus event against rewarding/wasteful rules. Rolling EWMA score with drift / flow alerts. |
| `hud-service` | **L** | Aggregates 6 live sections (dopamine, wellness, money, planner, code, KG) into one `hud_status_tick` event every 5 s. Optional tray icon. |
| `sysinfo-service` | — | Time / battery / wifi / bluetooth every 5 s. |
| `dailydata-service` | — | Weather (Open-Meteo + IP geo), Sensex/Nifty (yfinance), India news (Google News RSS). All free, no keys. |
| `claude-feed` | — | Sinks every ULTRON failure into `C:\dev\.ultron-feed\<date>.md` for Claude Code to read. |
| `llm-service` | **C** | LLM router (Ollama primary, Claude API fallback). Vision via LLaVA / llama3.2-vision. Conversation history. Intent router. |
| `insight_pulse` | **O** | Receives `screenshot_captured`, runs vision model, publishes `visual_label`. |
| `voice_engine` | **B** | Wake word + hotkey + Whisper STT + Kokoro/Piper/Edge TTS + Silero VAD + state machine. |
| `bridges_service` | — | Spotify, browser tab, GitHub, Google (calendar + gmail), app_detail, dev_watch, claude_session. |

---

## 3. Voice

**Two activation paths, both feed the same pipeline:**

- **Wake word** — say *"Hey Ultron, …"*. Listener runs continuously,
  caps the segment at 60 s, ends on 7 s of silence. Recognises
  punctuation variants (*"Hey, Ultron."* / *"hey   ultron"*).
  Tolerant of natural pauses mid-command.
- **Hotkey** — `Ctrl+Shift+Space` opens a recorder with the same
  limits.

**Shutdown phrases** kill the whole stack: *"bye ultron"*, *"by
ultron"*, *"buy ultron"*, *"goodnight ultron"*, *"goodbye ultron"*,
*"sleep ultron"*, *"shutdown ultron"*. ULTRON speaks a canned
farewell then spawns `ultron.ps1 stop` detached.

**Persona:**
- Addresses you as *"sir"* / *"commander"* — never as *"Commander
  Priyanshu"*. The first-name combination is post-processed out.
- No surveillance preambles. The post-processor strips *"I see you're
  working on …"* / *"Based on your snapshot …"* / *"Given your
  current state …"* from LLM output before TTS sees it.
- Doesn't volunteer internal metrics (cognitive load, tension,
  focus app) unless you ask.

**TTS sanitisation** — markdown emphasis (`**bold**`, `_italic_`,
backticks), code fences, bullet markers, and link syntax are
stripped before synthesis. No more *"asterisks asterisks bold
asterisks asterisks"*.

---

## 4. The intent router

The local LLM is unreliable at the tool-call protocol under load —
it narrates instead of acting, leaks persona violations, takes
seconds to respond. So **before** anything goes to the LLM, a
deterministic regex router catches 18+ phrasings of the common
verbs and dispatches the tool directly:

| You say | What happens |
|---|---|
| *"play Ocean Eyes on Spotify"* | `spotify_play{query:"ocean eyes"}` → "Playing ocean eyes." |
| *"play music"* / *"play some music"* | `media_control{play_pause}` → "Playing." |
| *"pause"* / *"pause the music"* | `media_control{play_pause}` → "Paused." |
| *"next song please"* | `media_control{next}` → "Next." |
| *"open chrome"* / *"open Spotify"* | `open_app{name}` → "Opening …" |
| *"search rust async on YouTube"* | `web_open{query, site:"youtube.com"}` |
| *"search rain in Chennai on Chrome"* | `web_open{query, browser:"chrome"}` |
| *"google X"* | `web_open{query:"X"}` |
| *"set brightness to 40"* | `brightness{set, level:40}` |
| *"dim the screen"* | `brightness{down, step:15}` |
| *"volume up"* / *"turn it down"* | `media_control{volume_*}` |
| *"mute"* | `media_control{mute}` |

When intent matches, the LLM is **bypassed entirely** — no narration,
no preamble, no minute-long ollama latency. Just the action plus a
one-word audible confirmation.

When intent doesn't match (novel / open-ended queries), the LLM
runs as normal with full context.

---

## 5. The toolbox (19 tools)

All tools dispatch through Module E. The LLM emits a fenced JSON
block ```` ```tool ```` for any tool call; tool-service executes
and returns a `tool_call_result`.

### System / actions

| Tool | What |
|---|---|
| `open_app(name)` | Launch a Windows app. Resolves via `Get-StartApps` AppsFolder AUMID so Microsoft Store apps (Spotify, Discord, WhatsApp) work just as well as classic apps. |
| `web_open(query / url / site / browser)` | Google search or open URL in chrome/brave/edge/firefox/default. |
| `spotify_play(query / uri)` | Play music via Spotify's URI handler. |
| `media_control(what)` | Windows media key. play_pause, next, prev, stop, mute, volume_up, volume_down. |
| `brightness(action, level / step)` | get / set / up / down. Hardware-verified to actually change the display. |
| `screenshot()` | Capture current screen for vision queries. |

### Read-only queries (cross-process via WS round-trip)

| Tool | What |
|---|---|
| `code_query(kind, …)` | Search the C:\\dev code index. find_symbol, search_symbols, list_files, stats. |
| `money_query(kind, …)` | Monthly summary, category rollup, top merchants, budget check, account balances, list_transactions, list_budgets, etc. |
| `wellness_query(kind, …)` | All streaks, weekly workout/sleep summary, latest metrics, weight trend, list_workouts/sleep/metrics. |
| `plan_query(kind, …)` | Today summary, upcoming blocks, upcoming events, list goals, goal progress, outcome time spent. |
| `kg_query(kind, …)` | Knowledge graph — stats, search entities, find entity, neighbours, egonet, shortest path, top entities. |
| `dopamine_query(kind, …)` | Current score, list patterns, list marks, daily rollup. |
| `memory_query(kind, …)` | Insight snapshots, app rollup, patterns, **time_window** (what was I doing between X and Y). |
| `knowledge_search(query)` | Full-text over the markdown KB. |
| `web_search(query)` | DuckDuckGo, returns text. |

### Filesystem (sandboxed to C:\\dev)

| Tool | What | Confirm |
|---|---|---|
| `read_file(path)` | Read a file, capped at 256 KB. | no |
| `write_file(path, content)` | Atomic write (tempfile → fsync → rename). | **yes** |
| `delete_file(path)` | Delete a file. | **yes** |
| `shell(cmd, cwd?)` | Run a shell command, output capped at 64 KB. | **yes** |

Confirm-required tools never execute on the LLM's word alone — a
single-use, time-bounded token is issued; the user has to approve.

---

## 6. The data ULTRON sees

Continuously updated and injected into the LLM's context block
(silently — the model uses it but doesn't recite it):

- **Time & system** — IST clock, time-of-day phase (morning /
  afternoon / evening / late night), battery % + plugged, wifi
  SSID, Bluetooth device count.
- **Focus** — current app + window title (your own ULTRON terminal
  is filtered out), category (terminal / browser / editor / …),
  visual label of what's on screen.
- **Cognitive state** — rolling tension EWMA, cognitive load,
  circadian phase, fatigue flag (3h+ coding streak), typing WPM.
- **Activity history** — recent visual labels, app rollups, behavioural
  patterns the memory engine has detected.
- **Integrations**
  - Spotify now-playing (track / artist / album).
  - Active browser tab (title + URL via the extension).
  - GitHub unread + recent events.
  - Calendar next event + 24h queue.
  - Gmail unread count + top subjects.
  - Office / VS Code / JetBrains / Discord — what file / project / channel.
- **Self-awareness**
  - Git activity in C:\\dev.
  - Recent code changes (filesystem watcher).
  - Boot reflection (what changed since last session).
  - Live Claude Code session tail.
- **Daily data**
  - Weather (Open-Meteo, current conditions + high/low).
  - Sensex + Nifty (yfinance, market-hours-aware polling).
  - Top India headlines (Google News RSS).

All under one **privacy classification**:
`LOCAL_ONLY` (money, wellness, location, paths) data is bridged but
never sent to a remote LLM — Module N drops or redacts it before
outbound calls.

---

## 7. The HUDs

Three ways to surface state:

- `ultron hud` — **terminal HUD** that scrolls live voice / state
  events, mic level, focus app, visual label.
- **System tray icon** — optional (`pystray`), displays current
  score and workout streak in the tooltip. Right-click menu: Open
  chat / Quit HUD.
- `ultron spectacle` — **fullscreen "spectacle" HUD**: chromeless
  Edge/Chrome window rendering an HTML dashboard. Cognitive score
  with band (drift / steady / flow), wellness streaks, weather,
  money + markets, India headlines, upcoming schedule, recent
  activity timeline. Press Esc to dismiss. Subscribes to the WS
  bridge directly via JavaScript.

---

## 8. Privacy

Single-user, single-machine by default. Three guarantees:

1. **Module N gates every outbound text** by privacy class. The
   `LOCAL_ONLY` class (money figures, wellness metrics, paths,
   tokens) never reaches a remote LLM. Outbound API calls go via
   the privacy router; it strips / redacts / refuses.
2. **The quantum log is append-only.** SQLite triggers reject
   `UPDATE` and `DELETE` on the audit chain. Verify integrity
   with `ultron logs` (walks the BLAKE3 chain end to end).
3. **No screen content leaves the machine** unless you explicitly
   ask. Vision is local (LLaVA / llama3.2-vision via Ollama). The
   only outbound calls without user prompt are weather / news /
   sensex polls (no user data sent) and (optionally) Claude API
   fallback for hard text questions, with N's privacy filter
   between.

---

## 9. Tests

- **Python**: 175 unit tests across 13 modules. `pytest` from
  `ULTRON_B_T2/ultron/python`.
- **Rust**: 162 unit tests across 7 crates (154 currently passing;
  8 pre-existing failures in async ghost tests / brittle assertions
  not touched in recent work).
- **Live integration smokes** (9): hit the running stack over the
  WS bridge and verify end-to-end behaviour.
  - `smoke_phase5.py` — cross-module: 6 query tools + HUD aggregator
    + money round-trip + KG add/stats.
  - `smoke_new_bridges.py` — battery / weather / sensex / news
    publish within 30 s.
  - `smoke_open_app.py` — Calculator launches via AppsFolder.
  - `smoke_spotify.py` — Microsoft Store Spotify launches.
  - `smoke_brightness.py` + `smoke_brightness_set.py` — hardware
    brightness actually changes (verified by reading WMI after).
  - `smoke_voice_to_tool.py` — full voice transcript → LLM → tool
    execution chain.
  - `smoke_new_tools.py` — web_open + spotify_play + claude_feed.
  - `smoke_intent_router.py` — 18 verb phrasings all intercept
    before the LLM.

---

## 10. Commands

From any terminal:

```powershell
ultron start       # bring up all 19 components + opens HUD
ultron stop        # tear them all down
ultron restart     # stop + start
ultron status      # show what's up / down
ultron chat        # text REPL on the bridge
ultron hud         # the terminal HUD
ultron spectacle   # the fullscreen HTML HUD
ultron logs        # walk the BLAKE3 quantum log
```

Per-config file at `%APPDATA%\ULTRON\config.toml` controls every
service — bridges, voice voice/STT models, tick cadences, privacy
salt, ghost peer secret, app aliases, etc. First boot writes a
random bridge token + ghost secret.

---

## 11. What the Claude Code feed gives you

ULTRON failures (tool errors, LLM errors, voice errors) auto-append
to `C:\dev\.ultron-feed\<date>.md` as a markdown section per
event — summary + full JSON payload. Future Claude Code sessions
read that file on demand.

The intended workflow: when something misbehaves, instead of
copy-pasting logs into a chat, just say *"Claude, look at the
ULTRON feed"* and I read the file directly.

---

## 12. Open and worth doing next

- **Email writing** — Gmail send (needs OAuth scope upgrade).
- **Spotify Web API control** — *play this specific track NOW*
  rather than *open Spotify's search for this track*. Needs OAuth.
- **WhatsApp Web bridge** — read recent chats / send a message.
  Bigger build, deferred.
- **Windows toast notifications** — `winsdk` install was blocking.
  Worth retrying.
- **Tauri rewrite of the spectacle HUD** — current chromeless-browser
  approach works but a native binary would be sharper.

---

*Where to look in the repo:*
- `C:\dev\ULTRON_B_T2\ultron\crates` — Rust workspace.
- `C:\dev\ULTRON_B_T2\ultron\python` — every Python sidecar +
  shared `ultron_bridge.py`.
- `C:\dev\ULTRON_B_T2\ultron\python\ultron_<module>` — one folder
  per module (privacy, tools, agents, code, money, trainer, planner,
  kg, dopamine, hud, sysinfo, dailydata, claude_feed, llm, voice).
- `C:\dev\ultron.ps1` — the single launcher.
