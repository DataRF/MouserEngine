# -*- mode: python ; coding: utf-8 -*-
# Compilación con PyInstaller:  pyinstaller --noconfirm --clean MouserEngine.spec
# Genera dist/MouserEngine.exe (Windows) o dist/MouserEngine (Linux), un solo archivo sin consola.

import sys
from pathlib import Path

root = Path(SPECPATH)
assets = root / "mouser_engine" / "assets"

a = Analysis(
    [str(root / "run_mouser_engine.py")],
    pathex=[str(root)],
    datas=[(str(assets), "mouser_engine/assets")],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pytest", "pytestqt", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtPdf", "PySide6.QtWebEngineCore", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MouserEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(assets / "icon.ico"),
    version=str(root / "packaging" / "version_info.txt") if sys.platform.startswith("win") else None,
)
