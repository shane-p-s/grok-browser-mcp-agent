# Force-stop whatever is listening on MCP PORT (same .env rules as start.ps1). Use when Ctrl+C does not exit uvicorn.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    foreach ($raw in Get-Content $envFile) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $i = $line.IndexOf("=")
        if ($i -lt 1) { continue }
        $name = $line.Substring(0, $i).Trim()
        $val = $line.Substring($i + 1).Trim().Trim('"')
        Set-Item -Path "env:$name" -Value $val
    }
}

$bindPort = if ($env:PORT) { [int]$env:PORT } else { 8765 }

$conns = Get-NetTCPConnection -LocalPort $bindPort -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "No LISTEN on port $bindPort (nothing to stop)."
    exit 0
}

$pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($procId in $pids) {
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        Write-Host "Stop-Process -Id $procId ($($p.ProcessName)) — was listening on $bindPort"
        Stop-Process -Id $procId -Force
    } catch {
        Write-Host "Could not stop PID ${procId}: $_"
    }
}

Write-Host "Done. Close any leftover Chrome windows from browser_task if needed."
