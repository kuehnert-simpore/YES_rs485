# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for the YES RS-485 desktop application.

The tiny launcher imports the package's existing ``yes_rs485.__main__.main``
entry point. It exists only because PyInstaller executes a script as
``__main__`` and the package's own relative import otherwise has no parent.
ttkbootstrap's theme assets are data files rather than Python modules, and
pyserial discovers the platform-specific port enumerators at runtime, so both
are collected explicitly here.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
ENTRY_POINT = PROJECT_ROOT / "packaging" / "launcher.py"


ttk_datas, ttk_binaries, ttk_hiddenimports = collect_all("ttkbootstrap")
# The application uses serial.tools.list_ports.comports().  list_ports selects
# the Windows backend by platform at import time; include those two modules
# explicitly instead of bundling pyserial's unrelated miniterm/OS backends.
serial_hiddenimports = [
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
]

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(SOURCE_ROOT)],
    binaries=ttk_binaries,
    datas=ttk_datas,
    hiddenimports=ttk_hiddenimports + serial_hiddenimports,
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
    name="YES_RS485",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
