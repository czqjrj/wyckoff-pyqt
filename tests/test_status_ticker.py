# -*- coding: utf-8 -*-
"""状态栏滚动头条 (自选股定时扫描高命中信号) 测试。

- build_ticker_msgs 纯函数: 事件/VSA 实测胜率阈值过滤、颜色、条数上限;
- _StatusTicker 组件: 消息设置/清空/轮播/点按 (离屏)。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6.QtWidgets import QWidget

from wyckoff.config import TICKER_MAX_ITEMS


def _rich(code="600104", name="上汽集团", events=None, vsa=None):
    return {code: {
        "name": name, "code": code, "phase": "A",
        "signals": [e["type"] for e in (events or [])],
        "events": events or [],
        "vsa": vsa or [],
    }}


def _ev(t, wr):
    return {"type": t, "date": "2026-08-20", "price": 10.0, "conf": 80, "_wr": wr}


def _vs(lb, wr):
    return {"label": lb, "date": "2026-08-20", "desc": lb, "_wr": wr}


@pytest.fixture
def fake_wr(monkeypatch):
    def _set(table):
        """table: {(kind,type): wr}"""
        def win_rate_of(kind, type_, horizon=20, baseline=0.5):
            return table.get((kind, str(type_)), baseline)
        monkeypatch.setattr("wyckoff.signal_accuracy.win_rate_of", win_rate_of)
    return _set


def test_event_threshold_filter(fake_wr):
    from desktop.main_window import build_ticker_msgs
    fake_wr({("event", "Spring"): 0.82, ("event", "SC"): 0.60,
             ("event", "BC"): 0.50, ("event", "UTAD"): 0.30})
    rich = _rich(events=[_ev("Spring", 0.82), _ev("SC", 0.60),
                         _ev("BC", 0.50), _ev("UTAD", 0.30)])
    msgs = build_ticker_msgs(rich)
    assert len(msgs) == 2, msgs  # 只有 Spring/SC 达标 (≥0.60)
    text0 = msgs[0][0]
    assert "Spring" in text0 and "82%" in text0
    # 只保留每只股票最高 2 条 → 这里已够
    assert all(any(k in m[0] for k in ("Spring", "SC")) for m in msgs)


def test_vsa_threshold_filter(fake_wr):
    from desktop.main_window import build_ticker_msgs
    fake_wr({("vsa", "SUP"): 0.56, ("vsa", "NS"): 0.48})
    rich = _rich(vsa=[_vs("SUP", 0.56), _vs("NS", 0.48)])
    msgs = build_ticker_msgs(rich)
    assert len(msgs) == 1, msgs       # SUP (0.56) ≥ VSA 阈值 0.55
    assert "SUP" in msgs[0][0] and "56%" in msgs[0][0]


def test_mixed_kinds_and_per_code_cap(fake_wr):
    from desktop.main_window import build_ticker_msgs
    fake_wr({("event", "Spring"): 0.82, ("event", "ST"): 0.71,
             ("event", "SOW"): 0.66, ("event", "LPS"): 0.63,
             ("vsa", "SV"): 0.56, ("vsa", "SC"): 0.55})
    rich = _rich(
        events=[_ev("Spring", 0.82), _ev("ST", 0.71), _ev("SOW", 0.66),
                _ev("LPS", 0.63)],
        vsa=[_vs("SV", 0.56), _vs("SC", 0.55)])
    msgs = build_ticker_msgs(rich)
    # 每只股票最多 2 条 → 事件 2 条最高, VSA 被挤出
    assert len(msgs) == 2, msgs
    assert "Spring" in msgs[0][0] and "ST" in msgs[1][0]


def test_global_max_items_cap(fake_wr):
    from desktop.main_window import build_ticker_msgs
    fake_wr({("event", "Spring"): 0.82})
    rich = {f"6001{i:02d}": {
        "name": f"股{i}", "code": f"6001{i:02d}",
        "events": [_ev("Spring", 0.82)], "vsa": []}
        for i in range(20)}
    msgs = build_ticker_msgs(rich)
    assert len(msgs) <= TICKER_MAX_ITEMS, msgs


def test_color_by_direction(fake_wr):
    import desktop.theme as tm
    from desktop.main_window import build_ticker_msgs
    fake_wr({("event", "Spring"): 0.82, ("event", "SOW"): 0.66,
             ("vsa", "SUP"): 0.56})
    rich = {
        "600001": {"name": "A", "events": [_ev("Spring", 0.82)], "vsa": []},
        "600002": {"name": "B", "events": [_ev("SOW", 0.66)], "vsa": []},
        "600003": {"name": "C", "events": [], "vsa": [_vs("SUP", 0.56)]},
    }
    msgs = build_ticker_msgs(rich)
    colors = {m[0].split("(")[0]: m[1] for m in msgs}
    assert colors["A"] == tm.C_UP      # 吸筹事件 → 红 (涨)
    assert colors["B"] == tm.C_DOWN    # 派发事件 → 绿 (跌)
    assert colors["C"] == tm.C_DOWN    # VSA 供应标签 (SUP) → 绿


# ── 组件测试 (离屏) ──

def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_ticker_set_clear_and_current():
    from desktop.main_window import _StatusTicker
    app = _app()
    tk = _StatusTicker()
    tk.setFixedWidth(300)
    tk.set_messages([("甲(600104) Spring 实测命中82%", "#d62728", "600104"),
                     ("乙(600519) ST 实测命中71%", "#d62728", "600519")])
    tk.flush_now()  # debounce 立即刷新, 确保消息落到 _msgs
    app.processEvents()
    assert tk.current_code() == "600104"
    tk._advance()
    assert tk.current_code() == "600519"
    tk.clear()
    tk.flush_now()
    app.processEvents()
    assert tk.current_code() == ""
    assert tk.text() == ""


def test_ticker_add_messages_merge_and_cap():
    from desktop.main_window import _StatusTicker
    app = _app()
    tk = _StatusTicker()
    tk.set_messages([("甲(600104) Spring 实测命中82%", "#d62728", "600104"),
                     ("乙(600519) ST 实测命中71%", "#d62728", "600519")])
    tk.flush_now()
    # 合并去重 (文本相同视为重复): 新消息在前, 旧重复项被剔除
    tk.add_messages([("乙(600519) ST 实测命中71%", "#d62728", "600519"),
                     ("丙(000858) SOW 实测命中66%", "#2f9e44", "000858")])
    tk.flush_now()
    texts = [m[0] for m in tk._msgs]
    assert texts == [("乙(600519) ST 实测命中71%", "#d62728", "600519")[0],
                     ("丙(000858) SOW 实测命中66%", "#2f9e44", "000858")[0],
                     ("甲(600104) Spring 实测命中82%", "#d62728", "600104")[0]], texts
    assert tk.current_code() == "600519"       # 新消息置顶
    # 上限截断: 塞入超过 TICKER_MAX_ITEMS 条
    many = [(f"股{i}(6001{i:02d}) Spring 实测命中82%", "#d62728", f"6001{i:02d}")
            for i in range(TICKER_MAX_ITEMS + 10)]
    tk.set_messages(many)
    tk.flush_now()
    assert len(tk._msgs) <= TICKER_MAX_ITEMS, len(tk._msgs)
    tk.add_messages(many)
    tk.flush_now()
    assert len(tk._msgs) <= TICKER_MAX_ITEMS, len(tk._msgs)


def test_mainwindow_has_center_ticker():
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        import desktop.main_window as mw
        orig_load = mw.load_settings
        orig_watch = mw.load_watchlist
        safe = {**mw.load_settings(),
                "default_load": "", "auto_refresh": False, "auto_scan": False,
                "refresh_interval": 0}
        mw.load_settings = lambda: safe
        mw.load_watchlist = lambda: []
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")
    try:
        w = MainWindow()
        w.resize(1600, 900)
        w.show()
        app.processEvents()
        assert hasattr(w, "status_ticker")
        # 头条已移至主工具栏: 刷新按钮与股票信息之间, 不再位于底部状态栏
        from desktop.main_window import _StatusTicker
        assert w.status_ticker.parent() is not w.statusBar()
        assert any(isinstance(x, _StatusTicker)
                   for x in w._top_bar.findChildren(QWidget))
    finally:
        try:
            w.close()
        except Exception:
            pass
        app.processEvents()
        mw.load_settings = orig_load
        mw.load_watchlist = orig_watch


def test_watch_scan_no_hit_placeholder():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    import desktop.main_window as mw
    import desktop.theme as tm
    orig_load = mw.load_settings
    orig_watch = mw.load_watchlist
    safe = {**mw.load_settings(),
            "default_load": "", "auto_refresh": False, "auto_scan": False,
            "refresh_interval": 0}
    mw.load_settings = lambda: safe
    mw.load_watchlist = lambda: []
    try:
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")
    w = None
    try:
        w = MainWindow()
        w.resize(1600, 900)
        w.show()
        app.processEvents()
        # 2 只自选股扫描完成, 无达标信号 → 灰色占位文案
        rich = {i: {"name": f"股{i}", "code": f"6001{i}", "events": [], "vsa": []}
                for i in "12"}
        w._on_watch_scan((True, {}, rich))
        w.status_ticker.flush_now()  # debounce 立即触发
        app.processEvents()
        assert w.status_ticker._msgs, "应有占位消息"
        text, color, code = w.status_ticker._msgs[0]
        assert "2 只扫描完成" in text and "暂无" in text
        assert color == tm.C_MUTED
        assert code == ""
    finally:
        try:
            if w is not None:
                w.close()
        except Exception:
            pass
        app.processEvents()
        mw.load_settings = orig_load
        mw.load_watchlist = orig_watch


def _wait_thread(th, app, timeout_ms=3000):
    """等待 QThread 完成, 中间持续 processEvents 防止死锁。"""
    from PyQt6.QtCore import QElapsedTimer
    t = QElapsedTimer()
    t.start()
    while th.isRunning() and t.elapsed() < timeout_ms:
        app.processEvents()
        th.msleep(10)
    return not th.isRunning()


def test_push_analysis_ticker_recent_hits(monkeypatch):
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    import desktop.main_window as mw
    orig_load = mw.load_settings
    orig_watch = mw.load_watchlist
    safe = {**mw.load_settings(),
            "default_load": "", "auto_refresh": False, "auto_scan": False,
            "refresh_interval": 0}
    mw.load_settings = lambda: safe
    mw.load_watchlist = lambda: []
    try:
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")

    def win_rate_of(kind, type_, horizon=20, baseline=0.5):
        return {"Spring": 0.82, "SOW": 0.66}.get(str(type_), baseline)
    monkeypatch.setattr("wyckoff.signal_accuracy.win_rate_of", win_rate_of)

    import pandas as pd
    import numpy as np

    def fake_find_pivots(df, order=6):
        return {"a": 0, "l": 1, "a2": 2, "l2": 3, "a3": 4, "l3": 5,
                "up_days": 3, "down_days": 2}

    def fake_detect_all(df, pivots):
        n = len(df)
        return [{"type": "Spring", "idx": n - 1, "date": df.index[-1],
                 "price": float(df["close"].iloc[-1]), "conf": 90}]

    def fake_vsa(df, scale=240):
        return [{"label": "SUP", "idx": len(df) - 1, "date": df.index[-1],
                 "desc": "supply", "price": float(df["close"].iloc[-1])}]
    monkeypatch.setattr("wyckoff.indicators.find_pivots", fake_find_pivots)
    monkeypatch.setattr("wyckoff.events.detect_all", fake_detect_all)
    monkeypatch.setattr("wyckoff.vsa.vsa_classify", fake_vsa)

    w = None
    try:
        w = MainWindow()
        w.resize(1600, 900)
        w.show()
        app.processEvents()
        idx = pd.date_range("2026-07-01", periods=30, freq="D")
        df = pd.DataFrame({"high": np.full(30, 12.0), "low": np.full(30, 10.0),
                           "open": np.full(30, 11.0), "close": np.full(30, 11.0),
                           "volume": np.full(30, 1e6), "amount": np.full(30, 1e7)},
                          index=idx)
        r = {"code": "sh600104", "name": "上汽集团", "df": df,
             "summary": {}, "sections": []}
        w._push_analysis_ticker(r)
        # 等后台分析 ticker 线程跑完
        th = getattr(w, "_analysis_ticker_th", None)
        assert th is not None, "_AnalysisTickerThread 未启动"
        assert _wait_thread(th, app), "分析 ticker 线程超时未完成"
        app.processEvents()
        w.status_ticker.flush_now()
        codes = [m[2] for m in w.status_ticker._msgs]
        assert any(c == "sh600104" for c in codes), codes
        assert any(m[2] == "sh600104" and "Spring" in m[0] for m in w.status_ticker._msgs)
        w.status_ticker.set_messages([])
        w.status_ticker.flush_now()
        assert w.status_ticker._msgs == []
    finally:
        try:
            if w is not None:
                w.close()
        except Exception:
            pass
        app.processEvents()
        mw.load_settings = orig_load
        mw.load_watchlist = orig_watch


def test_startup_ticker_scan_placeholder():
    """启动扫描开始前应先落到 '正在扫描' 占位, 再进入 _auto_scan_watchlist。"""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    import desktop.main_window as mw
    import desktop.theme as tm
    orig_load = mw.load_settings
    orig_watch = mw.load_watchlist
    safe = {**mw.load_settings(),
            "default_load": "", "auto_refresh": False, "auto_scan": False,
            "refresh_interval": 0}
    mw.load_settings = lambda: safe
    mw.load_watchlist = lambda: ["600104"]
    try:
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")
    w = None
    started = []
    try:
        w = MainWindow()
        w.resize(1600, 900)
        w.show()
        app.processEvents()
        w._auto_scan_watchlist = lambda: started.append(True)
        w._startup_ticker_scan()
        w.status_ticker.flush_now()  # debounce 立即刷新
        app.processEvents()
        assert started, "_startup_ticker_scan 应触发一次扫描"
        assert w.status_ticker._msgs
        text, color, code = w.status_ticker._msgs[0]
        assert "正在扫描" in text and "1 只" in text
        assert color == tm.C_MUTED
        # 已在扫 → 不重复启动
        w._scan_threads = {"th": object()}
        w._startup_ticker_scan()
        assert len(started) == 1
    finally:
        try:
            if w is not None:
                w.close()
        except Exception:
            pass
        app.processEvents()
        mw.load_settings = orig_load
        mw.load_watchlist = orig_watch