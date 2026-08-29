#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Wyckoff 分析客户端 (PyQt6 版) 入口。

用法:
    conda run -n wyckoff-pyqt python wyckoff_ui.py
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
    # 仅预加载 Core/Gui/Widgets/DBus 不够: PyQt6/pyqtgraph 运行时还会
    # 加载 Network/Svg/Test 等其余 Qt 模块, conda Python 自带的
    # RPATH=$ORIGIN/../lib 优先级高于 LD_LIBRARY_PATH, 这些库会解析到
    # conda 环境 (如 6.8.3), 与系统 Qt (如 6.8.0) 版本不一致直接 abort:
    # "Cannot mix incompatible Qt library"。这里把常用模块一并预加载,
    # 强制全进程统一使用系统 Qt。
    preloads = [f"{lib}/libQt6{name}.so.6"
                for name in ("Core", "Gui", "Widgets", "DBus",
                             "OpenGL", "XcbQpa", "Network", "Svg",
                             "SvgWidgets", "OpenGLWidgets", "PrintSupport",
                             "Xml", "Test", "WaylandClient")]
    old = [p for p in env.get("LD_PRELOAD", "").split(":") if p]
    env["LD_PRELOAD"] = ":".join(preloads + old)
    # LD_PRELOAD 只覆盖了 Core/Gui/Widgets/DBus; xcb 插件还会 dlopen
    # libQt6XcbQpa 等其余 Qt 库, 若解析到 conda/轮子版本会因私有 ABI
    # 不匹配而加载失败。把系统库目录放到 LD_LIBRARY_PATH 最前面,
    # 保证后续加载的 Qt 库同样来自系统。
    old_lp = [p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p]
    if getattr(sys, 'frozen', False):
        env["LD_LIBRARY_PATH"] = ":".join([sys._MEIPASS, lib] + old_lp)
    else:
        env["LD_LIBRARY_PATH"] = ":".join([lib] + old_lp)
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
    from ui.main_window import main
    main()


if __name__ == "__main__":
    sys.exit(main())
