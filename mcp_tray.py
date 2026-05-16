"""
Windows system-tray supervisor for the MCP server: uvicorn runs in the background
with output to logs/mcp-server.log. Use the tray icon (notification area) for
Restart / Stop / Open folder / Open log / Exit.

On first run, creates .venv if needed, pip-installs requirements, and (Windows) builds
Grok-PC-MCP.exe once for taskbar pin—no manual build script unless you set GROK_TRAY_NO_AUTO_EXE=1.
Double-click mcp-tray.bat or run Grok-PC-MCP.exe after it appears beside main.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO


def _repo_root() -> Path:
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _repo_root()
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "mcp-server.log"
SETUP_LOG = LOG_DIR / "tray-exe-build.log"
REQ_FILE = ROOT / "requirements.txt"
TASKBAR_EXE = ROOT / "Grok-PC-MCP.exe"
PYINSTALLER_SPEC = ROOT / "Grok-PC-MCP.spec"

_server_proc: subprocess.Popen | None = None
_server_log_f: TextIO | None = None


def _win_nowindow() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _load_env() -> None:
    from dotenv import load_dotenv

    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _run_stop_script() -> None:
    stop_ps = ROOT / "stop.ps1"
    if not stop_ps.is_file():
        return
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


def _venv_python() -> Path | None:
    exe = ROOT / ".venv" / "Scripts" / "python.exe"
    return exe if exe.is_file() else None


def _bootstrap_venv() -> bool:
    vdir = ROOT / ".venv"
    py_launcher = shutil.which("py")
    if py_launcher:
        r = subprocess.run(
            [py_launcher, "-3", "-m", "venv", str(vdir)],
            cwd=str(ROOT),
            creationflags=_win_nowindow(),
        )
        if r.returncode == 0 and _venv_python():
            return True
    for name in ("python", "python3"):
        py = shutil.which(name)
        if not py:
            continue
        r = subprocess.run(
            [py, "-m", "venv", str(vdir)],
            cwd=str(ROOT),
            creationflags=_win_nowindow(),
        )
        if r.returncode == 0 and _venv_python():
            return True
    return False


def _ensure_venv_and_requirements() -> None:
    if not REQ_FILE.is_file():
        return
    vp = _venv_python()
    if vp is None:
        if not _bootstrap_venv():
            msg = (
                "Could not create .venv. Install Python 3 from python.org "
                "or the Windows py launcher, then try again."
            )
            _fatal_gui(msg)
            sys.exit(1)
        vp = _venv_python()
    assert vp is not None
    r = subprocess.run(
        [
            str(vp),
            "-m",
            "pip",
            "install",
            "-q",
            "--disable-pip-version-check",
            "-r",
            str(REQ_FILE),
        ],
        cwd=str(ROOT),
        creationflags=_win_nowindow(),
    )
    if r.returncode != 0:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as lf:
            lf.write(f"\n--- pip install failed rc={r.returncode} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        _fatal_gui(
            f"pip install failed (exit {r.returncode}). Open the repo folder and run:\n"
            f".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        )
        sys.exit(1)


def _maybe_build_taskbar_exe() -> None:
    """One-time PyInstaller build so Grok-PC-MCP.exe exists for taskbar pin (optional skip via env)."""
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return
    if TASKBAR_EXE.is_file():
        return
    skip = os.getenv("GROK_TRAY_NO_AUTO_EXE", "").strip().lower()
    if skip in ("1", "true", "yes"):
        return
    vp = _venv_python()
    if vp is None:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    def slog(line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(SETUP_LOG, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")

    script = ROOT / "mcp_tray.py"
    if not script.is_file():
        return
    if not PYINSTALLER_SPEC.is_file():
        slog("Grok-PC-MCP.spec missing; cannot auto-build .exe.")
        return

    slog("Starting auto-build of Grok-PC-MCP.exe (typically 1-3 minutes, may be longer).")
    pip_pi = subprocess.run(
        [str(vp), "-m", "pip", "install", "-q", "pyinstaller"],
        cwd=str(ROOT),
        creationflags=_win_nowindow(),
    )
    if pip_pi.returncode != 0:
        slog(f"pip install pyinstaller failed (exit {pip_pi.returncode}); tray still runs from Python.")
        return

    work = ROOT / "build" / "pyinstaller"
    try:
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        slog(f"Could not prepare build folder: {e}")
        return

    try:
        r = subprocess.run(
            [
                str(vp),
                "-m",
                "PyInstaller",
                "--distpath",
                str(ROOT),
                "--workpath",
                str(work),
                "--clean",
                "--noconfirm",
                str(PYINSTALLER_SPEC),
            ],
            cwd=str(ROOT),
            creationflags=_win_nowindow(),
            timeout=1200,
        )
    except subprocess.TimeoutExpired:
        slog("PyInstaller timed out after 20 minutes; you can run scripts/build_grok_pc_mcp_exe.ps1 manually.")
        return
    if r.returncode != 0:
        slog(f"PyInstaller failed (exit {r.returncode}); see PowerShell build script or fix errors above. Tray still runs.")
        return
    if TASKBAR_EXE.is_file():
        slog("Grok-PC-MCP.exe is ready; next launch can use the .exe for taskbar pin.")
    else:
        slog("PyInstaller reported success but Grok-PC-MCP.exe is missing.")


def _fatal_gui(msg: str) -> None:
    print(msg, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, "Grok MCP", 0x10)
        except Exception:
            pass


def _win_single_instance() -> bool:
    """Return True if we should continue; False if another tray instance is running."""
    if sys.platform != "win32":
        return True
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetLastError(0)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW(None, True, "Local\\GrokBrowserMcpTray_1")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    return True


def _python_exe() -> str:
    v = _venv_python()
    if v is not None:
        return str(v)
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
    _run_stop_script()


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
    if sys.platform == "win32" and not _win_single_instance():
        sys.exit(0)

    _ensure_venv_and_requirements()
    _load_env()
    _maybe_build_taskbar_exe()
    _run_stop_script()

    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError as e:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            import traceback

            with open(LOG_DIR / "tray-import-error.log", "w", encoding="utf-8") as ef:
                traceback.print_exc(file=ef)
        except OSError:
            pass
        if getattr(sys, "frozen", False):
            msg = (
                "Grok-PC-MCP.exe is missing bundled tray modules (PyInstaller).\n\n"
                f"Detail: {e}\n\n"
                "Delete Grok-PC-MCP.exe in this folder, then double-click "
                "mcp-tray.bat to rebuild the .exe. Or run "
                ".\\scripts\\build_grok_pc_mcp_exe.ps1"
            )
        else:
            msg = (
                f"pystray failed to import: {e}\n\n"
                "Try: .venv\\Scripts\\python.exe -m pip install -r requirements.txt"
            )
        _fatal_gui(msg)
        sys.exit(1)

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
