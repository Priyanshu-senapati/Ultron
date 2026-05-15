# start_ultron.ps1 — Launch the full ULTRON stack
# Rust binaries from ULTRON_Q_COMPLETE (pre-built)
# Python sidecars from ULTRON_B_T2 (voice + LLM)

$BIN  = "C:\dev\ULTRON_Q_COMPLETE\ultron\target\release"
$PY   = "C:\dev\ULTRON_B_T2\ultron\python"
$VENV = "C:\dev\ULTRON_B_T2\ultron\.venv\Scripts\python.exe"

# Read the bridge token from config.toml
$TOKEN = (Select-String 'token\s*=\s*"(.+)"' "$env:APPDATA\ULTRON\config.toml" |
          ForEach-Object { $_.Matches[0].Groups[1].Value })

if (-not $TOKEN) {
    Write-Error "Could not read bridge token from config.toml"
    exit 1
}

Write-Host "Starting ULTRON stack..." -ForegroundColor Cyan
Write-Host "Token: $($TOKEN.Substring(0,8))..." -ForegroundColor DarkGray

# 1 — Core daemon (WS bridge on 127.0.0.1:9420)
Write-Host "[1/6] ultron-core" -ForegroundColor Green
Start-Process -FilePath "$BIN\ultron-core.exe" `
    -WorkingDirectory $BIN `
    -WindowStyle Normal

Start-Sleep -Seconds 2

# 2 — Insight pulse (5-second fusion ticks)
Write-Host "[2/6] ultron-insight-pulse" -ForegroundColor Green
Start-Process -FilePath "$BIN\ultron-insight-pulse.exe" `
    -WorkingDirectory $BIN `
    -WindowStyle Normal

# 3 — Memory engine
Write-Host "[3/6] ultron-memory-engine" -ForegroundColor Green
Start-Process -FilePath "$BIN\ultron-memory-engine.exe" `
    -WorkingDirectory $BIN `
    -WindowStyle Normal

# 4 — Ghost network (LAN sync)
Write-Host "[4/6] ultron-ghost" -ForegroundColor Green
Start-Process -FilePath "$BIN\ultron-ghost.exe" `
    -WorkingDirectory $BIN `
    -WindowStyle Normal

Start-Sleep -Seconds 1

# 5 — Module C: LLM service (listens for voice_transcript, publishes llm_response)
Write-Host "[5/6] llm-service (Module C)" -ForegroundColor Green
$env:ULTRON_TOKEN = $TOKEN
Start-Process -FilePath $VENV `
    -ArgumentList "$PY\llm_service.py" `
    -WorkingDirectory $PY `
    -WindowStyle Normal

Start-Sleep -Seconds 1

# 6 — Module B: Voice engine (STT → voice_transcript → waits for llm_response → TTS)
Write-Host "[6/6] voice-engine (Module B)" -ForegroundColor Green
Start-Process -FilePath $VENV `
    -ArgumentList "$PY\voice_engine.py" `
    -WorkingDirectory $PY `
    -WindowStyle Normal

Write-Host ""
Write-Host "All processes launched." -ForegroundColor Cyan
Write-Host "Hotkey: check config.toml [voice] hotkey to trigger recording." -ForegroundColor Yellow
Write-Host "To monitor events: $VENV $PY\bridge_test.py" -ForegroundColor DarkGray
