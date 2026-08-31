# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

路径全部动态推导 (相对 spec 文件 / 当前解释器环境), 不再硬编码本机绝对路径,
保证其他开发者/CI 克隆后可直接 `pyinstaller wyckoff.spec` 构建。
"""
import os
import sys

# spec 文件所在目录 = 项目根目录 (PyInstaller 注入 SPEC 变量为 spec 文件绝对路径)
_ROOT = os.path.dirname(os.path.abspath(SPEC)) if 'SPEC' in dir() else os.getcwd()
# 当前解释器环境的 lib 目录 (conda env / venv / 系统 python 均适用)
_ENV_LIB = os.path.join(sys.prefix, 'lib')

# certifi 的 cacert.pem 路径动态查找 (不依赖固定 site-packages 路径)
try:
    import certifi
    _CACERT = certifi.where()
except Exception:
    _CACERT = None


def _bin(*parts):
    """拼接环境 lib 目录下的二进制路径, 不存在则返回 None (跳过打包该项)。"""
    p = os.path.join(_ENV_LIB, *parts)
    return p if os.path.exists(p) else None


binaries = []
for _libname in ('libssl.so.3', 'libcrypto.so.3'):
    _p = _bin(_libname)
    if _p:
        binaries.append((_p, '.'))
if _CACERT:
    binaries.append((_CACERT, 'certifi'))

datas = []
hiddenimports = ['certifi']
hookspath = []
hooksconfig = {}
runtime_hooks = [os.path.join(_ROOT, 'pyi_rth_ssl_cert.py')]
excludes = []
noarchive = False
optimize = 0


a = Analysis(
    [os.path.join(_ROOT, 'wyckoff_ui.py')],
    pathex=[_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=hookspath,
    hooksconfig=hooksconfig,
    runtime_hooks=runtime_hooks,
    excludes=excludes,
    noarchive=noarchive,
    optimize=optimize,
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
