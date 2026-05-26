# ultron.ps1 -- single entry point for the ULTRON stack.
#
# Usage:
#   ultron start    Launch the whole stack (idempotent -- skips already-running parts)
#   ultron stop     Kill the whole stack
#   ultron status   Show what's running
#   ultron chat     Open the text REPL
#   ultron logs     Tail today's quantum-log via core --verify (read-only)
#   ultron restart  Stop + start
#
# Paths assume:
#   Rust binaries: C:\dev\ULTRON_Q_COMPLETE\ultron\target\release
#   Python:        C:\dev\ULTRON_B_T2\ultron\python
#   venv:          C:\dev\ULTRON_B_T2\ultron\.venv

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'chat', 'logs', 'restart', 'hud', 'spectacle', '')]
    [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'

# ── Locations ────────────────────────────────────────────────────────────
$BIN     = "C:\dev\ULTRON_Q_COMPLETE\ultron\target\release"
$PY_DIR  = "C:\dev\ULTRON_B_T2\ultron\python"
$VENV    = "C:\dev\ULTRON_B_T2\ultron\.venv\Scripts\python.exe"
$OLLAMA  = "C:\Users\priyanshu\AppData\Local\Programs\Ollama\ollama.exe"
$CONFIG  = "$env:APPDATA\ULTRON\config.toml"
$DB      = "$env:APPDATA\ULTRON\data\quantum.db"

# Process definitions -- name in proc list, exe path, args, log token.
$RustProcs = @(
    @{ Name = 'ultron-core';            Exe = "$BIN\ultron-core.exe";            Args = @() },
    @{ Name = 'ultron-insight-pulse';   Exe = "$BIN\ultron-insight-pulse.exe";   Args = @() },
    @{ Name = 'ultron-memory-engine';   Exe = "$BIN\ultron-memory-engine.exe";   Args = @() },
    @{ Name = 'ultron-ghost';           Exe = "$BIN\ultron-ghost.exe";           Args = @() }
)
$PyProcs = @(
    @{ Tag = 'privacy-service';  Script = "$PY_DIR\privacy_service.py" },
    @{ Tag = 'tool-service';     Script = "$PY_DIR\tool_service.py" },
    @{ Tag = 'agent-service';    Script = "$PY_DIR\agent_service.py" },
    @{ Tag = 'code-service';     Script = "$PY_DIR\code_service.py" },
    @{ Tag = 'money-service';    Script = "$PY_DIR\money_service.py" },
    @{ Tag = 'trainer-service';  Script = "$PY_DIR\trainer_service.py" },
    @{ Tag = 'planner-service';  Script = "$PY_DIR\planner_service.py" },
    @{ Tag = 'kg-service';       Script = "$PY_DIR\kg_service.py" },
    @{ Tag = 'dopamine-service'; Script = "$PY_DIR\dopamine_service.py" },
    @{ Tag = 'flow-service';     Script = "$PY_DIR\flow_service.py" },
    @{ Tag = 'reentry-service';  Script = "$PY_DIR\reentry_service.py" },
    @{ Tag = 'readiness-service';Script = "$PY_DIR\readiness_service.py" },
    @{ Tag = 'interrupt-service';Script = "$PY_DIR\interrupt_service.py" },
    @{ Tag = 'context-preserver';Script = "$PY_DIR\context_preserver_service.py" },
    @{ Tag = 'recall-service';   Script = "$PY_DIR\recall_service.py" },
    @{ Tag = 'emotion-service';  Script = "$PY_DIR\emotion_service.py" },
    @{ Tag = 'selftuner-service';Script = "$PY_DIR\selftuner_service.py" },
    @{ Tag = 'toast-service';    Script = "$PY_DIR\toast_service.py" },
    @{ Tag = 'hud-service';      Script = "$PY_DIR\hud_service.py" },
    @{ Tag = 'sysinfo-service';  Script = "$PY_DIR\sysinfo_service.py" },
    @{ Tag = 'dailydata-service';Script = "$PY_DIR\dailydata_service.py" },
    @{ Tag = 'claude-feed';      Script = "$PY_DIR\claude_feed_service.py" },
    @{ Tag = 'syshealth-service'; Script = "$PY_DIR\syshealth_service.py" },
    @{ Tag = 'clipboard-service'; Script = "$PY_DIR\clipboard_service.py" },
    @{ Tag = 'proactive-service'; Script = "$PY_DIR\proactive_service.py" },
    @{ Tag = 'llm-service';      Script = "$PY_DIR\llm_service.py" },
    @{ Tag = 'insight_pulse';    Script = "$PY_DIR\insight_pulse.py" },
    @{ Tag = 'voice_engine';     Script = "$PY_DIR\voice_engine.py" },
    @{ Tag = 'bridges_service';  Script = "$PY_DIR\bridges_service.py" }
)

# ── Helpers ──────────────────────────────────────────────────────────────

function Get-Token {
    $line = Select-String 'token\s*=\s*"(.+)"' $CONFIG | Select-Object -First 1
    if (-not $line) { throw "could not read bridge token from $CONFIG" }
    return $line.Matches[0].Groups[1].Value
}

function Get-PyProc([string]$scriptMatch) {
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($scriptMatch) }
}

function Test-PortListening([int]$port) {
    (netstat -ano | Select-String ":$port +.*LISTENING") -ne $null
}

function Test-OllamaUp {
    try {
        $r = & $VENV -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:11434/api/tags',timeout=5).status_code==200 else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Start-OllamaIfNeeded {
    if (Test-OllamaUp) {
        Write-Host "  ollama : up" -ForegroundColor DarkGray
        return
    }
    Write-Host "  ollama : starting..." -ForegroundColor Yellow
    Start-Process -FilePath $OLLAMA -ArgumentList 'serve' -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        if (Test-OllamaUp) {
            Write-Host "  ollama : up" -ForegroundColor Green
            return
        }
    }
    Write-Warning "ollama did not come up within 20s"
}

# ── Commands ─────────────────────────────────────────────────────────────

function Cmd-Status {
    Write-Host ""
    Write-Host "ULTRON status" -ForegroundColor Cyan

    # Ollama
    $oll = Test-OllamaUp
    Write-Host ("  ollama        : {0}" -f $(if ($oll) { 'up' } else { 'down' })) `
        -ForegroundColor $(if ($oll) { 'Green' } else { 'Red' })

    # WS bridge
    $bridgeUp = Test-PortListening 9420
    Write-Host ("  bridge :9420  : {0}" -f $(if ($bridgeUp) { 'listening' } else { 'down' })) `
        -ForegroundColor $(if ($bridgeUp) { 'Green' } else { 'Red' })

    # Rust procs
    foreach ($p in $RustProcs) {
        $proc = Get-Process -Name $p.Name -ErrorAction SilentlyContinue
        $up = [bool]$proc
        Write-Host ("  {0,-22}: {1}" -f $p.Name, $(if ($up) { "up (PID $($proc.Id))" } else { 'down' })) `
            -ForegroundColor $(if ($up) { 'Green' } else { 'Red' })
    }

    # Python procs
    foreach ($p in $PyProcs) {
        $proc = Get-PyProc $p.Script
        $up = [bool]$proc
        $pidStr = if ($up) { ($proc | ForEach-Object { $_.ProcessId }) -join ',' } else { '' }
        Write-Host ("  {0,-22}: {1}" -f $p.Tag, $(if ($up) { "up (PID $pidStr)" } else { 'down' })) `
            -ForegroundColor $(if ($up) { 'Green' } else { 'Red' })
    }
    Write-Host ""
}

function Cmd-Start {
    Write-Host ""
    Write-Host "ULTRON starting..." -ForegroundColor Cyan

    Start-OllamaIfNeeded

    $token = Get-Token
    $env:ULTRON_TOKEN = $token
    $env:ULTRON_LLAVA_MODEL = 'llava:latest'
    # ULTRON is single-user, single-region. Pin the timezone every
    # process inherits so logs, context and HUD all agree on "now".
    $env:TZ = 'Asia/Kolkata'

    # Rust binaries -- start each only if not already running.
    foreach ($p in $RustProcs) {
        if (Get-Process -Name $p.Name -ErrorAction SilentlyContinue) {
            Write-Host ("  {0,-22}: already up" -f $p.Name) -ForegroundColor DarkGray
            continue
        }
        if (-not (Test-Path $p.Exe)) {
            Write-Warning "binary missing: $($p.Exe)"
            continue
        }
        Start-Process -FilePath $p.Exe -WorkingDirectory $BIN -WindowStyle Normal
        Write-Host ("  {0,-22}: launched" -f $p.Name) -ForegroundColor Green

        # ultron-core needs ~2s before the bridge is up; pause once.
        if ($p.Name -eq 'ultron-core') { Start-Sleep -Seconds 2 }
    }

    # Python sidecars -- same idempotency.
    foreach ($p in $PyProcs) {
        if (Get-PyProc $p.Script) {
            Write-Host ("  {0,-22}: already up" -f $p.Tag) -ForegroundColor DarkGray
            continue
        }
        Start-Process -FilePath $VENV -ArgumentList $p.Script `
            -WorkingDirectory $PY_DIR -WindowStyle Normal
        Write-Host ("  {0,-22}: launched" -f $p.Tag) -ForegroundColor Green
    }

    Start-Sleep -Seconds 2
    Cmd-Status

    # Open the live HUD in its own visible window. The HUD subscribes to
    # the bus and prints every voice event in plain English.
    Write-Host "Opening live HUD..." -ForegroundColor Cyan
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-Command", "`$env:TZ='Asia/Kolkata'; & '$VENV' '$PY_DIR\hud.py'"

    Write-Host "Ready. Talk via:  ultron chat" -ForegroundColor Cyan
    Write-Host "Or press Ctrl+Shift+Space (hotkey) or say 'Hey Ultron' (wake word)." -ForegroundColor DarkGray
}

function Cmd-Hud {
    if (-not (Test-PortListening 9420)) {
        Write-Warning "bridge not running -- run 'ultron start' first"
        return
    }
    & $VENV "$PY_DIR\hud.py"
}

function Cmd-Spectacle {
    if (-not (Test-PortListening 9420)) {
        Write-Warning "bridge not running -- run 'ultron start' first"
        return
    }
    & $VENV "$PY_DIR\spectacle_hud.py"
    Write-Host "Spectacle HUD launched in a chromeless browser window." -ForegroundColor Cyan
}

function Cmd-Stop {
    Write-Host ""
    Write-Host "ULTRON stopping..." -ForegroundColor Cyan

    # Python first (so they unsubscribe cleanly) -- match by script name.
    foreach ($p in $PyProcs) {
        $procs = Get-PyProc $p.Script
        foreach ($proc in $procs) {
            try {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                Write-Host ("  {0,-22}: stopped (PID {1})" -f $p.Tag, $proc.ProcessId) -ForegroundColor Yellow
            } catch {
                Write-Host ("  {0,-22}: stop failed: {1}" -f $p.Tag, $_) -ForegroundColor Red
            }
        }
    }

    # Then the Rust binaries.
    foreach ($p in $RustProcs) {
        $proc = Get-Process -Name $p.Name -ErrorAction SilentlyContinue
        if ($proc) {
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction Stop
                Write-Host ("  {0,-22}: stopped (PID {1})" -f $p.Name, $proc.Id) -ForegroundColor Yellow
            } catch {
                Write-Host ("  {0,-22}: stop failed: {1}" -f $p.Name, $_) -ForegroundColor Red
            }
        }
    }

    # Kill the HUD wrapper powershell (launched with -NoExit so the window
    # doesn't auto-close when hud.py dies). Match by command-line hint so
    # we don't nuke unrelated powershells.
    $hudShells = Get-WmiObject Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'hud\.py' -or $_.CommandLine -match 'ULTRON live HUD') }
    foreach ($s in $hudShells) {
        try {
            Stop-Process -Id $s.ProcessId -Force -ErrorAction Stop
            Write-Host ("  hud-wrapper           : stopped (PID {0})" -f $s.ProcessId) -ForegroundColor Yellow
        } catch {
            Write-Host ("  hud-wrapper           : stop failed: {0}" -f $_) -ForegroundColor Red
        }
    }

    Write-Host ""
}

function Cmd-Chat {
    if (-not (Test-PortListening 9420)) {
        Write-Warning "bridge not running -- run 'ultron start' first"
        return
    }
    & $VENV "$PY_DIR\repl.py"
}

function Cmd-Logs {
    if (-not (Test-Path "$BIN\ultron-core.exe")) {
        Write-Warning "ultron-core.exe not found"
        return
    }
    Write-Host "Walking quantum log (BLAKE3 chain verify)..." -ForegroundColor Cyan
    & "$BIN\ultron-core.exe" --verify
}

function Cmd-Restart {
    Cmd-Stop
    Start-Sleep -Seconds 1
    Cmd-Start
}

# ── Dispatch ─────────────────────────────────────────────────────────────

switch ($Command) {
    'start'   { Cmd-Start }
    'stop'    { Cmd-Stop }
    'status'  { Cmd-Status }
    'chat'    { Cmd-Chat }
    'logs'    { Cmd-Logs }
    'hud'     { Cmd-Hud }
    'spectacle' { Cmd-Spectacle }
    'restart' { Cmd-Restart }
    default   { Cmd-Status }
}
