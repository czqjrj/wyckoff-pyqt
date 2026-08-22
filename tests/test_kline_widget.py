# -*- coding: utf-8 -*-
"""KlineWidget (pyqtgraph) 离屏冒烟测试: 渲染 + 三图 X 联动 + 交互 + 标签命中。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

from wyckoff.chart import build_kline_data
from wyckoff.config import EVENT_COLORS


def _df(n=120):
    rng = np.random.default_rng(7)
    closes = 50 + np.cumsum(rng.normal(0, 0.8, n))
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.002, n)),
        "close": closes * (1 + rng.normal(0, 0.002, n)),
        "high": closes * 1.015, "low": closes * 0.985,
        "volume": rng.uniform(1e5, 2e6, n),
        "price_ma5": pd.Series(closes).rolling(5, min_periods=1).mean().values,
        "price_ma10": pd.Series(closes).rolling(10, min_periods=1).mean().values,
        "price_ma20": pd.Series(closes).rolling(20, min_periods=1).mean().values,
        "price_ma50": pd.Series(closes).rolling(50, min_periods=1).mean().values,
        "price_ma200": pd.Series(closes).rolling(200, min_periods=1).mean().values,
        "vol_ratio_20": np.ones(n),
        "boll_up": closes * 1.06, "boll_dn": closes * 0.94,
    })


def _data(df):
    evs = [
        {"idx": 10, "type": "SC", "price": float(df["low"].iloc[10]),
         "color": EVENT_COLORS["SC"], "desc": "卖出高潮", "conf": 90},
        {"idx": 40, "type": "Spring", "price": float(df["low"].iloc[40]),
         "color": EVENT_COLORS["Spring"], "desc": "弹簧", "conf": 85},
        {"idx": len(df) - 5, "type": "JOC", "price": float(df["high"].iloc[-5]),
         "color": EVENT_COLORS["JOC"], "desc": "突破", "conf": 80},
        {"idx": len(df) - 2, "type": "LPS", "price": float(df["low"].iloc[-2]),
         "color": EVENT_COLORS["LPS"], "desc": "回踩", "conf": 75},
    ]
    pivots = [
        {"idx": 10, "type": "low", "price": float(df["low"].iloc[10])},
        {"idx": 60, "type": "high", "price": float(df["high"].iloc[60])},
    ]
    waves = [(10, float(df["low"].iloc[10]), "1"),
             (60, float(df["high"].iloc[60]), "2"),
             (len(df) - 5, float(df["low"].iloc[len(df) - 5]), "3")]
    vsa = [{"idx": 30, "label": "CHOC", "color": "#c92a2a"}]
    return build_kline_data(
        df, pivots, evs, "冒烟", waves=waves, draw_waves=True,
        draw_locks=True, tr={"top": float(df["high"].max()),
                             "bottom": float(df["low"].min())},
        profile={"poc": 55.0}, phase="accumulation",
        sector={"name": "汽车整车", "main20": 1e8}, vsa_signals=vsa)


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def widget():
    try:
        app = _app()
        from desktop.kline_widget import KlineWidget
    except Exception as e:  # 无 Qt/offscreen 环境
        pytest.skip(f"KlineWidget 不可用: {e}")
    df = _df()
    data = _data(df)
    w = KlineWidget(font_size=10)
    w.resize(900, 600)
    w.show()
    app.processEvents()
    w.set_data(**data)
    app.processEvents()
    return app, w, df


def test_kline_widget_renders(widget):
    """灌入完整数据后默认视野聚焦最近 N 根, reset_view 回到全幅, 标签可命中。"""
    app, w, df = widget
    x0, x1 = w.price_plot.getViewBox().viewRange()[0]
    n_view = min(len(df), 80)
    assert abs(x1 - (len(df) - 1)) < 1e-6
    assert abs(x1 - x0 - (n_view - 1)) < 1e-6, "默认视野应为最近80根"
    w.reset_view()
    app.processEvents()
    x0, x1 = w.price_plot.getViewBox().viewRange()[0]
    assert abs(x0 - 0) < 1e-6 and abs(x1 - (len(df) - 1)) < 1e-6
    hits = [it for it in w.scene().items()
            if type(it).__name__ == "TextItem" and getattr(it, "ev_label", None)]
    assert hits, "应有事件/VSA 文本标签"
    label, conf = w.hit_label(hits[0].sceneBoundingRect().center())
    assert label, "命中标签应返回 (label, conf)"
    assert "ev_conf" in hits[0].__dict__, "标签应携带置信度 (事件) 或 None (VSA)"


def test_kline_widget_x_sync(widget):
    """三栏 ViewBox X 联动: 缩放主图后量能与累计量跟随。"""
    app, w, df = widget
    w.zoom_x_about(len(df) / 2, 0.5)
    app.processEvents()
    rp = w.price_plot.getViewBox().viewRange()[0]
    rv = w.vol_plot.getViewBox().viewRange()[0]
    rc = w.cum_plot.getViewBox().viewRange()[0]
    assert abs(rv[0] - rp[0]) < 1e-6 and abs(rv[1] - rp[1]) < 1e-6
    assert abs(rc[0] - rp[0]) < 1e-6 and abs(rc[1] - rp[1]) < 1e-6


def test_kline_widget_arrow_zoom(widget):
    """上/下箭头以视图中心为锚点缩放 (基类统一交互)。"""
    app, w, df = widget
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    def key(k):
        return QKeyEvent(QEvent.Type.KeyPress, k,
                         Qt.KeyboardModifier.NoModifier)

    w.reset_view()
    app.processEvents()
    vb = w.price_plot.getViewBox()
    full_span = len(df) - 1
    center = sum(vb.viewRange()[0]) / 2
    w.keyPressEvent(key(Qt.Key.Key_Up))
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 - x0 == pytest.approx(full_span * 0.8), "上箭头应放大"
    assert sum((x0, x1)) / 2 == pytest.approx(center, abs=1e-6), \
        "放大应以视图中心为锚点"
    w.keyPressEvent(key(Qt.Key.Key_Down))
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 - x0 == pytest.approx(full_span), "下箭头应回到全幅"


def test_kline_widget_interactions(widget):
    """滚轮/键盘/复位/历史不抛异常且改变视图。"""
    app, w, df = widget
    from PyQt6.QtCore import QEvent, QPoint, QPointF
    from PyQt6.QtGui import QKeyEvent, QWheelEvent
    from PyQt6.QtCore import Qt

    w.reset_view()
    app.processEvents()
    full = w.price_plot.getViewBox().viewRange()[0]
    view = w.scene().views()[0]
    gpos = view.viewport().mapToGlobal(QPoint(100, 100))
    ev = QWheelEvent(QPointF(100, 100), QPointF(gpos), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    app.processEvents()
    z = w.price_plot.getViewBox().viewRange()[0]
    assert z[1] - z[0] < full[1] - full[0], "滚轮向上应缩小跨度"

    w.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Plus,
                              Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    w.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left,
                              Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    w.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Home,
                              Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    r = w.price_plot.getViewBox().viewRange()[0]
    assert abs(r[0] - 0) < 1e-6 and abs(r[1] - (len(df) - 1)) < 1e-6

    w.set_data(df=None, title="占位")
    app.processEvents()
    assert w._n == 0


def test_kline_widget_export_png(widget, tmp_path):
    """grab_pixmap 可导出 PNG。"""
    app, w, df = widget
    pm = w.grab_pixmap()
    out = tmp_path / "kline.png"
    assert pm.save(str(out))
    assert out.stat().st_size > 1000
