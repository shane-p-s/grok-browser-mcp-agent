"""
Windows system-tray supervisor for the MCP server: uvicorn runs in the background
with output to logs/mcp-server.log. Use the tray icon (notification area) for
Restart / Stop / Open folder / Open log / Exit.

Dependencies: pip install pystray pillow
Launch: mcp-tray.bat — pin to taskbar or add to shell:startup for login run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "mcp-server.log"

_server_proc: subprocess.Popen | None = None
_server_log_f: TextIO | None = None


def _load_env() -> None:
    from dotenv import load_dotenv

    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _win_nowindow() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _python_exe() -> str:
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def stop_mcp() -> None:
    global _server_proc, _server_log_f
    p = _server_proc
    _server_proc = None
    if p is not None:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    lf = _server_log_f
    _server_log_f = None
    if lf is not None:
        try:
            lf.flush()
            lf.close()
        except OSError:
            pass
    stop_ps = ROOT / "stop.ps1"
    if stop_ps.is_file():
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(stop_ps),
            ],
            cwd=str(ROOT),
            creationflags=_win_nowindow(),
            timeout=120,
        )


def start_mcp() -> tuple[bool, str]:
    """Stop anything on PORT, then start uvicorn (no console window)."""
    global _server_proc, _server_log_f
    stop_mcp()
    _load_env()
    host = (os.getenv("HOST") or "127.0.0.1").strip()
    port = (os.getenv("PORT") or "8765").strip()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    log_f.write(f"\n--- mcp_tray start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_f.flush()
    try:
        _server_proc = subprocess.Popen(
            [_python_exe(), "-m", "uvicorn", "main:app", "--host", host, "--port", port],
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=_win_nowindow(),
        )
    except OSError as e:
        log_f.write(f"spawn error: {e}\n")
        log_f.close()
        return False, str(e)
    time.sleep(0.4)
    if _server_proc.poll() is not None:
        code = _server_proc.poll()
        log_f.close()
        return False, f"uvicorn exited immediately (code {code}) — see {LOG_FILE}"
    _server_log_f = log_f
    return True, f"{host}:{port} (log: {LOG_FILE.name})"


def _open_repo() -> None:
    if sys.platform == "win32":
        os.startfile(str(ROOT))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(ROOT)], check=False)


def _open_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.is_file():
        LOG_FILE.write_text("", encoding="utf-8")
    if sys.platform == "win32":
        os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(LOG_FILE)], check=False)


def _tray_icon_image():
    from PIL import Image, ImageDraw

    w, h = 64, 64
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(52, 120, 200, 255))
    dr.rectangle([20, 38, 44, 44], fill=(255, 255, 255, 255))
    return img


def main() -> None:
    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError:
        msg = "Missing dependencies. Run: pip install pystray pillow"
        print(msg, file=sys.stderr)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, msg, "Grok MCP tray", 0x10)
            except Exception:
                pass
        else:
            input("Press Enter to close...")
        sys.exit(1)

    _load_env()
    icon_holder: dict[str, object] = {"icon": None}

    def notify(title: str, message: str) -> None:
        ic = icon_holder["icon"]
        if ic is not None:
            try:
                ic.notify(message, title=title)  # type: ignore[union-attr]
            except Exception:
                pass

    def do_start_safe() -> None:
        ok, msg = start_mcp()
        notify("Grok MCP — started" if ok else "Grok MCP — error", msg[:350] if ok else msg[:500])

    def on_restart(_icon, _item) -> None:
        threading.Thread(target=do_start_safe, daemon=True).start()

    def on_stop(_icon, _item) -> None:
        def work() -> None:
            stop_mcp()
            notify("Grok MCP", "Stopped.")

        threading.Thread(target=work, daemon=True).start()

    def on_exit(icon, _item) -> None:
        stop_mcp()
        icon.stop()

    menu = Menu(
        MenuItem("Restart MCP", on_restart),
        MenuItem("Stop MCP", on_stop),
        Menu.SEPARATOR,
        MenuItem("Open repo folder", lambda _i, _it: _open_repo()),
        MenuItem("Open server log", lambda _i, _it: _open_log()),
        Menu.SEPARATOR,
        MenuItem("Exit (stops MCP)", on_exit),
    )

    image = _tray_icon_image()
    icon = pystray.Icon(
        "grok_browser_mcp_tray",
        image,
        "Grok MCP Agent — right-click for menu",
        menu,
    )
    icon_holder["icon"] = icon

    def boot() -> None:
        time.sleep(0.6)
        do_start_safe()

    threading.Thread(target=boot, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
