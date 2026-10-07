"""事件探测阈值统一入口 (A5) 回归测试。

背景: docs/project_quality_plan.md A5 —— events.py 的高潮/SOS/JOC 探测阈值
此前散落为内联魔数, 与 vsa.py 各自一套、易漂移。本轮把阈值抽到
config.REVERSAL_EVENT_THRESHOLDS / SOS_THRESHOLDS 单一来源 (值保持不变)。

本文件锁两件事:
  1. 基线值不被无声改动 (阈值调整必须伴随本文件的显式更新);
  2. events.py 确实读的是共享表 (改表 → 检测行为跟着变), 而非又抄了一份。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import config, events
from wyckoff.indicators import add_indicators

# ── 基线锁 (任何改动都须先复验 survey/回测, 再显式更新此处) ──
BASELINE_REVERSAL = {
    "climax_vol_ok": 1.6,
    "climax_vol_hi": 2.0,
    "climax_wick_min": 0.30,
    "climax_lo_zone": 0.15,
    "climax_hi_zone": 0.10,
    "climax_min_bar": 20,
    "spring_pierce": 0.98,
    "utad_pierce": 1.02,
    "sow_vol_mult": 1.25,
    "sow_floor_pierce": 0.97,
}
BASELINE_SOS = {
    "pivot_vr_min": 1.3,
    "pivot_gap_bars": 30,
    "accum_window": 60,
    "bar_vol_mult": 1.25,
    "joc_vol_mult": 1.8,
    "joc_breakout_mult": 1.01,
    "joc_range_confirm": 0.98,
}


def test_baseline_values_locked():
    """阈值基线与 plan A5 抽取前完全一致 (行为不变的证明)。"""
    assert config.REVERSAL_EVENT_THRESHOLDS == BASELINE_REVERSAL
    assert config.SOS_THRESHOLDS == BASELINE_SOS


def test_events_reads_shared_registry():
    """events.py 持有的是 config 同一对象 (单一来源, 改 config 即全局生效)。"""
    assert events.REVERSAL_EVENT_THRESHOLDS is config.REVERSAL_EVENT_THRESHOLDS
    assert events.SOS_THRESHOLDS is config.SOS_THRESHOLDS


def _sc_df(n=80, crash_at=70):
    """构造一根"放量长下影砸到区间低位"的 SC 形态 K 线序列。"""
    close = np.linspace(20.0, 30.0, n)
    open_ = close.copy()
    high = close * 1.005
    low = close * 0.995
    open_[crash_at] = close[crash_at - 1]
    high[crash_at] = 30.5
    low[crash_at] = 21.0
    close[crash_at] = 24.0
    vol = np.full(n, 1e6)
    vol[crash_at] = 1e7
    return add_indicators(pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": open_, "high": high, "low": low, "close": close, "volume": vol,
    }), symbol="synthetic")


def test_climax_detected_with_default_thresholds():
    """基线阈值下该形态应产出 SC (证明测试夹具本身有效)。"""
    ev = events.detect_climaxes(_sc_df())
    assert [e["type"] for e in ev] == ["SC"], f"应仅产 SC, 实际={ev}"


def test_climax_threshold_change_drives_behavior(monkeypatch):
    """改共享表的量比下限 → 检测结果随之变化 (单一来源证明, 非复制粘贴)。"""
    monkeypatch.setitem(config.REVERSAL_EVENT_THRESHOLDS, "climax_vol_ok", 999.0)
    assert events.detect_climaxes(_sc_df()) == [], "抬高量比门后不应再出 SC"

    monkeypatch.setitem(config.REVERSAL_EVENT_THRESHOLDS, "climax_wick_min", 0.99)
    assert events.detect_climaxes(_sc_df()) == [], "抬高影线门后不应再出 SC"


def _sos_df(n=100, breakout_at=65, amp=2.0, breakout_close=27.2):
    """bar 型 SOS 夹具: 前 30 根高低交替 (boll 带宽 ~2, 让突破棒落在带内) +
    第 65 根放量收在 30 日收盘高点之上 (量比 1.46 过 1.25 门、不过 1.8 门)。
    组合出 boll_pct≈0.75 (不过 sos_joc_boll_cap 0.8 门) 且 raw_sos=True。"""
    close = np.full(n, 25.0)
    close[35:breakout_at] = np.where(
        np.arange(breakout_at - 35) % 2 == 0, 25.0 - amp, 25.0 + amp)
    close[breakout_at] = breakout_close
    vol = np.full(n, 1e6)
    vol[breakout_at] = 1.5e6
    return add_indicators(pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": close, "high": close + 0.05, "low": close - 0.05,
        "close": close, "volume": vol,
    }), symbol="synthetic")


def test_sos_detected_with_default_thresholds():
    """基线阈值下夹具应产出 bar 型 SOS (前置: 吸筹背景 SC 由 base_events 提供)。"""
    df = _sos_df()
    base = [{"type": "SC", "idx": 30, "price": float(df["close"].iloc[30]),
             "date": pd.Timestamp("2023-01-01")}]
    ev = events.detect_joc_lps_bu(df, [], base)
    assert any(e["type"] == "SOS" for e in ev), f"应产 SOS, 实际={ev}"


def test_sos_threshold_change_drives_behavior(monkeypatch):
    """SOS/JOC 放量门同样来自共享表: 抬门即滤掉突破事件。"""
    df = _sos_df()
    base = [{"type": "SC", "idx": 30, "price": float(df["close"].iloc[30]),
             "date": pd.Timestamp("2023-01-01")}]
    assert any(e["type"] == "SOS" for e in events.detect_joc_lps_bu(df, [], base))

    monkeypatch.setitem(config.SOS_THRESHOLDS, "bar_vol_mult", 999.0)
    gated = events.detect_joc_lps_bu(df, [], base)
    assert not any(e["type"] in ("SOS", "JOC") for e in gated), \
        "抬高放量门后突破事件应被滤掉"


def test_every_registry_key_is_consumed_by_events():
    """静态锁: 表里每个键都真被 events.py 引用 (防有人改回内联魔数留死键)。"""
    import inspect

    src = inspect.getsource(events)
    for k in BASELINE_REVERSAL:
        assert f'REVERSAL_EVENT_THRESHOLDS["{k}"]' in src, f"键 {k} 未被 events 消费"
    for k in BASELINE_SOS:
        assert f'SOS_THRESHOLDS["{k}"]' in src, f"键 {k} 未被 events 消费"

