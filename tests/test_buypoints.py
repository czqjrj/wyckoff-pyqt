"""威科夫完整做多买点 (buypoints) 单元与集成回归测试。

覆盖: 弹簧/弹簧二次测试/ST 左侧买点, 结构化放量突破(确认站稳)、突破回踩,
以及"左侧起仓→右侧加仓"门控纪律 (无左侧买点时右侧买点不成立)。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import buypoints as BP
from wyckoff.indicators import add_indicators


def _mkdf(n=140, vari=0.04, seed=7):
    """构造"下跌→吸筹→弹簧→箱体→放量突破→回踩"的完整场景 K 线。"""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2024-01-01", periods=n, freq="D")
    close = np.empty(n)
    low = np.empty(n)
    high = np.empty(n)
    op = np.empty(n)
    vol = np.full(n, 1.0e6)

    c = 12.0
    for i in range(n):
        if i < 45:                       # 下跌趋势 12 -> 8
            c = max(8.0, c - 0.09)
        elif i < 55:                     # SC 附近, 下跌加速后企稳
            c += 0.03
        elif i < 92:                     # 吸筹平台 8.3~8.7
            c += rng.normal(0, vari)
            c = min(8.75, max(8.15, c))
        elif i == 92:                    # 弹簧: 下破前低
            c = 7.9
        elif i == 93:                    # 快速收回
            c = 8.2
        elif i < 102:                    # 收回后抬升
            c = 8.5
        elif i < 132:                    # 箱体顶部 9.0 附近窄幅整理
            c += rng.normal(0, 0.02)
            c = min(9.05, max(8.7, c))
        elif i == 132:                   # 放量突破箱顶
            c = 9.30
            vol[i] = 2.8e6
        elif i == 133:                   # 确认2: 站稳箱顶
            c = 9.22
        elif i == 134:                   # 确认3: 站稳箱顶
            c = 9.25
        elif i == 135:
            c = 9.18
        elif i == 136:                   # 缩量回踩箱顶并收复
            c = 9.10
            vol[i] = 0.6e6
        elif i < 160:
            c += rng.normal(0, 0.05)
        else:
            c = c * 1.01 if rng.random() < 0.6 else c * 0.995

        lo = c - 0.15
        hi = c + 0.15
        if i == 92:
            lo = 7.80                    # 弹簧低点
            hi = 8.05
        if i == 132:
            hi = 9.40
            lo = 9.15
        if i == 136:
            lo = 8.90                    # 试探不破箱顶
            hi = 9.18
        lo = max(lo, 0.1)
        hi = max(hi, lo + 0.05)
        close[i] = c
        low[i] = lo
        high[i] = hi
        op[i] = c + rng.normal(0, 0.03)

    df = pd.DataFrame({
        "day": days, "open": op, "high": high,
        "low": low, "close": close, "volume": vol.astype(float),
    })
    return df


def _mkdf_late(n=146, seed=11):
    """构造"下跌→底部横盘→靠近末尾弹簧收回"的场景, 供 latest 窗口测试。"""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2024-06-01", periods=n, freq="D")
    close = np.empty(n)
    low = np.empty(n)
    high = np.empty(n)
    op = np.empty(n)
    vol = np.full(n, 1.0e6)
    c = 12.0
    for i in range(n):
        if i < 80:
            c = max(8.0, c - 0.05)
        elif i < 130:
            c += rng.normal(0, 0.03)
            c = min(8.5, max(8.0, c))
        elif i == 135:
            c = 7.8                       # 弹簧低点
        elif i == 136:
            c = 8.1
        elif i == 137:
            c = 8.4                       # 收回
        else:
            c += rng.normal(0, 0.03)
            c = min(8.6, max(8.2, c))
        lo = c - 0.12
        hi = c + 0.12
        if i == 135:
            lo, hi = 7.7, 7.9
        if i == 137:
            lo, hi = 8.2, 8.5
        close[i] = c
        low[i] = lo
        high[i] = hi
        op[i] = c + rng.normal(0, 0.03)
    return pd.DataFrame({
        "day": days, "open": op, "high": high,
        "low": low, "close": close, "volume": vol.astype(float),
    })


def _spring_event(idx=92, price=7.8):
    return [{"type": "Spring", "idx": idx, "price": price,
             "conf": 80, "date": None}]


def _spring_and_sos_events():
    return [{"type": "Spring", "idx": 92, "price": 7.8, "conf": 80, "date": None},
            {"type": "SOS", "idx": 134, "price": 9.25, "conf": 85, "date": None}]


def test_spring_bp_emitted_with_recovery():
    df = add_indicators(_mkdf(), symbol="test")
    bps = BP.struct_buy_points(df, _spring_event())
    springs = [b for b in bps if b["kind"] == "spring"]
    assert springs, bps
    b = springs[0]
    assert b["bar_idx"] > 92
    assert b["stop_price"] < b["entry_price"]
    assert b["target_price"] > b["entry_price"]
    assert b["entry_price"] > 7.8                # 弹簧收回后才入场


def test_structural_breakout_confirmed():
    df = add_indicators(_mkdf(), symbol="test")
    breaks = BP._structural_breakouts(df)
    confs = BP._structural_confirmations(df, breaks)
    assert breaks and confs
    s = confs[0]
    assert "level" in s and s["j"] == 134
    assert s["level"] > 0 and s["conf"] > 55


def test_structural_pullback_detected():
    df = add_indicators(_mkdf(), symbol="test")
    breaks = BP._structural_breakouts(df)
    pulls = BP._structural_pullbacks(df, breaks)
    assert pulls
    p = pulls[0]
    assert 132 < p["j"] <= 136
    assert p["shrink"] < 0.95


def test_disabled_kinds_never_emitted():
    df = add_indicators(_mkdf(), symbol="test")
    bps = BP.struct_buy_points(df, _spring_and_sos_events())
    assert not [b for b in bps if b["kind"] in BP.DISABLED_KINDS], \
        [b["kind"] for b in bps]


def test_rightside_gated_without_leftside():
    df = add_indicators(_mkdf(), symbol="test")
    bps = BP.struct_buy_points(df, [{"type": "BU", "idx": 133,
                                     "price": 9.05, "conf": 80, "date": None}])
    assert not [b for b in bps if b["kind"] == "bu_backup"], \
        [b["kind"] for b in bps]


def test_rightside_emitted_after_leftside():
    df = add_indicators(_mkdf(), symbol="test")
    events = [{"type": "Spring", "idx": 92, "price": 7.8, "conf": 80, "date": None},
              {"type": "BU", "idx": 133, "price": 9.05, "conf": 80, "date": None}]
    cache = {i: "accumulation" for i in range(len(df))}
    bps = BP.struct_buy_points(df, events, context_cache=cache)
    bu = [b for b in bps if b["kind"] == "bu_backup"]
    assert bu, [b["kind"] for b in bps]
    b = bu[0]
    assert 132 < b["bar_idx"] <= 136
    assert b["stop_price"] < b["entry_price"]
    # 右侧须有更低的左侧买点支撑 (门控纪律)
    lefts = [a for a in bps if a["kind"] in ("st_bottom", "lps", "spring",
                                             "spring_retest")]
    assert any(a["bar_idx"] < b["bar_idx"] and
               a["entry_price"] < b["entry_price"] for a in lefts)


def test_reject_stage_blocks_distribution_markdown():
    assert BP._reject_stage("distribution", ("sos_break",))
    assert BP._reject_stage("markdown", ("spring",))
    assert not BP._reject_stage("accumulation", ("sos_break",))
    assert not BP._reject_stage("flat", ("spring",))


def test_kind_meta_and_target_price():
    df = add_indicators(_mkdf(), symbol="test")
    e = {"type": "BU", "idx": 133, "price": 9.05, "conf": 80}
    bp = BP._lps_bp(df, "accumulation", e)
    assert bp["kind"] == "bu_backup"
    assert bp["cls"] == "right_main"
    risk = bp["entry_price"] - bp["stop_price"]
    assert bp["target_price"] == round(bp["entry_price"] + risk * bp["rr"], 4)


def test_latest_buy_points_only_recent():
    df = add_indicators(_mkdf_late(), symbol="test")
    events = [{"type": "Spring", "idx": 135, "price": 7.8, "conf": 80, "date": None}]
    latest = BP.latest_buy_points(df, events, look=10)
    assert latest
    for b in latest:
        assert b["bar_idx"] >= len(df) - 10
    assert len(latest) <= 8
    assert all(b["target_price"] is None or b["now"] < b["target_price"]
               for b in latest)
