# Optional manual rebuild (same as mcp_tray auto-build on first run).
# Auto-build: first tray start creates Grok-PC-MCP.exe unless GROK_TRAY_NO_AUTO_EXE=1.
# Uses Grok-PC-MCP.spec (pystray._win32 + collect_all) so the .exe includes the Win32 tray backend.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Error "No .venv found. Double-click mcp-tray.bat once (or: py -3 -m venv .venv ; .\.venv\Scripts\pip install -r requirements.txt), then re-run this script."
}

& $VenvPy -m pip install -q pyinstaller
$Work = Join-Path $Root "build\pyinstaller"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Path $Work -Force | Out-Null

$Spec = Join-Path $Root "Grok-PC-MCP.spec"
if (-not (Test-Path $Spec)) {
    Write-Error "Grok-PC-MCP.spec not found in repo root."
}

& $VenvPy -m PyInstaller --distpath $Root --workpath $Work --clean --noconfirm $Spec

$BuiltExe = Join-Path $Root "Grok-PC-MCP.exe"
Write-Host "Built: $BuiltExe - keep this file in the repo folder with main.py, stop.ps1, and your .env file."
