#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Wyckoff 分析客户端 (PyQt6 版) 入口。

用法:
    conda run -n wyckoff-pyqt python wyckoff_desktop.py
"""
import os
import sys


def _fix_linux_ime():
    """Linux 下中文输入法 (fcitx5/ibus) 修复。

    问题: PyQt6 轮子自带的 Qt 与发行版系统 Qt 版本不一致 (如 Deepin 24
    系统 Qt 6.8 vs 轮子 Qt 6.11), 系统的 fcitx5 输入法插件按系统 Qt 的
    私有 ABI 编译, 加载进轮子 Qt 进程时因 Qt_6_PRIVATE_API 符号不匹配
    而失败 → 所有文本框无法调出输入法、打不了中文。

    解决: 在导入任何 Qt 模块之前, 用 LD_PRELOAD 把进程统一切到系统 Qt,
    并让插件目录也指向系统 Qt 插件 (与 fcitx5-frontend-qt6 完全配套)。
    通过 re-exec 自身实现一次; 无系统 Qt 或非 Linux 时静默跳过。
    """
    if sys.platform != "linux" or os.environ.get("WYCKOFF_IME_FIXED") == "1":
        return
    lib = "/usr/lib/x86_64-linux-gnu"
    plugins = f"{lib}/qt6/plugins"
    if not (os.path.isdir(plugins)
            and os.path.exists(f"{lib}/libQt6Core.so.6")):
        return
    env = os.environ.copy()
    preloads = [f"{lib}/libQt6{name}.so.6"
                for name in ("Core", "Gui", "Widgets", "DBus")]
    old = [p for p in env.get("LD_PRELOAD", "").split(":") if p]
    env["LD_PRELOAD"] = ":".join(preloads + old)
    env["QT_PLUGIN_PATH"] = plugins + ":" + env["QT_PLUGIN_PATH"] \
        if env.get("QT_PLUGIN_PATH") else plugins
    env.setdefault("QT_IM_MODULE", "fcitx")
    env["WYCKOFF_IME_FIXED"] = "1"
    # 用 /proc/self/cmdline 而非 sys.argv: 后者在 `python -c` 下只有
    # ['-c'], 会丢失代码串导致 re-exec 失败。
    try:
        with open("/proc/self/cmdline", "rb") as f:
            raw = f.read().split(b"\0")[:-1]
        argv = [a.decode("utf-8", "surrogateescape") for a in raw]
    except OSError:
        argv = None
    if not argv or argv[0] != sys.executable:
        argv = [sys.executable] + sys.argv
    os.execve(sys.executable, argv, env)


_fix_linux_ime()


def main():
    from desktop.main_window import main
    main()


if __name__ == "__main__":
    sys.exit(main())
