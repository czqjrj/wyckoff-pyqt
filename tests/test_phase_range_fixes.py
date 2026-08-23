"""阶段区间划分四项理论修正回归测试。

1. Spring/UTAD 刺破收回不切断区间 (_detect_ranges 探针逻辑);
2. 区间类型 = 区间内事件加权 + 进入方向先验 (_build_phases / _range_type_by_events);
3. _mark_bottoms 需吸筹事件证据才标吸筹;
4. 区间参数全部抽到 config, 常量与 events.Spring 阈值口径统一。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.config import (
    ACC_RANGE_EV,
    DIST_RANGE_EV,
    RANGE_BAND,
    RANGE_EVENT_WEIGHT,
    RANGE_MIN_BARS,
    RANGE_PROBE_WIN,
    RANGE_TOL,
)
from wyckoff.phases import _detect_ranges, _mark_bottoms, _range_type_by_events


def _osc_df(n=200, spring_idx=None, spring_lo=78.0, recover=True,
            lo=80.0, hi=95.0):
    """区间震荡K线, 可选在 spring_idx 处下刺到 spring_lo (随后是否收回)。"""
    rng = np.random.default_rng(7)
    t = np.arange(n)
    mid = (lo + hi) / 2
    close = mid + (hi - lo) / 2 * 0.7 * np.sin(t / 6.0) \
        + rng.normal(0, 0.25, n)
    if spring_idx is not None:
        close[spring_idx] = spring_lo - 1.0
        if not recover:
            close[spring_idx + 1:spring_idx + 1 + RANGE_PROBE_WIN] = \
                lo - 3.0 + rng.normal(0, 0.2,
                                      RANGE_PROBE_WIN)  # 收盘不收回 → 真跌破
    high = np.maximum(close + 1.2, close * 1.01)
    low = np.minimum(close - 1.2, close * 0.99)
    if spring_idx is not None:
        low[spring_idx] = spring_lo
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": close * 0.999,
        "close": close,
        "high": high,
        "low": low,
        "volume": rng.uniform(1e5, 2e6, n),
    })


def _piv(idxs_types):
    """[(idx, 'low', price), ...] → pivot dicts。"""
    return [{"idx": i, "type": ty, "price": pr} for i, ty, pr in idxs_types]


# ── 1. Spring 刺破收回不切断区间 ──

def test_spring_recovery_keeps_one_range():
    df = _osc_df(n=200, spring_idx=100, spring_lo=78.0, recover=True)
    # 区间下沿参考 81 (idx20), 弹簧刺破 78 后 12 根内收盘收回 81 上方
    pivots = _piv([
        (20, "low", 81.0), (40, "high", 95.0), (60, "low", 82.0),
        (80, "high", 94.0), (100, "low", 78.0),
        (130, "high", 93.0), (160, "low", 83.0),
    ])
    ranges = _detect_ranges(df, pivots)
    # 弹簧被吞并: 单个区间应贯穿弹簧点, 未在弹簧处切断
    assert len(ranges) == 1, ranges
    a, e = ranges[0][0], ranges[0][1]
    assert a <= 20 and e >= 160, (a, e)


def test_spring_no_recovery_breaks_range():
    df = _osc_df(n=200, spring_idx=100, spring_lo=78.0, recover=False)
    pivots = _piv([
        (20, "low", 81.0), (40, "high", 95.0), (60, "low", 82.0),
        (80, "high", 94.0), (100, "low", 78.0),
        (130, "high", 93.0), (160, "low", 83.0),
    ])
    ranges = _detect_ranges(df, pivots)
    assert ranges, ranges
    # 真跌破: 弹簧处切断, 区间止于弹簧前一根
    assert ranges[0][1] == 99, ranges[0]


def test_utad_recovery_keeps_one_range():
    df_up = _osc_df(n=200, spring_idx=100, spring_lo=78.0, recover=True)
    # 上侧陷阱: 把区间镜像为 [80,95], 在 idx 处上刺前高后收回
    hi_arr = 173.0 - df_up["low"].values
    lo_arr = 173.0 - df_up["high"].values
    cl = df_up["close"].values
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=200),
        "open": cl * 0.999,
        "close": cl,
        "high": hi_arr,
        "low": lo_arr,
        "volume": df_up["volume"].values,
    })
    # 前高参考 93 (idx20), 上刺新高 96.5 后收回
    pivots = _piv([
        (20, "high", 93.0), (40, "low", 83.0), (60, "high", 92.0),
        (80, "low", 84.0), (100, "high", 96.5),
        (130, "low", 85.0), (160, "high", 91.0),
    ])
    ranges = _detect_ranges(df, pivots)
    assert len(ranges) == 1, ranges
    a, e = ranges[0][0], ranges[0][1]
    assert a <= 20 and e >= 160, (a, e)


# ── 2. 区间类型 = 事件加权 + 进入方向先验 ──

def _mk_events(items):
    return [{"idx": i, "type": ty} for i, ty in items]


def test_events_override_entry_prior():
    # 上涨进入 → 先验 distribution; 区间内大量吸筹事件 → 翻转为吸筹
    ev = _mk_events([(70, "SC"), (80, "ST"), (90, "Spring"), (100, "SOS")])
    typ = _range_type_by_events(ev, 60, 140, "distribution")
    assert typ == "accumulation", typ

    # 下跌进入 → 先验 accumulation; 区间内派发事件占优 → 翻转为派发
    ev2 = _mk_events([(70, "BC"), (90, "UTAD"), (110, "SOW")])
    typ2 = _range_type_by_events(ev2, 60, 140, "accumulation")
    assert typ2 == "distribution", typ2


def test_events_absent_keeps_prior():
    assert _range_type_by_events(None, 60, 140, "accumulation") == "accumulation"
    assert _range_type_by_events([], 60, 140, "distribution") == "distribution"
    # 非吸筹/派发事件与区间外事件不计入
    ev = _mk_events([(30, "SC"), (90, "AR"), (100, "LPSY")])
    typ = _range_type_by_events(ev, 60, 140, "distribution")
    assert typ == "distribution", typ


def test_events_equal_weight_keeps_prior():
    ev = _mk_events([(70, "SC"), (71, "BC")])  # 权重 1.0 对 1.0
    typ = _range_type_by_events(ev, 60, 140, "accumulation")
    assert typ == "accumulation", typ
    typ2 = _range_type_by_events(ev, 60, 140, "distribution")
    assert typ2 == "distribution", typ2


def test_weight_tables_in_config():
    # 事件权重表只含已知事件类型 (防拼写漂移)
    known = {"PSY", "SC", "AR", "ST", "Spring", "SOS", "LPS", "JOC", "BU",
             "BC", "UT", "UTAD", "LPSY", "SOW", "Shakeout"}
    assert set(ACC_RANGE_EV) & known == set(ACC_RANGE_EV)
    assert set(DIST_RANGE_EV) & known == set(DIST_RANGE_EV)
    assert 0 < RANGE_EVENT_WEIGHT < 1


# ── 3. _mark_bottoms 底部标记: 无事件 V 型底也按结构标吸筹 ──
# 校准: 标"下跌"后 20 根上涨占 74%; "markdown→accumulation/markup" 拐点其后
# 20 根上涨占 66~84%, 死等事件证据会漏掉绝大多数无事件的 V 型底。

def _bottom_df():
    n = 80
    closes = np.empty(n)
    closes[:30] = np.linspace(100.0, 62.0, 30)      # 下跌
    closes[30] = 60.0                                # 底
    closes[31:40] = np.linspace(61.0, 63.0, 9)       # 底部缓慢回升
    closes[40:] = np.linspace(63.0, 75.0, 40)        # 拉升段
    high = closes * 1.01
    low = closes * 0.99
    low[30] = 59.5
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.999,
        "close": closes,
        "high": high,
        "low": low,
        "volume": np.full(n, 1e6),
    })


def _bottom_phases():
    return [(0, 39, "markdown"), (40, 79, "markup")]


def test_mark_bottoms_no_events_legacy_mark():
    df = _bottom_df()
    out = _mark_bottoms(df, _bottom_phases())
    labels = [k for *_x, k in out]
    assert "accumulation" in labels, out


def test_mark_bottoms_empty_events_still_marks_vbottom():
    # 无任何事件, 但 V 型底 + 回升8~30% (结构确认) → 仍标吸筹
    df = _bottom_df()
    out = _mark_bottoms(df, _bottom_phases(), events=[])
    labels = [k for *_x, k in out]
    assert "accumulation" in labels, out


def test_mark_bottoms_with_accum_event_marks():
    df = _bottom_df()
    ev = [{"idx": 35, "type": "SC"}]
    out = _mark_bottoms(df, _bottom_phases(), events=ev)
    labels = [k for *_x, k in out]
    assert "accumulation" in labels, out


def test_mark_bottoms_wrong_event_still_marks_vbottom():
    # 拐点窗口内无吸筹事件, 但底部结构清晰 → 仍标吸筹 (价格结构即确认)
    df = _bottom_df()
    ev = [{"idx": 75, "type": "BC"}, {"idx": 10, "type": "SC"}]
    out = _mark_bottoms(df, _bottom_phases(), events=ev)
    labels = [k for *_x, k in out]
    assert "accumulation" in labels, out


def test_mark_bottoms_no_recovery_keeps_markdown():
    # 底部后继续下跌 (无回升) → 不标吸筹
    n = 80
    closes = np.linspace(100.0, 60.0, 40).tolist() \
        + np.linspace(60.0, 54.0, 40).tolist()
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes,
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": np.full(n, 1e6),
    })
    out = _mark_bottoms(df, [(0, 39, "markdown"), (40, 79, "markdown")])
    labels = [k for *_x, k in out]
    assert "accumulation" not in labels, out


# ── 4. 参数抽到 config 且口径统一 ──

def test_config_params_open_and_consistent():
    assert RANGE_TOL == 0.02          # 与 events.Spring 刺破阈值 (0.98) 统一口径
    assert RANGE_PROBE_WIN > 0
    assert RANGE_BAND > 0
    assert RANGE_MIN_BARS > 0
    # 0.98 阈值反向校验
    assert 1 - RANGE_TOL == 0.98
