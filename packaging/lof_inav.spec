# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).parent
seed_database = project_root / "build" / "portable_seed" / "lof_inav.sqlite3"

if not seed_database.is_file():
    raise FileNotFoundError(
        "Portable seed database is missing. Run scripts\\build_portable.ps1."
    )


a = Analysis(
    [str(project_root / "lof_inav_desktop.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "public"), "public"),
        (str(project_root / "config" / "fund_rules.json"), "config"),
        (str(seed_database), "seed"),
    ],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name="LOF_iNAV",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LOF_iNAV",
)
