# -*- coding: utf-8 -*-
"""数据源健康度状态栏 (MainWindow) 离屏测试。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def win():
    try:
        app = _app()
        import desktop.main_window as mw
        orig_load = mw.load_settings
        orig_watch = mw.load_watchlist
        safe = {**mw.load_settings(),
                "default_load": "",
                "auto_refresh": False,
                "refresh_interval": 0}
        mw.load_settings = lambda: safe
        mw.load_watchlist = lambda: []
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")
    w = MainWindow()
    w.resize(1600, 900)
    w.show()
    app.processEvents()
    yield app, w
    w.close()
    app.processEvents()
    mw.load_settings = orig_load
    mw.load_watchlist = orig_watch


def test_source_health_empty(win):
    """无请求记录时健康度标签为空。"""
    app, w = win
    assert hasattr(w, "source_health_label")
    from wyckoff.datasource import reset_source_health
    reset_source_health()
    w._update_source_health()
    app.processEvents()
    assert w.source_health_label.text() == ""


def test_source_health_renders(win):
    """有健康记录时按源名渲染 成功/总次数, 并按成功率着色。"""
    app, w = win
    from wyckoff.datasource import _health_hit, reset_source_health
    reset_source_health()
    for _ in range(3):
        _health_hit("新浪", True)
    _health_hit("腾讯", False, "timeout")
    _health_hit("东方财富", True)
    w._update_source_health()
    app.processEvents()
    text = w.source_health_label.text()
    assert "数据源" in text
    assert "新浪" in text and "3/3" in text
    assert "腾讯" in text and "0/1" in text
    assert "东方财富" in text and "1/1" in text
    assert "color" in text, "应包含颜色标记"
