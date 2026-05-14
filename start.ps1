# Start MCP server on localhost (for Tailscale Funnel). Loads .env from this script's directory.
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

$bindHost = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$bindPort = if ($env:PORT) { $env:PORT } else { "8765" }

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }

Write-Host "Starting uvicorn on http://${bindHost}:${bindPort} (repo: $Root, python: $pythonExe)"
& $pythonExe -m uvicorn main:app --host $bindHost --port $bindPort
