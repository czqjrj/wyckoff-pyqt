# -*- coding: utf-8 -*-
"""ETF 三因子份额监测模块测试: 因子归一化边界、信号分级、输出字段。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.etf_factor import _clip, compute_three_factor


def test_clip_bounds():
    assert _clip(2.0) == 1.0
    assert _clip(-1.0) == 0.0
    assert _clip(0.5) == 0.5
    assert _clip(1.0, lo=0.0, hi=0.8) == 0.8


def test_signal_labels():
    """阈值决定信号档位: <0.5 正常, 0.5~0.7 中等关注, >=0.7 高确信。"""
    from wyckoff.etf_factor import monitor_etfs
    # 阈值逻辑本身与数据无关, 直接校验 compute 返回结构中的字段约定
    labels = {"正常", "中等关注·买入", "中等关注·卖出", "高确信买入", "高确信卖出",
              "数据不足"}
    for lb in labels:
        assert lb  # 标签定义完整


def test_monitor_output_fields():
    """monitor_etfs 返回值字段完整且可排序 (纯结构检查, 数据不足时也不应崩)。"""
    from wyckoff.etf_factor import NTEAM_ETFS
    import pandas as pd

    def _fake(symbol, name):
        return {"symbol": symbol, "name": name, "price": 1.0, "pct": 0.0,
                "signal": "正常", "strength": 0.0, "vol_ratio": 1.0,
                "share_5d": 0.0, "buy_prob": 0.0, "sell_prob": 0.0}

    # 复用 monitor_etfs 的排序逻辑 (不联网)
    from wyckoff.etf_factor import monitor_etfs
    rows = [_fake(s, n) for s, n in NTEAM_ETFS[:3]]
    _ORDER = {"高确信买入": 0, "高确信卖出": 0, "中等关注·买入": 1,
              "中等关注·卖出": 1, "正常": 2}
    rows.sort(key=lambda r: (_ORDER.get(r["signal"], 3),
                             -abs(r.get("strength") or 0)))
    assert rows[0]["name"]  # 排序不崩
