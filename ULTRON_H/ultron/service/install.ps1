# ULTRON v5.1 — Windows Service installer.
# Run from an elevated PowerShell, OR just run normally and accept the UAC prompt.

[CmdletBinding()]
param(
    [string]$BinaryPath = (Join-Path $PSScriptRoot '..\target\release\ultron-core.exe')
)

$ErrorActionPreference = 'Stop'

function Test-IsElevated {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsElevated)) {
    Write-Host "Re-launching as Administrator..." -ForegroundColor Yellow
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -BinaryPath `"$BinaryPath`""
    Start-Process powershell -Verb RunAs -ArgumentList $argLine
    exit 0
}

if (-not (Test-Path $BinaryPath)) {
    Write-Host "Binary not found at $BinaryPath" -ForegroundColor Red
    Write-Host "Build it first:    cargo build --release --workspace" -ForegroundColor Yellow
    exit 1
}

Write-Host "Installing ULTRON service from $BinaryPath ..." -ForegroundColor Cyan
& $BinaryPath --install
if ($LASTEXITCODE -ne 0) {
    Write-Host "Install failed (exit $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Starting service..." -ForegroundColor Cyan
sc.exe start UltronCore | Out-Null

Start-Sleep -Seconds 2
sc.exe query UltronCore
