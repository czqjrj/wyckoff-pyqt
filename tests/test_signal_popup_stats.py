# -*- coding: utf-8 -*-
"""信号解释弹窗的胜率/置信度统计块 (MainWindow._signal_stats_html) 离屏测试。

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


def test_stats_shows_confidence_for_event(win, monkeypatch):
    """事件信号 (kind=event) 带置信度 → 显示 置信度 分档配色。"""
    import wyckoff.signal_accuracy as sa
    app, w = win
    monkeypatch.setattr(sa, "load_win_rates",
                        lambda horizon, force=False: {})
    html = w._signal_stats_html("SOS", conf=82)
    assert "信号置信度: " in html
    assert "82/100" in html


def test_stats_win_rate_two_horizons(win, monkeypatch):
    """历史胜率同时展示 5根/20根 及样本数。"""
    import wyckoff.signal_accuracy as sa
    app, w = win
    rates5 = {("event", "SOS"): {"n": 40, "win": 0.625, "mean": 0.03}}
    rates20 = {("event", "SOS"): {"n": 38, "win": 0.55, "mean": 0.04}}
    monkeypatch.setattr(sa, "load_win_rates",
                        lambda horizon, force=False: rates5 if horizon == 5 else rates20)
    html = w._signal_stats_html("SOS", conf=None)
    assert "历史胜率(5根): <b>62.5%</b> (样本40)" in html
    assert "历史胜率(20根): <b>55.0%</b> (样本38)" in html


def test_stats_insufficient_sample(win, monkeypatch):
    """样本 < 10 视为样本不足, 不展示百分数。"""
    import wyckoff.signal_accuracy as sa
    app, w = win
    rates5 = {("event", "SOS"): {"n": 3, "win": 1.0, "mean": 0.1}}
    monkeypatch.setattr(sa, "load_win_rates",
                        lambda horizon, force=False: rates5 if horizon == 5 else {})
    html = w._signal_stats_html("SOS", conf=None)
    assert "暂无足够的历史评估样本" in html
    assert "100.0%" not in html


def test_stats_vsa_no_confidence(win, monkeypatch):
    """VSA 标签 (kind=vsa) 无置信度, 只展示胜率或样本不足提示。"""
    from wyckoff import vsa_explain
    app, w = win
    monkeypatch.setattr(vsa_explain, "VSA_EXPLAIN",
                        {"BU": {"meaning": "x"}})
    monkeypatch.setattr(vsa_explain, "EVENT_EXPLAIN", {})
    import wyckoff.signal_accuracy as sa
    monkeypatch.setattr(sa, "load_win_rates",
                        lambda horizon, force=False: {})
    html = w._signal_stats_html("BU", conf=None)
    assert "信号置信度" not in html
    assert "暂无足够的历史评估样本" in html


def test_stats_unknown_label_defaults_event(win, monkeypatch):
    """未知标签回退 event 分支: 给置信度仍展示。"""
    import wyckoff.signal_accuracy as sa
    app, w = win
    monkeypatch.setattr(sa, "load_win_rates",
                        lambda horizon, force=False: {})
    html = w._signal_stats_html("未知XYZ", conf=60)
    assert "60/100" in html
