"""IndWidget (pyqtgraph 技术指标) 离屏冒烟测试: 渲染 + 交互 + 导出。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

from wyckoff.chart import build_ind_data


def _df(n=300):
    rng = np.random.default_rng(11)
    closes = pd.Series(20 + np.cumsum(rng.normal(0, 0.4, n)))
    closes = np.clip(closes, 8, None)
    open_ = closes * (1 + rng.normal(0, 0.003, n))
    df = pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": open_, "close": closes,
        "high": np.maximum(open_, closes) * 1.01,
        "low": np.minimum(open_, closes) * 0.99,
        "volume": rng.uniform(1e5, 2e6, n),
    })
    df["boll_mid"] = closes.rolling(20).mean()
    df["boll_up"] = df["boll_mid"] + 2 * closes.rolling(20).std()
    df["boll_dn"] = df["boll_mid"] - 2 * closes.rolling(20).std()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma10"] = df["volume"].rolling(10).mean()
    df["vol_ratio_20"] = df["volume"].rolling(20).mean().pct_change() + 1
    df["macd_dif"] = rng.normal(0, 0.5, n)
    df["macd_dea"] = df["macd_dif"] * 0.8
    df["macd_hist"] = df["macd_dif"] - df["macd_dea"]
    df["kdj_k"] = rng.uniform(5, 95, n)
    df["kdj_d"] = df["kdj_k"] * 0.7
    df["kdj_j"] = df["kdj_k"] * 1.2 - 30
    df["rsi_6"] = rng.uniform(10, 90, n)
    df["rsi_12"] = rng.uniform(10, 90, n)
    df["rsi_24"] = rng.uniform(10, 90, n)
    df["obv"] = np.cumsum(rng.normal(0, 1e5, n))
    return df


def _data(df):
    from wyckoff.market import relative_strength_series
    rng = np.random.default_rng(5)
    idx = pd.DataFrame({
        "day": df["day"],
        "close": 3000 + np.cumsum(rng.normal(0, 6, len(df))),
    })
    return build_ind_data(df, index_series=idx,
                          rs_series=relative_strength_series(df, idx))


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
        from desktop.ind_widget import IndWidget
    except Exception as e:  # 无 Qt/offscreen 环境
        pytest.skip(f"IndWidget 不可用: {e}")
    df = _df()
    data = _data(df)
    w = IndWidget(font_size=10)
    w.resize(900, 1400)
    w.show()
    app.processEvents()
    w.set_data(**data)
    app.processEvents()
    return app, w, df


def test_ind_widget_renders(widget):
    """默认显示全幅 (与原版一致), reset_view 回到全幅, 八面板齐全且解读行已填充。"""
    app, w, df = widget
    assert set(w.plots) == {"price", "volume", "macd", "kdj", "rsi", "obv",
                            "vp", "rs"}
    x0, x1 = w.plots["price"].getViewBox().viewRange()[0]
    n = w._n
    assert abs(x0 - 0) < 1e-6 and x1 >= n - 1, "默认视野应为全幅"
    w.reset_view()
    app.processEvents()
    x0, x1 = w.plots["price"].getViewBox().viewRange()[0]
    assert abs(x0 - w._full_x[0]) < 1e-6 and abs(x1 - w._full_x[1]) < 1e-6
    texts = {k: l.text for k, l in w.cap_labels.items()}
    assert all(v for v in texts.values()), "每个面板应有解读行文案"
    assert "现价" in texts["price"] and "布林" in texts["price"]


def test_ind_widget_wheel_zoom(widget):
    """滚轮向上缩小跨度 (以光标为锚点), 各面板 X 联动。"""
    app, w, df = widget
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    w.reset_view()
    app.processEvents()
    full = w.plots["price"].getViewBox().viewRange()[0]
    view = w.scene().views()[0]
    br = w.plots["price"].getViewBox().sceneBoundingRect()
    pos = QPoint(int(br.center().x()), int(br.center().y()))
    gpos = view.viewport().mapToGlobal(pos)
    ev = QWheelEvent(QPointF(pos), QPointF(gpos), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    app.processEvents()
    z = w.plots["price"].getViewBox().viewRange()[0]
    assert z[1] - z[0] < full[1] - full[0], "滚轮向上应缩小跨度"
    z2 = w.plots["obv"].getViewBox().viewRange()[0]
    assert abs(z2[0] - z[0]) < 1e-6 and abs(z2[1] - z[1]) < 1e-6, "日期面板 X 应联动"
    zv = w.plots["vp"].getViewBox().viewRange()[0]
    vp = _data(df)["vp"]
    expected = max(vp["vols"]) * 1.15
    assert abs(zv[1] - expected) < 1e-3, "量价分布 X 轴应为成交量量程 (万手)"


def test_ind_widget_keyboard_reset(widget):
    """键盘 +/左右/Home 不抛异常, Home 复位到全幅, 占位数据回到空态。"""
    app, w, df = widget
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    def key(k):
        return QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier)

    w.reset_view()
    app.processEvents()
    full = w.plots["price"].getViewBox().viewRange()[0]
    w.zoom_x_about((full[0] + full[1]) / 2, 0.5)
    app.processEvents()
    z0 = w.plots["price"].getViewBox().viewRange()[0]
    assert z0[1] - z0[0] < full[1] - full[0], "先缩放到非全幅"
    w.keyPressEvent(key(Qt.Key.Key_Left))
    app.processEvents()
    p = w.plots["price"].getViewBox().viewRange()[0]
    assert abs(p[0] - z0[0]) > 1e-6, "左键应平移"
    w.keyPressEvent(key(Qt.Key.Key_Home))
    app.processEvents()
    r = w.plots["price"].getViewBox().viewRange()[0]
    assert abs(r[0] - full[0]) < 1e-6 and abs(r[1] - full[1]) < 1e-6

    w.set_data(n=0)
    app.processEvents()
    assert w._n == 0


def test_ind_widget_export_png(widget, tmp_path):
    """grab_pixmap 可导出 PNG。"""
    app, w, df = widget
    pm = w.grab_pixmap()
    out = tmp_path / "ind.png"
    assert pm.save(str(out))
    assert out.stat().st_size > 1000
