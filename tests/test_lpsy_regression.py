"""detect_lpsy 定义回归测试 (600104 2026-09-28 误标修复):

旧实现只要求"锚后 25 根内、缩量、低于前高"的任意反弹高点, 会把 AR 首轮
反弹 (远离供应区、贴着回调低点) 误标为 LPSY。修复后需满足:
1. 反抽高点回到供应区附近 (≥ 锚×0.93) —— 远离前高的首轮反弹不算对前高的测试;
2. 反抽前须存在回调低点枢轴 (AR);
3. 排除最新虚拟枢轴 (idx == n-1), 未确认实时 bar 不触发空头事件。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import events as E
from wyckoff.indicators import add_indicators


def _mkdf_dist(n=120, close_lines=None, bounce_high=9.5, bounce_at=85, vols=None):
    """构造 BC(高10.0)→AR低→反抽高的分段 K 线。

    baseline:
      idx 0..59  上行至 ~9.9
      idx 60     BC/UTAD 高点 10.0
      idx 61..75 回调至 AR 低点 ~8.55
      idx 76..bounce_at  反抽 (高点=bounce_high)
      idx bounce_at+1..  回落偏弱
    """
    closes = np.linspace(8.0, 9.9, 61)
    b, a = closes[-1], 8.6
    ar_seg = np.linspace(9.9, 8.6, 16)          # 60..75 (AR 低点 8.6)
    up_seg = np.linspace(8.6, bounce_high - 0.05, max(0, 1 + (bounce_at - 76)))
    tail = np.linspace(bounce_high - 0.05, 8.2,
                       n - (len(closes) - 1) - len(ar_seg) - len(up_seg))
    closes = np.concatenate([closes[:-1], ar_seg, up_seg, tail])
    closes = np.clip(closes, 7.5, 10.2)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = np.maximum(opens, closes) * 1.005
    lows = np.minimum(opens, closes) * 0.995
    highs[bounce_at] = max(highs[bounce_at], bounce_high)  # 反抽高点
    lows[75] = min(lows[75], 8.55)                        # AR 低点
    highs[60] = max(highs[60], 10.0)                       # BC 高点
    if vols is None:
        vols = np.full(n, 1e6)
        vols[60] = 6e6    # BC 放量
        vols[bounce_at] = 4e5   # 反抽缩量
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })


def _pivots(highs, lows):
    """按 (idx, price) 依次放入 高低 枢轴 (交替)。"""
    out = []
    for i, p in highs:
        out.append({"type": "high", "idx": i, "price": float(p),
                    "date": pd.Timestamp("2023-01-01")})
    for i, p in lows:
        out.append({"type": "low", "idx": i, "price": float(p),
                    "date": pd.Timestamp("2023-01-01")})
    out.sort(key=lambda x: x["idx"])
    return out


_BASE = [{"type": "BC", "idx": 60, "date": pd.Timestamp("2023-01-01"), "price": 10.0}]


def test_lpsy_not_on_ar_first_bounce_far_from_supply():
    """AR 首轮反弹远离供应区 (600104 今日形态) 不得标 LPSY。"""
    df = add_indicators(_mkdf_dist(bounce_high=8.8), symbol="600104")
    pivots = _pivots([(60, 10.0), (85, 8.8)], [(75, 8.55)])
    ev = E.detect_lpsy(df, pivots, _BASE)
    assert not [e for e in ev if e["type"] == "LPSY"], \
        "反抽远离供应区 (8.8 < 10.0×0.93) 不应产 LPSY"


def test_lpsy_when_bounce_reaches_supply_zone():
    """回调后反抽回到供应区附近且缩量未过前高 → LPSY。"""
    df = add_indicators(_mkdf_dist(bounce_high=9.5), symbol="600104")
    pivots = _pivots([(60, 10.0), (85, 9.5)], [(75, 8.55)])
    ev = E.detect_lpsy(df, pivots, _BASE)
    lpsy = [e for e in ev if e["type"] == "LPSY"]
    assert lpsy, "反抽回到前高 7% 以内且缩量, 应产 LPSY"
    assert lpsy[0]["idx"] == 85
    assert lpsy[0]["price"] == 9.5


def test_lpsy_requires_ar_low_before_bounce():
    """无回调低点枢轴 (AR) 的首轮冲高不得标 LPSY (归 UT 阶段)。"""
    df = add_indicators(_mkdf_dist(bounce_high=9.5), symbol="600104")
    pivots = _pivots([(60, 10.0), (85, 9.5)], [])   # 缺 AR 低点
    ev = E.detect_lpsy(df, pivots, _BASE)
    assert not [e for e in ev if e["type"] == "LPSY"], \
        "无回调低点的反弹是 AR 首轮冲高, 不应产 LPSY"


def test_lpsy_skips_virtual_last_bar():
    """最新虚拟枢轴 (idx==n-1, 未确认实时 bar) 即使接近前高也不标 LPSY。"""
    df = add_indicators(_mkdf_dist(bounce_high=9.5, bounce_at=119), symbol="600104")
    pivots = _pivots([(60, 10.0), (119, 9.5)], [(75, 8.55)])  # 高点在末根
    ev = E.detect_lpsy(df, pivots, _BASE)
    assert not [e for e in ev if e["type"] == "LPSY"], \
        "末根虚拟枢轴 (未确认) 不应触发空头信号"


def test_lpsy_confirmed_pivot_but_high_volume_blocked():
    """反抽回供应区但未缩量 → 不放行 (量闸保持)。"""
    df = _mkdf_dist(bounce_high=9.5)
    df.loc[85, "volume"] = 3e6   # 反抽柱子不再低量
    df = add_indicators(df, symbol="600104")
    pivots = _pivots([(60, 10.0), (85, 9.5)], [(75, 8.55)])
    ev = E.detect_lpsy(df, pivots, _BASE)
    assert not [e for e in ev if e["type"] == "LPSY"], "未缩量反抽不应产 LPSY"