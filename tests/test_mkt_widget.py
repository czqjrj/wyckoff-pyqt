# -*- coding: utf-8 -*-
"""MktWidget (pyqtgraph 资金透视) 离屏冒烟测试: 渲染 + 交互 + 导出。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

from wyckoff.chart import build_market_data


def _market(n_flow=60, n_holder=40, n_sd=30):
    rng = np.random.default_rng(7)
    days = pd.date_range("2024-01-01", periods=n_flow, freq="D")
    main_flow_series = []
    for d in days:
        main_flow_series.append({
            "day": d,
            "main": float(rng.normal(0.3e8, 1.2e8)),
            "super": float(rng.normal(0.2e8, 0.5e8)),
            "large": float(rng.normal(0.1e8, 0.4e8)),
            "mid": float(rng.normal(-0.05e8, 0.3e8)),
            "small": float(rng.normal(-0.1e8, 0.3e8)),
        })
    chip_dist = {
        "prices": [float(v) for v in np.linspace(15, 30, 30)],
        "weights": [float(v) for v in np.random.dirichlet(np.ones(30))],
        "cur": 22.5, "poc": 24.0, "below": 0.42,
    }
    holder_series = []
    for i, d in enumerate(pd.date_range("2023-03-31", periods=n_holder, freq="QE")):
        pre_num = float(rng.uniform(5e4, 8e4))
        holder_num = pre_num * (1 + rng.normal(0, 0.02))
        holder_series.append({
            "end_date": d, "pre_date": d - pd.DateOffset(months=3),
            "holder_num": holder_num, "pre_num": pre_num,
            "ratio": (holder_num - pre_num) / pre_num * 100,
        })
    sd_days = pd.date_range("2024-01-01", periods=n_sd, freq="D")
    sd_series = [{"day": d, "demand": float(rng.uniform(1e5, 1e6)),
                  "supply": float(rng.uniform(1e5, 1e6))} for d in sd_days]
    chips_series = [{"day": d, "conc": float(rng.uniform(10, 45)),
                     "profit": float(rng.uniform(20, 80)), "avg_cost": 21.0}
                    for d in days]
    return {
        "conf_q": "high",
        "fund": {"name": "测试股份", "pe_ttm": 18.5, "pb": 1.8,
                 "mcap_yi": 320, "turnover": 2.1, "eps": 1.2,
                 "net_growth": 15.3},
        "main_flow_series": main_flow_series,
        "chip_dist": chip_dist,
        "holder_series": holder_series,
        "sd_series": sd_series,
        "chips_series": chips_series,
        "flow": None,
    }


_DATA = None


def _data(market=None):
    global _DATA
    if _DATA is None:
        _DATA = build_market_data(market or _market())
    return _DATA


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
        from desktop.mkt_widget import MktWidget
    except Exception as e:  # 无 Qt/offscreen 环境
        pytest.skip(f"MktWidget 不可用: {e}")
    w = MktWidget(font_size=10)
    w.resize(980, 1500)
    w.show()
    app.processEvents()
    w.set_data(_data())
    app.processEvents()
    return app, w


def test_mkt_widget_renders(widget):
    """五面板齐全, 标题/估值卡/综合文案/解读已填充, 日期面板默认全幅。"""
    app, w = widget
    assert set(w.plots) == {"main_flow", "sub_flow", "chips", "holders", "sd"}
    assert "PE" in w.header_label.text and "市值" in w.header_label.text
    assert "20日" in w.plots["main_flow"].titleLabel.text
    assert "资金分项" in w.plots["sub_flow"].titleLabel.text
    assert "筹码堆积" in w.plots["chips"].titleLabel.text
    assert "股东户数" in w.plots["holders"].titleLabel.text
    assert "供需比" in w.plots["sd"].titleLabel.text
    assert "近20日主力" in w.cap_label.text and "集中度" in w.cap_label.text
    assert w.insights_label.text, "解读行应有内容"

    x0, x1 = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert abs(x0 - 0) < 1e-6 and x1 >= 59, "主力流默认全幅"
    xc0, xc1 = w.plots["chips"].getViewBox().viewRange()[0]
    wmax = max(_data()["chips"]["weights"]) * 1.15
    assert abs(xc1 - wmax) < 1e-3, "筹码 X 轴应为分布权重量程, 不被日期联动"


def test_mkt_widget_wheel_zoom_independent(widget):
    """滚轮缩放只作用于本面板 X, 不联动其他面板。"""
    app, w = widget
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtCore import Qt

    w.reset_view()
    app.processEvents()
    full_main = w.plots["main_flow"].getViewBox().viewRange()[0]
    full_sd = w.plots["sd"].getViewBox().viewRange()[0]

    view = w.scene().views()[0]
    gpos = view.viewport().mapToGlobal(QPoint(300, 248))
    ev = QWheelEvent(QPointF(300, 248), QPointF(gpos), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    app.processEvents()
    zm = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert zm[1] - zm[0] < full_main[1] - full_main[0], "滚轮向上应缩小跨度"
    zs = w.plots["sd"].getViewBox().viewRange()[0]
    assert abs(zs[0] - full_sd[0]) < 1e-6 and abs(zs[1] - full_sd[1]) < 1e-6, \
        "资金透视各面板缩放互不影响 (不同于指标页联动)"

    w.reset_plot("main_flow")
    app.processEvents()
    r = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert abs(r[0] - full_main[0]) < 1e-6 and abs(r[1] - full_main[1]) < 1e-6


def test_mkt_widget_keyboard_reset(widget):
    """左右平移/Home 复位不抛异常; 复位后日期面板回到全幅。"""
    app, w = widget
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import Qt

    def key(k):
        return QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier)

    w.reset_view()
    app.processEvents()
    full = w.plots["main_flow"].getViewBox().viewRange()[0]
    w.zoom_x_about("main_flow", (full[0] + full[1]) / 2, 0.5)
    app.processEvents()
    z0 = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert z0[1] - z0[0] < full[1] - full[0]
    w.keyPressEvent(key(Qt.Key.Key_Left))
    app.processEvents()
    p = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert abs(p[0] - z0[0]) > 1e-6, "左键应平移"
    w.keyPressEvent(key(Qt.Key.Key_Home))
    app.processEvents()
    r = w.plots["main_flow"].getViewBox().viewRange()[0]
    assert abs(r[0] - full[0]) < 1e-6 and abs(r[1] - full[1]) < 1e-6


def test_mkt_widget_empty(widget):
    """空数据回到空态, 不抛异常。"""
    app, w = widget
    w.set_data(None)
    app.processEvents()
    assert w._empty
    assert w.cap_label.text == ""


def test_mkt_widget_export_png(widget, tmp_path):
    """grab_pixmap 可导出 PNG。"""
    app, w = widget
    w.set_data(_data())
    app.processEvents()
    pm = w.grab_pixmap()
    out = tmp_path / "mkt.png"
    assert pm.save(str(out))
    assert out.stat().st_size > 1000
