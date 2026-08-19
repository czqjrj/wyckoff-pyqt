# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/home/RuanJun/wyckoff-pyqt/wyckoff_desktop.py'],
    pathex=['/home/RuanJun/wyckoff-pyqt'],
    binaries=[
        ('/home/RuanJun/.unioncode/miniforge/envs/wyckoff-pyqt/lib/libssl.so.3', '.'),
        ('/home/RuanJun/.unioncode/miniforge/envs/wyckoff-pyqt/lib/libcrypto.so.3', '.'),
        ('/home/RuanJun/.local/lib/python3.12/site-packages/certifi/cacert.pem', 'certifi'),
    ],
    datas=[],
    hiddenimports=['certifi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['/home/RuanJun/wyckoff-pyqt/pyi_rth_ssl_cert.py'],
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
    name='wyckoff',
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='wyckoff',
)
