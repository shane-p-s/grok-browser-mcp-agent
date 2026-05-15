@echo off
REM Pin this file to the taskbar: right-click -> Pin to taskbar (or create a shortcut to this file).
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-mcp.ps1"
