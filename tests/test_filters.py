# -*- coding: utf-8 -*-
"""补充分析方法过滤层 (wyckoff/filters.py) 回归测试。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.indicators import add_indicators
from wyckoff.filters import (chip_analysis, granville_signal, volatility_signal,
                             fundamental_filter, flow_gate,
                             build_filter_sections, filter_summary_cards)


def _mk_df(n=300, closes=None, volumes=None):
    if closes is None:
        closes = 20 + np.linspace(0, -4, n) * (n / 300)
    rng = np.random.default_rng(7)
    closes = np.asarray(closes, dtype=float)
    vols = (np.asarray(volumes, dtype=float) if volumes is not None
            else rng.uniform(5e5, 2e6, n))
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.999, "close": closes,
        "high": closes * 1.01, "low": closes * 0.99,
        "volume": vols,
    })
    return add_indicators(df, symbol="600104")


def _mk_flow(main_seq):
    n = len(main_seq)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "main": main_seq, "super": 0, "large": 0, "mid": 0, "small": 0,
    })


def test_chip_analysis_basic():
    df = _mk_df()
    chip = chip_analysis(df)
    assert chip is not None
    assert 0.0 <= chip["profit_ratio"] <= 1.0
    assert chip["cost_low"] <= chip["cost_high"]
    assert chip["concentration"] >= 0
    assert chip["avg_cost"] > 0
    assert chip["state"] in ("集中吸筹", "高位获利", "深度套牢", "均衡")


def test_chip_analysis_insufficient():
    assert chip_analysis(_mk_df(n=10)) is None


def test_granville_signal_basic():
    ma = granville_signal(_mk_df())
    assert ma is not None
    assert ma["arrangement"] in ("多头排列", "空头排列", "均线纠缠")
    assert ma["tone"] in ("bullish", "bearish", "neutral")
    assert ma["signal"]  # 非空 (买/卖/无明确信号)


def test_volatility_signal_basic():
    vol = volatility_signal(_mk_df())
    assert vol is not None
    assert 0 <= vol["position"] <= 1
    assert vol["state"] in ("低波动蓄势", "高波动", "正常波动")
    assert vol["tone"] in ("bullish", "bearish", "neutral")


def test_fundamental_filter_healthy_and_trap():
    ok = fundamental_filter({"pe_ttm": 12.0, "pb": 0.8, "net_growth": 0.05})
    assert ok is not None
    assert ok["hard_fail"] is False
    assert ok["tone"] in ("bullish", "neutral")
    trap = fundamental_filter({"pe_ttm": 60.0, "pb": 5.0, "net_growth": -0.2})
    assert trap is not None
    assert trap["hard_fail"] is True
    assert trap["tone"] == "bearish"
    assert fundamental_filter(None) is None


def test_flow_gate_directions():
    n = 40
    df = _mk_df(n=160)
    inflow = _mk_flow(np.r_[np.full(20, -1e7), np.full(20, 5e7)])
    outflow = _mk_flow(np.r_[np.full(20, 1e7), np.full(20, -5e7)])
    gi = flow_gate("底部整固 (Accumulation)", df, inflow)
    go = flow_gate("底部整固 (Accumulation)", df, outflow)
    assert gi is not None and go is not None
    assert gi["pct"] > 0 and go["pct"] < 0
    assert flow_gate("底部整固", df, None) is None


def test_sections_and_cards():
    df = _mk_df()
    filters = {
        "chip": chip_analysis(df),
        "ma": granville_signal(df),
        "vol": volatility_signal(df),
        "fund": fundamental_filter({"pe_ttm": 60.0, "pb": 5.0, "net_growth": -0.2}),
        "flow": flow_gate("底部整固 (Accumulation)", df,
                          _mk_flow(np.r_[np.full(20, 1e7), np.full(20, -5e7)])),
    }
    sections = build_filter_sections(filters)
    titles = [t for t, _ in sections]
    assert "筹码分布" in titles
    assert any("过滤" in t for t in titles)
    assert "过滤总览" in titles
    cards = filter_summary_cards(filters)
    labels = [c["label"] for c in cards]
    assert "筹码" in labels and "波动" in labels
    assert "资金门" in labels and "排雷" in labels
    # 均线卡由 build_signal_summary 原生输出, 过滤层不重复
    assert "均线" not in labels
    # 排雷红灯 → 总览须有警示
    joined = "\n".join(ln for _, ls in sections for ln in ls)
    assert "排雷红灯" in joined
