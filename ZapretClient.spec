# -*- mode: python ; coding: utf-8 -*-
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, SPECPATH)
from build_support.policy import validate_binary_origins

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/zapret-client.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
audit = validate_binary_origins(a.binaries)
Path(os.environ['ZAPRET_BUILD_AUDIT']).write_text(
    json.dumps(audit, indent=2), encoding='utf-8')
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ZapretClient',
    icon=str(Path(SPECPATH) / 'assets' / 'zapret-client.ico'),
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
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ZapretClient',
)
