# -*- coding: utf-8 -*-
"""多维度信号融合 (wyckoff.fusion.fuse_signals) 的回归测试。

验证: K线结构 / 威科夫事件 / VSA / P&F 四维信号被量化成统一多空评分,
方向一致的维度产生共振并提高置信度, 分歧产生矛盾提示 (综合方向不盲目跟多)。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.fusion import (fuse_signals, _event_score, _vsa_score,
                            _pnf_score, _kline_score)


def _df(closes):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.999, "close": closes,
        "high": closes * 1.008, "low": closes * 0.992,
        "volume": np.random.rand(n) * 1e6,
        "price_ma20": pd.Series(closes).rolling(20, min_periods=1).mean().values,
        "price_ma50": pd.Series(closes).rolling(50, min_periods=1).mean().values,
        "price_ma200": pd.Series(closes).rolling(200, min_periods=1).mean().values,
    })


def test_bullish_fusion():
    """全维偏多 → 综合看多 + 高置信, 且无矛盾。"""
    closes = np.linspace(50, 120, 300) + np.sin(np.linspace(0, 10, 300)) * 3
    df = _df(closes)
    events = [
        {"idx": len(df) - 10, "type": "Spring", "conf": 90},
        {"idx": len(df) - 30, "type": "SOS", "conf": 80},
    ]
    vsa = [{"idx": len(df) - 5, "label": "SPR"},
           {"idx": len(df) - 15, "label": "SPR"}]
    pnf_t = {"direction": "up", "tr_top": 110.0, "tr_bottom": 90.0,
             "横向计数上方目标": 130.0}
    f = fuse_signals(df, "上升趋势 (Markup)", events, vsa, pnf_t)
    assert f["score"] > 30, f"应偏多, got {f['score']}"
    assert f["bias"] == "看多"
    assert f["confidence"] in ("高", "中")
    assert not f["conflicts"], "全维同向不应有矛盾"


def test_bearish_fusion():
    """全维偏空 → 综合看空 + 高置信。"""
    closes = np.linspace(120, 60, 300) + np.sin(np.linspace(0, 10, 300)) * 3
    df = _df(closes)
    events = [{"idx": len(df) - 10, "type": "UTAD", "conf": 90}]
    vsa = [{"idx": len(df) - 5, "label": "ND"},
           {"idx": len(df) - 20, "label": "ND"}]
    pnf_t = {"direction": "down", "tr_top": 110.0, "tr_bottom": 90.0,
             "横向计数下方目标": 70.0}
    f = fuse_signals(df, "下跌趋势 (Markdown)", events, vsa, pnf_t)
    assert f["score"] < -30, f"应偏空, got {f['score']}"
    assert f["bias"] == "看空"
    assert f["confidence"] in ("高", "中")
    assert not f["conflicts"]


def test_conflict_detected():
    """K线看空 + P&F看多 → 方向矛盾被报告, 综合不盲目跟多。"""
    closes = np.linspace(120, 80, 300) + np.sin(np.linspace(0, 10, 300)) * 2
    df = _df(closes)
    f = fuse_signals(df, "下跌趋势 (Markdown)", [], [],
                     {"direction": "up", "tr_top": 100.0, "tr_bottom": 90.0,
                      "横向计数上方目标": 110.0})
    assert f["conflicts"], "应检测到方向矛盾"
    assert f["score"] < 15, "矛盾时综合分不应强偏多"


def test_event_score_decay():
    """威科夫事件评分: 多头正、空头负、时间衰减 (距当前越近权重越高)。"""
    recent = [{"idx": 119, "type": "SOS", "conf": 100}]  # 距当前 1
    old = [{"idx": 20, "type": "SOS", "conf": 100}]      # 距当前 100
    s_recent = _event_score(recent, recent_window=120, max_idx=120)
    s_old = _event_score(old, recent_window=120, max_idx=120)
    assert s_recent > 0 and s_recent > s_old, "近期事件应比久远事件权重更高"
    bear = [{"idx": 110, "type": "UTAD", "conf": 100}]
    assert _event_score(bear, max_idx=120) < 0


def test_vsa_score_direction():
    bull = [{"idx": 110, "label": "SPR"}, {"idx": 100, "label": "SPR"}]
    bear = [{"idx": 110, "label": "ND"}, {"idx": 100, "label": "ND"}]
    assert _vsa_score(bull, max_idx=120) > 0
    assert _vsa_score(bear, max_idx=120) < 0
    assert _vsa_score([]) == 0


def test_pnf_score_states():
    # 向上未到位 → 强多
    assert _pnf_score({"direction": "up", "横向计数上方目标": 200.0}, 100.0) > 0
    # 向上已到位 → 弱多
    assert _pnf_score({"direction": "up", "横向计数上方目标": 90.0}, 100.0) < \
        _pnf_score({"direction": "up", "横向计数上方目标": 200.0}, 100.0)
    # 向下未到位 → 强空
    assert _pnf_score({"direction": "down", "横向计数下方目标": 50.0}, 100.0) < 0
    # 区间内 → 中性
    mid = _pnf_score({"direction": "range", "tr_top": 110.0, "tr_bottom": 90.0}, 100.0)
    assert -20 < mid < 20


def test_kline_score_phase_and_ma():
    up = _df(np.linspace(50, 120, 300))
    assert _kline_score("上升趋势 (Markup)", up, []) > 0
    dn = _df(np.linspace(120, 60, 300))
    assert _kline_score("下跌趋势 (Markdown)", dn, []) < 0
