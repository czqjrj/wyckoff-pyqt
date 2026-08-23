"""反面证据积分 (wyckoff/counterevidence.py) 回归测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.counterevidence import RED_TH, _alert_level, ce_lines, counter_evidence


def _mk_df(closes, opens=None, highs=None, lows=None, volumes=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens, dtype=float) if opens is not None else closes * 0.999
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(closes, opens) * 1.005
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(closes, opens) * 0.995
    volumes = np.asarray(volumes, dtype=float) if volumes is not None else np.full(n, 1e6)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": opens, "close": closes, "high": highs, "low": lows,
        "volume": volumes,
    })


def _ev(etype, idx, price):
    return {"type": etype, "idx": idx, "date": None, "price": price, "desc": ""}


def test_alert_levels():
    assert _alert_level(0) == "NONE"
    assert _alert_level(35) == "YELLOW"
    assert _alert_level(60) == "ORANGE"
    assert _alert_level(RED_TH + 1) == "RED"


def test_accumulation_hypothesis_counter():
    """吸筹假设下, UTAD/BC 应计入反面积分。"""
    n = 150
    closes = np.linspace(20, 18, n) + np.sin(np.arange(n) / 5) * 0.3
    df = _mk_df(closes)
    events = [_ev("SC", 30, 17.5), _ev("AR", 45, 19.0),
              _ev("Spring", 100, 17.4), _ev("UTAD", 120, 20.1),
              _ev("BC", 130, 20.3)]
    ce = counter_evidence(df, events, phase="底部整固 (Accumulation)")
    assert ce["hypothesis"] == "吸筹"
    names = {e["name"] for e in ce["events"]}
    assert "UTAD" in names and "BC" in names
    # UTAD+BC(+NO_DEMAND) 加分后, Spring成功(-25) 消减; 分数仍应体现反面信号存在
    assert ce["score"] >= 25
    assert ce["reversal"] is False  # 未到红线


def test_distribution_hypothesis_counter():
    """派发假设下, Spring/SOS 应计入反面积分。"""
    n = 150
    closes = np.linspace(20, 22, n) + np.sin(np.arange(n) / 4) * 0.3
    df = _mk_df(closes)
    events = [_ev("BC", 30, 20.5), _ev("AR", 50, 20.8),
              _ev("Spring", 100, 21.0), _ev("SOS", 120, 21.6)]
    ce = counter_evidence(df, events, phase="顶部构筑 (Distribution)")
    assert ce["hypothesis"] == "派发"
    names = {e["name"] for e in ce["events"]}
    assert "Spring" in names and "SOS" in names


def test_sc_break_triggers_reversal():
    """SC 低点被有效跌破 + 后续派发事件 → 触发紧急反转。"""
    n = 140
    closes = np.linspace(20, 15, n)
    df = _mk_df(closes)
    events = [_ev("SC", 40, 17.0), _ev("AR", 55, 18.0),
              _ev("UTAD", 90, 18.5), _ev("BC", 100, 18.6)]
    ce = counter_evidence(df, events, phase="底部整固 (Accumulation)")
    names = {e["name"] for e in ce["events"]}
    assert "SC_BREAK" in names and "UTAD" in names
    # SC_BREAK35 + UTAD25 + BC15 + NO_DEMAND10 = 85 ≥ 71
    assert ce["score"] >= RED_TH
    assert ce["reversal"] is True
    assert ce["reversal_reason"]


def test_undetermined_hypothesis():
    ce = counter_evidence(_mk_df([10] * 100), [], phase="区间整理")
    assert ce["hypothesis"] == "未定"
    assert ce["score"] == 0.0


def test_ce_lines_render():
    n = 140
    closes = np.linspace(20, 15, n)
    df = _mk_df(closes)
    events = [_ev("SC", 40, 17.0), _ev("AR", 55, 18.0),
              _ev("UTAD", 90, 18.5), _ev("BC", 100, 18.6)]
    ce = counter_evidence(df, events, phase="底部整固 (Accumulation)")
    lines = ce_lines(ce)
    assert any("红灯" in ln for ln in lines)
    assert any("紧急反转" in ln for ln in lines)
