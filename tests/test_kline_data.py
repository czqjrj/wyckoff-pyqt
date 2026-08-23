"""验证 build_kline_data (pyqtgraph 桌面端 K 线数据收集) 的输出契约。

该函数是 matplotlib plot_chart 与 pyqtgraph KlineWidget 的共享数据边界,
必须保证返回 dict 的键/结构与 KlineWidget.set_data 一致 (键集即契约)。
"""
import numpy as np
import pandas as pd

from wyckoff.chart import _event_layout, build_kline_data, kline_caption
from wyckoff.config import EVENT_COLORS

_KEYS = {
    "df", "title", "pivots", "events", "waves", "draw_waves", "locks",
    "tr", "profile", "phase", "segs", "sector", "vsa_signals",
    "wave_cum", "wave_segs", "up_mask", "caption", "symbol", "scale",
}


def _df(n=80):
    closes = np.linspace(50, 80, n) + np.sin(np.linspace(0, 8, n)) * 2
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.998, "close": closes,
        "high": closes * 1.01, "low": closes * 0.99,
        "volume": np.random.rand(n) * 1e6,
        "price_ma20": pd.Series(closes).rolling(20, min_periods=1).mean().values,
        "price_ma50": pd.Series(closes).rolling(50, min_periods=1).mean().values,
        "vol_ratio_20": np.ones(n),
    })


def _events(df):
    return [
        {"idx": 5, "type": "SC", "price": float(df["low"].iloc[5]),
         "color": EVENT_COLORS["SC"], "desc": "卖出高潮", "conf": 90},
        {"idx": 20, "type": "Spring", "price": float(df["low"].iloc[20]),
         "color": EVENT_COLORS["Spring"], "desc": "弹簧", "conf": 85},
        {"idx": 60, "type": "SOS", "price": float(df["high"].iloc[60]),
         "color": EVENT_COLORS["SOS"], "desc": "强势信号", "conf": 80},
    ]


def _pivots(df):
    return [
        {"idx": 5, "type": "low", "price": float(df["low"].iloc[5])},
        {"idx": 35, "type": "high", "price": float(df["high"].iloc[35])},
        {"idx": 70, "type": "high", "price": float(df["high"].iloc[70])},
    ]


def test_kline_data_keys_and_lengths():
    """返回 dict 的键集与 KlineWidget.set_data 签名一致, 序列长度正确。"""
    df = _df()
    d = build_kline_data(df, _pivots(df), _events(df), "标题", waves=None)
    assert set(d) == _KEYS, f"键集漂移: {set(d) ^ _KEYS}"
    assert d["df"] is df
    assert d["title"] == "标题"
    assert d["draw_waves"] is True
    assert len(d["wave_cum"]) == len(df)
    assert len(d["up_mask"]) == len(df)
    assert d["up_mask"].dtype == bool
    text, color = d["caption"]
    assert isinstance(text, str) and text.startswith("现价")
    assert isinstance(color, str) and color.startswith("#")


def test_kline_data_events_layout():
    """events 为 (e, sign, dy) 三元组, 去重且 sign/dy 可绘。"""
    df = _df()
    events = _events(df)
    d = build_kline_data(df, _pivots(df), events, "标题", waves=None)
    laid = d["events"]
    assert len(laid) == 3
    for e, sign, dy in laid:
        assert e["type"] in {"SC", "Spring", "SOS"}
        assert sign in (-1, 1)
        assert dy > 0


def test_kline_data_event_layout_dedup_and_stagger():
    """同一天重复事件去重; 同一天多事件错开 dy。"""
    df = _df()
    ev = _events(df)
    dup = ev + [{"idx": 5, "type": "SC", "price": ev[0]["price"],
                 "color": EVENT_COLORS["SC"], "desc": "", "conf": 90},
                {"idx": 20, "type": "Spring", "price": ev[1]["price"],
                 "color": EVENT_COLORS["Spring"], "desc": "", "conf": 90},
                {"idx": 6, "type": "PSY", "price": ev[0]["price"] - 1,
                 "color": EVENT_COLORS["PSY"], "desc": "", "conf": 90}]
    laid = _event_layout(df, dup)
    assert len(laid) == 4  # SC@5 去重, PSY@6 保留 (不同日同类不撞)
    day5 = [dy for e, _s, dy in laid if e["idx"] == 5]
    assert len(day5) == 1


def test_kline_data_locks():
    """locks 为 (idx, price, level) 三元组, 仅近期事件参与。"""
    df = _df()
    events = _events(df)
    events += [
        {"idx": len(df) - 3, "type": "JOC", "price": float(df["high"].iloc[-3]),
         "color": EVENT_COLORS["JOC"], "desc": "突破", "conf": 80},
        {"idx": len(df) - 1, "type": "LPS", "price": float(df["low"].iloc[-1]),
         "color": EVENT_COLORS["LPS"], "desc": "回踩", "conf": 75},
    ]
    d = build_kline_data(df, _pivots(df), events, "标题", waves=None)
    assert d["locks"], "近期 SOS/JOC/LPS 应产出买点锁"
    for lx, ly, level in d["locks"]:
        assert 0 <= lx < len(df)
        assert ly > 0
        assert level in (1, 2, 3)


def test_kline_data_wave_cum_segs():
    """波浪数据: wave_segs 三元组边界合法, wave_cum 分段带符号。"""
    df = _df()
    waves = [(10, float(df["low"].iloc[10]), "1"),
             (30, float(df["high"].iloc[30]), "2"),
             (55, float(df["low"].iloc[55]), "3")]
    d = build_kline_data(df, _pivots(df), _events(df), "标题", waves=waves)
    assert d["waves"] == [list(w) for w in waves]
    segs = d["wave_segs"]
    assert segs is not None and len(segs) >= 2
    for a, b, direction in segs:
        assert 0 <= a <= b < len(df)
        assert direction in (-1, 0, 1)
    cum = d["wave_cum"]
    assert np.isfinite(cum).all()


def test_kline_data_auto_segs_and_passthrough():
    """segs 缺省时自动生成阶段段; tr/profile/vsa 原样透传。"""
    df = _df()
    tr = {"top": 78.0, "bottom": 52.0}
    vsa = [{"idx": 40, "label": "CHOC", "color": "#c92a2a"}]
    d = build_kline_data(df, _pivots(df), _events(df), "标题", waves=None,
                         tr=tr, profile={"poc": 66.0}, vsa_signals=vsa,
                         sector={"name": "汽车整车", "main20": -1.2e8})
    assert d["segs"], "应自动生成阶段段"
    for seg in d["segs"]:
        assert len(seg) == 4
        assert 0 <= seg[0] <= seg[1] < len(df)
    assert d["tr"] is tr
    assert d["profile"]["poc"] == 66.0
    assert d["vsa_signals"] is vsa
    text, color = d["caption"]
    assert "板块" in text and "汽车整车" in text


def test_kline_caption_neutral():
    """均线/量比缺失时降级为'数据不足'而非崩溃。"""
    df = _df()
    df = df.drop(columns=["price_ma20", "price_ma50", "vol_ratio_20"])
    text, color = kline_caption(df, [])
    assert isinstance(text, str) and text
    assert isinstance(color, str)
