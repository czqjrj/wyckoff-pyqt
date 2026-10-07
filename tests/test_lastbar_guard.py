"""末 bar (未确认实时/虚拟枢轴) 守卫回归测试 — 与 LPSY 修复同型。

根因: find_pivots 追加 idx==n-1 的虚拟枢轴, 实时合并 bar (datasource.merge_realtime_bar
合并的盘中高低/收盘) 尚未定型, 方向性事件在末 bar 触发会产生假信号。修复统一
要求各方向性检测器排除 idx==n-1, 并用正/反用例锁定行为。

覆盖: SOW/Shakeout (含 else 分支误标)、Spring、SOS、LPS/BU、JOC (bar 型)。
每个检测器均含: 末 bar 触发 → 无事件; 真实枢轴/确认 bar 触发 → 仍正常出信号。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import events as E
from wyckoff.indicators import add_indicators


def _mkdf(close, high=None, low=None, vol=None, high_at=None, days=None):
    n = len(close)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    h = np.maximum(open_, close) * 1.004
    lo = np.minimum(open_, close) * 0.996
    if high_at:
        for i, p in high_at:
            h[i] = p
    if high is not None:
        h = high
    if low is not None:
        lo = low
    v = np.full(n, 1e6) if vol is None else vol
    return add_indicators(pd.DataFrame({
        "day": (pd.date_range("2023-01-01", periods=n, freq="D") if days is None else days),
        "open": open_, "high": h, "low": lo, "close": close, "volume": v,
    }), symbol="synthetic")


def _piv(entries):
    out = [{"type": t, "idx": i, "price": p, "date": pd.Timestamp("2023-01-01")}
           for i, t, p in entries]
    out.sort(key=lambda x: x["idx"])
    return out


def _base(*items):
    return [{"type": t, "idx": i, "price": p, "date": pd.Timestamp("2023-01-01")}
            for t, i, p in items]


# ── SOW / Shakeout ────────────────────────────────────────────────
def test_sow_none_when_breakdown_at_last_bar():
    """末 bar 放量破位 + 无未来确认窗口 → 不得标 SOW/Shakeout/TSO。"""
    n = 80
    close = np.concatenate([np.linspace(8.0, 9.9, 71), [9.55, 9.5, 9.45] + [9.4] * 6])
    high = np.maximum(np.roll(close, 1), close) * 1.004
    high[59] = 10.0
    low = np.minimum(np.roll(close, 1), close) * 0.996
    low[64] = 8.9
    low[79] = 9.4                          # 破位低点在末 bar
    vol = np.full(n, 1e6)
    vol[79] = 4e6
    df = _mkdf(close, high=high, low=low, vol=vol)
    base3 = _base(("BC", 59, 10.0))
    set = E.detect_sow(df, _piv([(59, "high", 10.0), (64, "low", 8.9), (79, "low", 9.4)]), base3)
    assert set == [], f"末 bar 未确认破位不应出任何 SOW 系事件, 实际={set}"


def test_shakeout_still_fires_on_confirmed_bar():
    """真实枢轴破位 + 未来快速收回 → Shakeout 仍正常触发。"""
    n = 80
    close = np.concatenate([np.linspace(8.0, 9.9, 71), [9.55, 10.05] + [9.9] * 7])
    high = np.maximum(np.roll(close, 1), close) * 1.004
    high[59] = 10.0
    low = np.minimum(np.roll(close, 1), close) * 0.996
    low[64] = 8.9
    low[78] = 9.6                          # 浅破位 (真实枢轴)
    vol = np.full(n, 1e6)
    vol[78] = 4e6
    df = _mkdf(close, high=high, low=low, vol=vol)
    ev = E.detect_sow(df, _piv([(59, "high", 10.0), (64, "low", 8.9), (78, "low", 9.6)]),
                      _base(("BC", 59, 10.0)))
    shake = [e for e in ev if e["type"] == "Shakeout" and e["idx"] == 78]
    assert shake, f"真实枢轴破位+收回应标 Shakeout@78, 实际={ev}"


# ── Spring ─────────────────────────────────────────────────────────
def test_spring_not_on_last_bar():
    """末 bar 虚拟低枢轴刺破前低后收回 → 不得标 Spring。"""
    close = np.concatenate([np.linspace(8.0, 9.2, 78), [8.9, 9.25]])
    low = np.minimum(np.roll(close, 1), close) * 0.996
    low[79] = 8.5                          # 末 bar 虚拟低枢轴
    df = _mkdf(close, low=low)
    ev = E.detect_pivot_events(
        df, _piv([(60, "high", 9.3), (75, "low", 9.0), (79, "low", 8.5)]),
        _base(("SC", 30, 8.0)))
    assert not [e for e in ev if e["type"] == "Spring"], f"末 bar 不应标 Spring, 实际={ev}"


def test_spring_still_fires_on_confirmed_low():
    """真实低枢轴刺破后收回 → Spring 仍正常触发。"""
    close = np.concatenate([np.linspace(8.0, 9.2, 78), [8.9, 9.25]])
    low = np.minimum(np.roll(close, 1), close) * 0.996
    low[78] = 8.5                          # 真实低枢轴
    df = _mkdf(close, low=low)
    ev = E.detect_pivot_events(
        df, _piv([(60, "high", 9.3), (75, "low", 9.0), (78, "low", 8.5)]),
        _base(("SC", 30, 8.0)))
    spr = [e for e in ev if e["type"] == "Spring" and e["idx"] == 78]
    assert spr, f"真实低枢轴应标 Spring@78, 实际={ev}"


# ── SOS ────────────────────────────────────────────────────────────
def test_sos_not_on_last_bar():
    """末 bar 收盘站上突破位+放量 → 不得标 SOS。"""
    n = 80
    close = np.linspace(8.0, 9.5, n)
    close[78:] = 9.6
    high = np.maximum(np.roll(close, 1), close) * 1.004
    high[70] = 9.0
    high[79] = 9.75
    vol = np.full(n, 1e6)
    vol[70:79] = 3e6
    vol[79] = 5e6
    df = _mkdf(close, high=high, vol=vol)
    ev = E.detect_pivot_events(
        df, _piv([(70, "high", 9.0), (75, "low", 8.8), (79, "high", 9.75)]),
        _base(("SC", 30, 8.0), ("Spring", 45, 7.8)))
    assert not [e for e in ev if e["type"] == "SOS"], f"末 bar 不应标 SOS, 实际={ev}"


def test_sos_still_fires_on_confirmed_high():
    """真实高枢轴收盘确认+放量 → SOS 仍正常触发。"""
    n = 80
    close = np.linspace(8.0, 9.5, n)
    close[78:] = 9.6
    high = np.maximum(np.roll(close, 1), close) * 1.004
    high[70] = 9.0
    high[78] = 9.7
    vol = np.full(n, 1e6)
    vol[70:79] = 3e6
    vol[79] = 5e6
    df = _mkdf(close, high=high, vol=vol)
    ev = E.detect_pivot_events(
        df, _piv([(70, "high", 9.0), (78, "high", 9.7), (75, "low", 8.8)]),
        _base(("SC", 30, 8.0), ("Spring", 45, 7.8)))
    sos = [e for e in ev if e["type"] == "SOS" and e["idx"] == 78]
    assert sos, f"真实高枢轴应标 SOS@78, 实际={ev}"


# ── LPS / BU (SOS/JOC 后回踩) ──────────────────────────────────────
def _joc_df(joc_at=80, n=100, crash_at=64, low_at=None, low_price=8.3, vol_low_at=None):
    close = np.full(n, 7.7)
    close[20:61] = 7.75
    close[crash_at] = 6.4
    close[joc_at] = 7.97
    close[joc_at + 1:] = np.linspace(7.9, 7.85, n - joc_at - 1)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = close + 0.05
    high[20:61] = 7.85
    high[joc_at] = 8.2
    low = close - 0.05
    low[68] = 7.55
    if low_at is not None:
        low[low_at] = low_price
    vol = np.full(n, 1e6)
    vol[joc_at] = 3e6
    if vol_low_at is not None:
        for i in (vol_low_at if isinstance(vol_low_at, (list, tuple)) else [vol_low_at]):
            vol[i] = 3e5
    return _mkdf(pd.Series(close), high=high, low=low, vol=vol)


def test_bu_lps_not_on_last_bar():
    """SOS/JOC 后缩量回踩的低点在末 bar → 不得标 LPS/BU。"""
    df = _joc_df(joc_at=80, low_at=99, low_price=8.3, vol_low_at=99)
    pivots = _piv([(80, "high", 8.2), (68, "low", 7.55), (99, "low", 8.3)])
    ev = E.detect_joc_lps_bu(df, pivots, _base(("SC", 15, 6.2)))
    assert not [e for e in ev if e["type"] in ("LPS", "BU") and e["idx"] == 99], \
        f"末 bar 低枢轴不应产 LPS/BU, 实际={ev}"


def test_bu_lps_still_fires_on_confirmed_low():
    """同一回踩低点在真实枢轴 (idx<末) → BU/LPS 仍正常触发。"""
    df = _joc_df(joc_at=80, low_at=95, low_price=8.3, vol_low_at=95)
    pivots = _piv([(80, "high", 8.2), (68, "low", 7.55), (95, "low", 8.3)])
    ev = E.detect_joc_lps_bu(df, pivots, _base(("SC", 15, 6.2)))
    pull = [e for e in ev if e["type"] in ("LPS", "BU") and e["idx"] == 95]
    assert pull, f"真实低枢轴应产 LPS/BU@95, 实际={ev}"


# ── JOC (bar 型) ───────────────────────────────────────────────────
def test_joc_not_on_last_bar():
    """bar 型 JOC: 突破出现在末 bar → 不得触发。"""
    df = _joc_df(joc_at=99, crash_at=85)
    ev = E.detect_joc_lps_bu(df, _piv([(68, "low", 7.55)]), _base(("SC", 15, 6.2)))
    assert not [e for e in ev if e["type"] == "JOC"], f"末 bar 不应产 JOC, 实际={ev}"


def test_joc_still_fires_on_confirmed_bar():
    """同一突破在真实 bar (n-10) → JOC 正常触发。"""
    df = _joc_df(joc_at=90, crash_at=85)
    ev = E.detect_joc_lps_bu(df, _piv([(68, "low", 7.55)]), _base(("SC", 15, 6.2)))
    joc = [e for e in ev if e["type"] == "JOC" and e["idx"] == 90]
    assert joc, f"真实 bar 应产 JOC@90, 实际={ev}"
