# -*- coding: utf-8 -*-
"""左右面板折叠功能 (MainWindow) 离屏测试。

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
                "refresh_interval": 0,
                "left_panel_visible": True,
                "right_panel_visible": True}
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


def test_panels_visible_by_default(win):
    """初始左右面板均可见, 菜单勾选状态与视图一致。"""
    app, w = win
    assert w.watch_panel.isVisible()
    assert w.right_panel.isVisible()
    assert w.act_toggle_watch.isChecked()
    assert w.act_toggle_right.isChecked()


def test_collapse_left_panel(win):
    """折叠左栏: 面板隐藏, 设置与菜单勾选同步, 宽度被记住。"""
    app, w = win
    w.toggle_panel("watch", True)
    app.processEvents()
    w.toggle_panel("watch", False)
    app.processEvents()
    assert not w.watch_panel.isVisible()
    assert not w.act_toggle_watch.isChecked()
    assert w.settings.get("left_panel_visible") is False
    assert w._panel_widths.get("watch", 0) > 0
    w.toggle_panel("watch", True)
    app.processEvents()


def test_collapse_right_panel(win):
    """折叠右栏: 面板隐藏, 设置与菜单勾选同步, 宽度被记住。"""
    app, w = win
    w.toggle_panel("right", True)
    app.processEvents()
    w.toggle_panel("right", False)
    app.processEvents()
    assert not w.right_panel.isVisible()
    assert not w.act_toggle_right.isChecked()
    assert w.settings.get("right_panel_visible") is False
    assert w._panel_widths.get("right", 0) > 0
    w.toggle_panel("right", True)
    app.processEvents()


def test_collapse_buttons_exist(win):
    """左右面板标题栏有折叠按钮, 且绑定到正确的折叠动作。"""
    from PyQt6.QtWidgets import QPushButton
    app, w = win
    watch_btn = {b.text() for b in w.watch_panel.findChildren(QPushButton) if b.toolTip()}
    assert "«" in watch_btn, "自选股标题栏应有折叠按钮 «"
    right_btn = {b.text() for b in w.right_panel.findChildren(QPushButton) if b.toolTip()}
    assert "»" in right_btn, "信号汇总标题栏应有折叠按钮 »"


def test_apply_panel_state_restores(win):
    """_apply_panel_state 按设置恢复: 右栏折叠时启动应保持隐藏。"""
    app, w = win
    w.settings["left_panel_visible"] = True
    w.settings["right_panel_visible"] = False
    w._apply_panel_state()
    app.processEvents()
    assert w.watch_panel.isVisible()
    assert not w.right_panel.isVisible()
    assert not w.act_toggle_right.isChecked()
    w.settings["right_panel_visible"] = True
    w._apply_panel_state()
    app.processEvents()
    assert w.right_panel.isVisible()


def test_toolbar_toggle_buttons_sync(win):
    """工具栏「左栏/右栏」按钮随面板状态同步勾选, 折叠后仍可一键打开。"""
    app, w = win
    assert hasattr(w, "btn_toggle_watch")
    assert hasattr(w, "btn_toggle_right")
    assert w.btn_toggle_watch.isChecked()
    assert w.btn_toggle_right.isChecked()
    w.toggle_panel("watch", False)
    app.processEvents()
    assert not w.btn_toggle_watch.isChecked()
    assert not w.watch_panel.isVisible()
    # 工具栏按钮永远可见 → 直接点击即可重新打开
    w.btn_toggle_watch.click()
    app.processEvents()
    assert w.btn_toggle_watch.isChecked()
    assert w.watch_panel.isVisible()
    w.toggle_panel("right", False)
    app.processEvents()
    assert not w.btn_toggle_right.isChecked()
    assert not w.right_panel.isVisible()
    w.btn_toggle_right.click()
    app.processEvents()
    assert w.right_panel.isVisible()


def test_apply_panel_state_does_not_corrupt_settings(win):
    """回归: 启动恢复面板状态时不得改写 settings 里的可见性字段。

    曾因 toggle_panel 内部 _remember_panel_state 在窗口未显示时把
    right_panel_visible 误写为 False, 导致"下一次启动右栏被折叠"。
    """
    app, w = win
    w.settings["left_panel_visible"] = True
    w.settings["right_panel_visible"] = True
    w._apply_panel_state()
    app.processEvents()
    # 应用前后 settings 的可见性字段保持不变 (不被当前可见性覆盖)
    assert w.settings.get("left_panel_visible") is True
    assert w.settings.get("right_panel_visible") is True
    assert w.watch_panel.isVisible()
    assert w.right_panel.isVisible()


def test_startup_restores_collapsed_panel(tmp_path, monkeypatch):
    """退出时折叠右栏 → 下次启动保持折叠 (端到端恢复)。"""
    import desktop.main_window as mw
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    saved = {}
    monkeypatch.setattr(mw, "save_settings", lambda s: saved.update(s))
    safe = {**mw.load_settings(),
            "default_load": "", "auto_refresh": False,
            "left_panel_visible": True, "right_panel_visible": False,
            "start_maximized": False}
    monkeypatch.setattr(mw, "load_settings", lambda: safe)
    monkeypatch.setattr(mw, "load_watchlist", lambda: [])
    from desktop.main_window import MainWindow
    w = MainWindow()
    w.resize(1600, 900)
    w.show()
    app.processEvents()
    assert not w.right_panel.isVisible(), "上次退出右栏折叠, 本次启动应保持隐藏"
    assert not w.act_toggle_right.isChecked()
    assert w.watch_panel.isVisible()
    w.close()
    app.processEvents()
