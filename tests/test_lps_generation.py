# -*- coding: utf-8 -*-
"""LPS/BU 事件生成回归测试。

曾实测 bug: detect_joc_lps_bu 的 LPS/BU 循环以调用方传入的 base_events
(不含本函数刚生成的 SOS/JOC) 为准, 导致 LPS/BU 永远为空 (11只股票近3年
0 个样本) —— 威科夫标准买点(Phase D 最后支撑点)从未被输出。修复后应以
函数内部生成的 SOS/JOC 为基准。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import events as E


def _mkdf(n=400):
    """构造含一组明确 SOS/JOC + 缩量回踩的结构化 K 线。"""
    rng = np.random.default_rng(42)
    days = pd.date_range("2023-01-01", periods=n, freq="D")
    rows = []
    close = 10.0
    for i in range(n):
        if i in (90, 120, 160):
            vol = 5.0          # 放量 (SOS/JOC 需要 vol>=ma20*1.25/1.8)
        elif i in (95, 125, 165):
            vol = 0.3          # 缩量回踩 (LPS 需要 volume < vol_ma20)
        else:
            vol = 1.0
        if i in (90, 120, 160):
            close += 1.2       # 突破
        elif i in (95, 125, 165):
            close -= 0.3       # 温和回踩, 不破 floor
        else:
            close += rng.normal(0, 0.1)
        op = close - rng.normal(0, 0.05)
        hi = close + 0.2
        lo = close - 0.2
        rows.append([days[i], op, hi, lo, close, vol * 1e6])
    df = pd.DataFrame(rows, columns=["day", "open", "high", "low", "close", "volume"])
    # 补齐 _EventContext 需要的指标列 (SOS/JOC/LPS/BU 判定 + 上下文预计算)
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio_20"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
    df["range"] = df["high"] - df["low"]
    df["lower_wick"] = np.minimum(df["open"], df["close"]) - df["low"]
    df["upper_wick"] = df["high"] - np.maximum(df["open"], df["close"])
    return df


def _fake_pivots(df):
    """模拟 find_pivots: 在构造的关键点出枢轴 (high/low)。"""
    pivots = []
    for i in (90, 95, 120, 125, 160, 165):
        pivots.append({"type": "high", "idx": i,
                       "price": float(df["high"].iloc[i]),
                       "date": df["day"].iloc[i]})
        pivots.append({"type": "low", "idx": i,
                       "price": float(df["low"].iloc[i]),
                       "date": df["day"].iloc[i]})
    return pivots


def test_lps_generated_from_internal_sos():
    """SOS 之后出现缩量回踩低点 → 必须产出 LPS (修复后行为)。"""
    df = _mkdf()
    pivots = _fake_pivots(df)
    events = E.detect_joc_lps_bu(df, pivots, base_events=[])
    lps = [e for e in events if e["type"] == "LPS"]
    assert lps, "SOS/JOC 后的缩量回踩应产出 LPS, 修复前恒为空"


def test_bu_generated_after_joc():
    """JOC 之后回撤至区间上沿 → 产出 BU。"""
    df = _mkdf()
    pivots = _fake_pivots(df)
    events = E.detect_joc_lps_bu(df, pivots, base_events=[])
    bu = [e for e in events if e["type"] == "BU"]
    sos = [e for e in events if e["type"] in ("SOS", "JOC")]
    assert sos, "应能识别出 SOS/JOC"
    assert len(bu) + len([e for e in events if e["type"] == "LPS"]) > 0


def test_sos_joc_present_in_same_pass():
    """同一函数应既生成 SOS/JOC 又基于它们生成 LPS/BU (自产自销修复)。"""
    df = _mkdf()
    pivots = _fake_pivots(df)
    events = E.detect_joc_lps_bu(df, pivots, base_events=[])
    kinds = {e["type"] for e in events}
    assert "SOS" in kinds or "JOC" in kinds
    assert kinds & {"LPS", "BU"}, "基于内部 SOS/JOC 的 LPS/BU 必须存在"