# Stop listener on PORT, then start MCP (same folder as this script). Pin restart-mcp.bat to the taskbar.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== grok-browser-mcp-agent: restart ===" -ForegroundColor Cyan
Write-Host "Repo: $Root"

$stopScript = Join-Path $Root "stop.ps1"
$startScript = Join-Path $Root "start.ps1"

if (-not (Test-Path $stopScript)) { throw "Missing stop.ps1" }
if (-not (Test-Path $startScript)) { throw "Missing start.ps1" }

& $stopScript
Start-Sleep -Seconds 1
& $startScript
