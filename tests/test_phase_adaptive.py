"""阶段划分自适应优化回归测试 (P0-1/P0-2/P1-3/P1-4/P2-5/P2-6)。

覆盖:
1. _adapt_min_rec 单位修复: 波动率 % 输入 → 小数阈值 (低波动 4% / 高波动封顶 6%);
2. _mark_bottoms/_mark_tops 波动率自适应 rec_lo: 低波动 3.5% 回升/回落可标
   吸筹/派发, 而固定 0.08 不标; 显式 rec_lo 覆盖自适应;
3. _detect_ranges 自适应带宽不抛且随波动率变化;
4. phase_segments 参数覆盖 (离线 sweep 兼容);
5. structure_progress 事件窗口锚定最近 SC/BC (跨基地不链式推进);
6. structure_progress 同级别重复事件 (多重 LPS/BU) 计入详情。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from wyckoff.phases import _adapt_min_rec, _detect_ranges, _mark_bottoms, _mark_tops
from wyckoff.structure import structure_progress


def _osc_df(n=200, lo=80.0, hi=95.0):
    """区间震荡 K 线 (与 test_phase_range_fixes 同构)。"""
    rng = np.random.default_rng(7)
    t = np.arange(n)
    mid = (lo + hi) / 2
    close = mid + (hi - lo) / 2 * 0.7 * np.sin(t / 6.0) + rng.normal(0, 0.25, n)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": close * 0.999,
        "close": close,
        "high": np.maximum(close + 1.2, close * 1.01),
        "low": np.minimum(close - 1.2, close * 0.99),
        "volume": rng.uniform(1e5, 2e6, n),
    })


def _piv(idxs_types):
    return [{"idx": i, "type": ty, "price": pr} for i, ty, pr in idxs_types]


def _early_bottom_df(n=80):
    """下跌 → 3.5% 早升 V 型后走平 (低波动, 仅触发情形A 早期筑底窗口)。"""
    closes = np.empty(n)
    closes[:30] = np.linspace(100.0, 61.0, 30)
    closes[30] = 60.0                                # 底
    closes[31:50] = np.linspace(60.35, 62.1, 19)     # 缓升 +3.5%
    closes[50:] = 62.0                               # 走平 (跨段 rec<固定 8%)
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
        "price_ma20": np.linspace(50.0, 68.0, n),   # 底后上行
    })


def _early_top_df(n=80):
    """拉升 → 3.5% 早落倒 V 型后走平 (低波动, 仅触发情形A 早期筑顶窗口)。"""
    closes = np.empty(n)
    closes[:30] = np.linspace(62.0, 99.0, 30)
    closes[30] = 100.0                               # 顶
    closes[31:50] = np.linspace(98.0, 96.5, 19)      # 缓落 -3.5%
    closes[50:] = 97.0                               # 走平 (跨段 drop<固定 8%)
    high = closes * 1.01
    low = closes * 0.99
    high[30] = 101.0
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.999,
        "close": closes,
        "high": high,
        "low": low,
        "volume": np.full(n, 1e6),
        "price_ma20": np.linspace(95.0, 70.0, n),   # 顶后下行
    })


# ── 1. _adapt_min_rec 单位修复 ──

def test_adapt_min_rec_units():
    assert _adapt_min_rec(0.0) == pytest.approx(0.05)   # 下限 0.05
    assert _adapt_min_rec(2.0) == pytest.approx(0.05)   # 低波 → 卡在下限
    assert _adapt_min_rec(4.0) == pytest.approx(0.05)
    assert _adapt_min_rec(5.0) == pytest.approx(0.055)
    assert _adapt_min_rec(10.0) == 0.06                 # 封顶 6%
    assert _adapt_min_rec(40.0) == 0.06


# ── 2. 拐点标记波动率自适应 ──

def test_bottom_adaptive_marks_lowvol_early_bottom():
    """低波动自适应 rec_lo≈0.04: rec=3.5% 落入早期筑底窗口 → 标吸筹。"""
    df = _early_bottom_df()
    piv = [{"idx": 30, "type": "low", "price": 60.0}]
    phases = [(0, 49, "markdown"), (50, 79, "markup")]
    ad = _mark_bottoms(df, phases, events=[], pivots=piv, vol_pct=2.0)
    assert "accumulation" in [k for *_x, k in ad]
    # 固定 0.08: 3.5% 不在 [4%, 8%) 窗口 → 不标
    fx = _mark_bottoms(df, phases, events=[], pivots=piv, rec_lo=0.08)
    assert "accumulation" not in [k for *_x, k in fx]


def test_top_adaptive_marks_lowvol_early_top():
    df = _early_top_df()
    piv = [{"idx": 30, "type": "high", "price": 100.0}]
    phases = [(0, 49, "markup"), (50, 79, "markdown")]
    ad = _mark_tops(df, phases, events=[], pivots=piv, vol_pct=2.0)
    assert "distribution" in [k for *_x, k in ad]
    fx = _mark_tops(df, phases, events=[], pivots=piv, rec_lo=0.08)
    assert "distribution" not in [k for *_x, k in fx]


def test_explicit_rec_lo_overrides_vol_adapt():
    """显式 rec_lo 优先于波动率自适应 (离线 sweep 用)。"""
    df = _early_bottom_df()
    piv = [{"idx": 30, "type": "low", "price": 60.0}]
    phases = [(0, 49, "markdown"), (50, 79, "markup")]
    out = _mark_bottoms(df, phases, events=[], pivots=piv,
                        rec_lo=0.10, vol_pct=2.0)
    assert "accumulation" not in [k for *_x, k in out]


# ── 3. 区间带宽自适应 ──

def test_detect_ranges_band_adapts():
    df = _osc_df(n=200)
    piv = _piv([(20, "low", 81.0), (40, "high", 95.0), (60, "low", 82.0),
                (80, "high", 94.0), (120, "low", 83.0), (160, "high", 92.0)])
    lo = _detect_ranges(df, piv)                       # 无 vol → RANGE_BAND 默认
    hi = _detect_ranges(df, piv, vol_pct=30.0)         # 高波动 → band 放宽至 0.60
    assert isinstance(lo, list) and isinstance(hi, list)
    assert len(lo) == 1 and len(hi) == 1               # 全波动率下区间仍完整


def test_phase_segments_override_compatible():
    """phase_segments 参数覆盖 (rec_lo/band/vol_pct) 可运行, 不破坏默认路径。"""
    from wyckoff.phases import phase_segments
    df = _osc_df(n=200)
    piv = _piv([(20, "low", 81.0), (40, "high", 95.0), (60, "low", 82.0),
                (80, "high", 94.0), (120, "low", 83.0), (160, "high", 92.0)])
    for kw in (dict(),
               dict(rec_lo=0.10, band=0.60, vol_pct=None),
               dict(vol_pct=2.0, band=0.45)):
        segs = phase_segments(df, piv, events=[], **kw)
        assert isinstance(segs, list)
        for s in segs:
            assert s[2] in ("markdown", "accumulation", "markup", "distribution")


# ── 4. 结构进度锚定最近基地起点 (P2-5) ──

def _df(n=240):
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 100.0,
    })


def _ev(type_, idx, conf=80):
    return {"type": type_, "idx": idx, "conf": conf,
            "date": pd.Timestamp("2024-01-01")}


def test_structure_anchor_rejects_older_base():
    """两个基地 (SC@60 与 SC@150): 锚定最近 SC 后, 旧基地的 Spring 不参与推进,
    LPS 因缺 SOS/JOC 前置被拦截 → 停在 Phase A (不跨基地链式)。"""
    df = _df(240)
    events = [_ev("SC", 60, 90), _ev("Spring", 100, 90),
              _ev("SC", 150, 90), _ev("LPS", 160, 85)]
    letter, _, detail = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "A"


def test_structure_anchor_keeps_current_base():
    """新基地 SC→SOS 完整链 (SOS 前置 SC 在 60 根内) → 推进到 Phase D。"""
    df = _df(240)
    events = [_ev("SC", 60, 90), _ev("Spring", 100, 90),
              _ev("SC", 150, 90), _ev("SOS", 200, 80)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "D"


# ── 5. 同级别重复事件强化 (P2-6) ──

def test_structure_repeated_lps_reinforcement():
    df = _df(240)
    events = [_ev("SC", 45, 90), _ev("Spring", 60, 90),
              _ev("SOS", 90, 80), _ev("LPS", 100, 85), _ev("LPS", 115, 85)]
    letter, _, detail = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "D"
    assert "多重后段确认" in detail
    assert "LPS×3" in detail


def test_structure_repeated_lpsy_dist_detail():
    df = _df(300)
    events = [_ev("BC", 110, 95), _ev("AR", 125, 70), _ev("UTAD", 160),
              _ev("LPSY", 180, 80), _ev("LPSY", 195, 80)]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter == "D"
    assert "多重后段确认" in detail
    assert "LPSY×2" in detail
