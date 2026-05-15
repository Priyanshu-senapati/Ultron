# ULTRON v5.1 — Windows Service uninstaller.

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

# Try to stop first if running.
sc.exe stop UltronCore 2>&1 | Out-Null
Start-Sleep -Seconds 1

if (Test-Path $BinaryPath) {
    & $BinaryPath --uninstall
    exit $LASTEXITCODE
} else {
    # Binary already gone; fall back to sc.exe delete.
    sc.exe delete UltronCore
    exit $LASTEXITCODE
}
