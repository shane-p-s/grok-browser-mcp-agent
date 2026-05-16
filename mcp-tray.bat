@echo off
REM One double-click: free PORT, then tray (or Grok-PC-MCP.exe if present).
REM Keeps this window only until setup finishes; tray has no console.
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0stop.ps1"

if exist "%~dp0Grok-PC-MCP.exe" (
  start "" "%~dp0Grok-PC-MCP.exe"
  exit /b 0
)

if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0mcp_tray.py"
  exit /b 0
)

where pyw >nul 2>&1
if %ERRORLEVEL% equ 0 (
  start "" pyw -3 "%~dp0mcp_tray.py"
  exit /b 0
)

start "" pythonw "%~dp0mcp_tray.py"
exit /b 0
