"""十字光标 (Crosshair) 离屏测试: 类本身 + 四个图表集成。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest
from pyqtgraph.Qt.QtCore import QPointF

from wyckoff.chart import build_ind_data, build_kline_data, build_market_data
from wyckoff.pnf import build_pnf, build_pnf_data, pnf_history_targets, pnf_targets


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _emit_at(plot):
    """在指定 plot 的 viewBox 中心 emit 鼠标移动, 返回 scene 坐标。"""
    rect = plot.getViewBox().sceneBoundingRect()
    pos = QPointF(rect.center().x(), rect.center().y())
    plot.scene().sigMouseMoved.emit(pos)
    return pos


def _emit_outside(plot):
    """在 viewBox 之外 emit 鼠标移动 (应隐藏十字光标)。"""
    rect = plot.getViewBox().sceneBoundingRect()
    plot.scene().sigMouseMoved.emit(
        QPointF(rect.right() + 1e5, rect.center().y()))


# ── 类自身 ──
class TestCrosshairClass:
    def test_show_and_text(self):
        import pyqtgraph as pg

        from desktop.crosshair import Crosshair
        app = _app()
        win = pg.GraphicsLayoutWidget()
        plot = win.addPlot()
        ch = Crosshair(plot, lambda x: f"x={x:.1f}", lambda y: f"y={y:.2f}")
        win.show()
        app.processEvents()
        vb = plot.getViewBox()
        rect = vb.sceneBoundingRect()
        pos = QPointF(rect.center().x(), rect.center().y())
        ch._moved(pos)
        assert ch.vline.isVisible() and ch.hline.isVisible()
        assert ch.txt_x.isVisible() and ch.txt_y.isVisible()
        assert "x=" in ch.txt_x.toPlainText()
        assert "y=" in ch.txt_y.toPlainText()

    def test_hide_outside_and_detach(self):
        import pyqtgraph as pg

        from desktop.crosshair import Crosshair
        app = _app()
        win = pg.GraphicsLayoutWidget()
        plot = win.addPlot()
        ch = Crosshair(plot, lambda x: "x", lambda y: "y")
        win.show()
        app.processEvents()
        rect = plot.getViewBox().sceneBoundingRect()
        ch._moved(QPointF(rect.right() + 1e5, rect.center().y()))
        assert not ch.vline.isVisible() and not ch.txt_x.isVisible()
        ch.detach()
        for it in (ch.vline, ch.hline, ch.txt_x, ch.txt_y):
            assert it.scene() is None, "detach 应从 plot 移除全部 item"
        ch._moved(QPointF(1.0, 1.0))  # detach 后 emit 不抛异常


# ── Kline ──
def _kline_df(n=120):
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


def _kline_data(df):
    return build_kline_data(
        df, [], [], "冒烟", profile={"poc": 55.0},
        phase="accumulation", sector={"name": "汽车整车", "main20": 1e8})


@pytest.fixture(scope="module")
def kline():
    app = _app()
    try:
        from desktop.kline_widget import KlineWidget
    except Exception as e:
        pytest.skip(f"KlineWidget 不可用: {e}")
    df = _kline_df()
    w = KlineWidget(font_size=10)
    w.resize(900, 600)
    w.show()
    app.processEvents()
    w.set_data(**_kline_data(df))
    app.processEvents()
    return app, w, df


def test_kline_crosshairs(kline):
    """主图 3 条十字光标: X 为日期, Y 分别为价格/万手/整数。"""
    app, w, df = kline
    assert len(w._crosshairs) == 3
    for ch in w._crosshairs:
        _emit_at(ch.plot)
    app.processEvents()
    assert "2023-" in w._crosshairs[0].txt_x.toPlainText(), \
        "主图 X 应显示日期"
    assert w._crosshairs[1].txt_y.toPlainText().endswith("万手")
    assert w._crosshairs[2].txt_y.toPlainText().isdigit()


def test_kline_crosshair_hide_outside(kline):
    app, w, df = kline
    _emit_outside(w.price_plot)
    app.processEvents()
    assert not w._crosshairs[0].vline.isVisible()
    assert not w._crosshairs[0].txt_y.isVisible()


def test_kline_crosshair_rebuild(kline):
    """set_data 重建后仍为 3 条且工作正常 (旧 item 已清理)。"""
    app, w, df = kline
    w.set_data(**_kline_data(df))
    app.processEvents()
    assert len(w._crosshairs) == 3
    _emit_at(w.price_plot)
    app.processEvents()
    assert "2023-" in w._crosshairs[0].txt_x.toPlainText()


# ── Ind ──
def _ind_df(n=300):
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


@pytest.fixture(scope="module")
def ind():
    app = _app()
    try:
        from desktop.ind_widget import IndWidget
    except Exception as e:
        pytest.skip(f"IndWidget 不可用: {e}")
    df = _ind_df()
    w = IndWidget(font_size=10)
    w.resize(900, 1400)
    w.show()
    app.processEvents()
    w.set_data(**build_ind_data(df))
    app.processEvents()
    return app, w


def test_ind_crosshairs(ind):
    """八面板各一条: 日期面板 X 为日期, vp 面板 X 为成交量。"""
    app, w = ind
    assert len(w._crosshairs) == 8
    for ch in w._crosshairs:
        _emit_at(ch.plot)
    app.processEvents()
    assert "2023-" in w._crosshairs[0].txt_x.toPlainText()
    assert not w._crosshairs[0].txt_y.toPlainText().endswith("万手")
    vp_ch = w._crosshairs[6]
    assert vp_ch.txt_x.toPlainText().endswith("万手"), \
        "vp 面板 X 应显示成交量"
    assert "." in vp_ch.txt_y.toPlainText(), "vp 面板 Y 应显示价格"


# ── Pnf ──
def _pnf_df(n=300):
    rng = np.random.default_rng(7)
    closes = 20 + np.cumsum(rng.normal(0, 0.4, n))
    closes = np.clip(closes, 8, None)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.003, n)),
        "close": closes * (1 + rng.normal(0, 0.003, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": rng.uniform(1e5, 2e6, n),
    })


@pytest.fixture(scope="module")
def pnf():
    app = _app()
    try:
        from desktop.pnf_widget import PnfWidget
    except Exception as e:
        pytest.skip(f"PnfWidget 不可用: {e}")
    df = _pnf_df()
    cols, box = build_pnf(df)
    w = PnfWidget(font_size=10)
    w.resize(900, 600)
    w.show()
    app.processEvents()
    w.set_data(**build_pnf_data(
        cols, box, "冒烟", targets=pnf_targets(df, cols, box),
        history=pnf_history_targets(cols, box)))
    app.processEvents()
    return app, w


def test_pnf_crosshairs(pnf):
    """单图一条: X 为列序号, Y 为价格。"""
    app, w = pnf
    assert len(w._crosshairs) == 1
    _emit_at(w.plot)
    app.processEvents()
    ch = w._crosshairs[0]
    assert "列 " in ch.txt_x.toPlainText()
    assert "." in ch.txt_y.toPlainText()


# ── Mkt ──
def _market():
    rng = np.random.default_rng(7)
    days = pd.date_range("2024-01-01", periods=60, freq="D")
    main_flow_series = [{
        "day": d, "main": float(rng.normal(0.3e8, 1.2e8)),
        "super": float(rng.normal(0.2e8, 0.5e8)),
        "large": float(rng.normal(0.1e8, 0.4e8)),
        "mid": float(rng.normal(-0.05e8, 0.3e8)),
        "small": float(rng.normal(-0.1e8, 0.3e8)),
    } for d in days]
    holder_series = [{
        "end_date": d, "pre_date": d - pd.DateOffset(months=3),
        "holder_num": float(rng.uniform(5e4, 8e4)),
        "pre_num": float(rng.uniform(5e4, 8e4)),
        "ratio": float(rng.normal(0, 2)),
    } for d in pd.date_range("2023-03-31", periods=40, freq="QE")]
    sd_series = [{"day": d, "demand": float(rng.uniform(1e5, 1e6)),
                  "supply": float(rng.uniform(1e5, 1e6))}
                 for d in pd.date_range("2024-01-01", periods=30, freq="D")]
    chips_series = [{"day": d, "conc": float(rng.uniform(10, 45)),
                     "profit": float(rng.uniform(20, 80)), "avg_cost": 21.0}
                    for d in days]
    return {
        "conf_q": "high",
        "fund": {"name": "测试股份", "pe_ttm": 18.5, "pb": 1.8,
                 "mcap_yi": 320, "turnover": 2.1, "eps": 1.2,
                 "net_growth": 15.3},
        "main_flow_series": main_flow_series,
        "chip_dist": {"prices": [float(v) for v in np.linspace(15, 30, 30)],
                      "weights": [float(v)
                                  for v in np.random.dirichlet(np.ones(30))],
                      "cur": 22.5, "poc": 24.0, "below": 0.42},
        "holder_series": holder_series,
        "sd_series": sd_series,
        "chips_series": chips_series,
        "flow": None,
    }


@pytest.fixture(scope="module")
def mkt():
    app = _app()
    try:
        from desktop.mkt_widget import MktWidget
    except Exception as e:
        pytest.skip(f"MktWidget 不可用: {e}")
    w = MktWidget(font_size=10)
    w.resize(980, 1500)
    w.show()
    app.processEvents()
    w.set_data(build_market_data(_market()))
    app.processEvents()
    return app, w


def test_mkt_crosshairs(mkt):
    """五面板各一条: 资金面板 Y 为亿, chips X 为权重, 其余 X 为日期。"""
    import re
    app, w = mkt
    assert len(w._crosshairs) == 5
    for ch in w._crosshairs:
        _emit_at(ch.plot)
    app.processEvents()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                        w._crosshairs[0].txt_x.toPlainText())
    assert w._crosshairs[0].txt_y.toPlainText().endswith("亿")
    chips_ch = w._crosshairs[2]
    assert "2024-" not in chips_ch.txt_x.toPlainText(), \
        "chips X 为分布权重, 不显示日期"
    assert "." in chips_ch.txt_x.toPlainText()
    assert "." in chips_ch.txt_y.toPlainText()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                        w._crosshairs[3].txt_x.toPlainText())
    assert w._crosshairs[3].txt_y.toPlainText().replace(",", "").isdigit()
