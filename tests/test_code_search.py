# -*- coding: utf-8 -*-
"""键盘精灵 (CodeSearchDialog + MainWindow 全局事件过滤) 离屏测试。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

pyqt = pytest.importorskip("PyQt6")


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture(scope="module")
def win(app):
    from desktop.main_window import MainWindow
    w = MainWindow()
    w.settings = {**w.settings, "ai_interpret_enabled": False,
                  "ai_falsify_enabled": False, "confirm_enabled": False}
    w.show()
    for _ in range(10):
        app.processEvents()
    yield w
    w.close()
    # 等待所有可能仍在运行的后台线程结束, 避免其在后续测试期间调用
    # fetch_kline 写入 SQLite / 触发 monkeypatch, 造成缓存与计数污染。
    for _ in range(800):
        ths = []
        t0 = getattr(w, "_thread", None)
        if t0 is not None and t0.isRunning():
            ths.append(t0)
        for t in list(getattr(w, "_analysis_threads", {}) or {}):
            if t.isRunning():
                ths.append(t)
        for t in list(getattr(w, "_rt_threads", {}) or {}):
            if t.isRunning():
                ths.append(t)
        if not ths:
            break
        app.processEvents()
        time.sleep(0.05)


def _spirit_visible(win):
    return win._spirit is not None and win._spirit.isVisible()


def _close_spirit(win):
    if win._spirit is not None and win._spirit.isVisible():
        win._spirit.hide()
        for _ in range(5):
            QApplication.processEvents()


def _open_from_watch(win, key):
    win.watch_list.setFocus()
    for _ in range(5):
        QApplication.processEvents()
    QTest.keyClick(QApplication.focusWidget(), key)
    for _ in range(5):
        QApplication.processEvents()


def test_alphabet_on_non_input_opens_spirit(win, app):
    _close_spirit(win)
    _open_from_watch(win, Qt.Key.Key_S)
    assert _spirit_visible(win)
    assert win._spirit.entry.text() == "S"


def test_typing_in_input_does_not_open_spirit(win, app):
    _close_spirit(win)
    app.setActiveWindow(win)
    win.cb_scale.setFocus()
    for _ in range(5):
        app.processEvents()
    QTest.keyClick(win.cb_scale, Qt.Key.Key_S)
    for _ in range(5):
        app.processEvents()
    assert not _spirit_visible(win)


def test_query_lists_results_and_picks(win, app):
    _close_spirit(win)
    _open_from_watch(win, Qt.Key.Key_A)
    assert _spirit_visible(win)
    sp = win._spirit
    sp.entry.setText("sh")
    for _ in range(40):
        app.processEvents()
        time.sleep(0.05)
        if sp.result_list.count():
            break
    assert sp.result_list.count() > 0
    picked = []
    sp.picked.connect(lambda c: picked.append(c))
    sp.result_list.setCurrentRow(0)
    sp._confirm()
    for _ in range(5):
        app.processEvents()
    assert picked, "键盘精灵 confirm 应发出 picked 信号"


def test_escape_closes_spirit(win, app):
    _close_spirit(win)
    _open_from_watch(win, Qt.Key.Key_B)
    assert _spirit_visible(win)
    sp = win._spirit
    QTest.keyClick(sp.entry, Qt.Key.Key_Escape)
    for _ in range(5):
        app.processEvents()
    assert not _spirit_visible(win)
