@echo off
REM System tray for Grok MCP: no foreground console; uvicorn runs in background.
REM Uses pythonw when available so this script does not keep a console open.
REM Install tray deps once: pip install pystray pillow  (also in requirements.txt)
REM Right-click the tray icon -> Restart / Stop / Open folder / Open log / Exit.
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  "%~dp0.venv\Scripts\pythonw.exe" "%~dp0mcp_tray.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0mcp_tray.py"
) else (
  pythonw "%~dp0mcp_tray.py" 2>nul
  if errorlevel 1 py -3 "%~dp0mcp_tray.py"
)
