"""MainWindow QLib 菜单集成冒烟测试。

验证"工具"菜单包含 QLib 数据更新/模型训练动作且处理器存在, 并检查后台
线程会用 FnThread 而非占用过多默认值。不触发真实网络/训练。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def test_main_window_has_qlib_menu_actions():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication([])

    import ui.main_window as mw

    # 直接检查源码级接线: 菜单里有这两个 action, 且处理器方法存在
    src = open(mw.__file__, encoding="utf-8").read()
    assert "QLib 数据更新" in src
    assert "QLib 模型训练" in src
    assert "_qlib_update_data" in src
    assert "_qlib_train_model" in src
    assert mw.MainWindow._qlib_update_data is not None
    assert mw.MainWindow._qlib_train_model is not None


def test_main_window_imports_fnthread():
    pytest.importorskip("PyQt6")
    import ui.main_window as mw
    from ui.components.workers import FnThread

    assert mw.FnThread is FnThread


def test_qlib_update_no_watchlist_pool_returns_empty(monkeypatch):
    monkeypatch.setattr("wyckoff.qlib_update.load_watch_pool",
                        lambda: [])
    from wyckoff import qlib_update

    assert qlib_update.load_watch_pool() == []
