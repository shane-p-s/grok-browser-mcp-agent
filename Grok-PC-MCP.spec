# -*- mode: python ; coding: utf-8 -*-
# Portable spec: run from repo root: pyinstaller Grok-PC-MCP.spec
# pystray loads pystray._win32 dynamically; PyInstaller needs an explicit hook via collect_all.

from PyInstaller.utils.hooks import collect_all

datas: list = []
binaries: list = []
hiddenimports: list = ["pystray._win32", "six"]

for _pkg in ("pystray", "PIL"):
    d, b, h = collect_all(_pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["mcp_tray.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Grok-PC-MCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
