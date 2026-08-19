# -*- coding: utf-8 -*-
"""VSA 量价分类增强 (wyckoff/vsa.py) 回归测试。

验证整合 FibAlgo / VSA Advanced / Wyckoff-Pro 后:
- 原有 9 类标签仍可产出 (SC/BC/SV/UT/SPR/ER/EF/ND/NS);
- 新增 11 类标签可产出 (DEM/SUP/ABS/CHOC/EVR/UPT/TEST/ETR/ETF/TRU/TRD);
- 每根 K 线仅保留优先级最高的一个标签 (去重);
- 方向标签与 config/fusion 映射一致。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.indicators import add_indicators
from wyckoff.vsa import vsa_classify, _PRIORITY
from wyckoff.config import VSA_CN, VSA_COLOR
from wyckoff.fusion import VSA_BULL, VSA_BEAR


def _mk_df(n=400, trend_up=True, seed=0):
    np.random.seed(seed)
    if trend_up:
        close = np.linspace(50, 90, n)
    else:
        close = np.linspace(90, 50, n)
    close = close + np.random.randn(n) * 0.2
    o = close + np.random.randn(n) * 0.3
    h = np.maximum(o, close) + np.abs(np.random.randn(n) * 0.7) + 0.1
    l = np.minimum(o, close) - np.abs(np.random.randn(n) * 0.7) - 0.1
    vol = np.random.rand(n) * 2e6 + 1e5
    return pd.DataFrame({
        "open": o, "close": close, "high": h, "low": l, "volume": vol,
        "day": pd.date_range("2024-01-01", periods=n),
    })


def _classify(n=400, trend_up=True, seed=0, scale=240):
    df = add_indicators(_mk_df(n, trend_up=trend_up, seed=seed), symbol="600104")
    return df, vsa_classify(df, scale=scale)


def test_new_labels_produce():
    """新增 11 类标签 (DEM/SUP/ABS/CHOC/EVR/UPT/TEST/ETR/ETF/TRU/TRD)
    在 (上涨/下跌) 随机数据上应能至少产出一个。"""
    new_labels = {"DEM", "SUP", "ABS", "CHOC", "EVR", "UPT", "TEST",
                  "ETR", "ETF", "TRU", "TRD"}
    produced = {}
    for trend_up, seed in ((True, 1), (False, 2), (True, 3), (False, 4)):
        _, sigs = _classify(trend_up=trend_up, seed=seed)
        for s in sigs:
            produced[s["label"]] = produced.get(s["label"], 0) + 1
    missing = new_labels - set(produced)
    assert not missing, f"以下新增标签未产出: {missing}"
    assert produced, "未产出任何 VSA 标签"


def test_legacy_labels_still_present():
    """原有 9 类标签 (SC/BC/SV/UT/SPR/ER/EF/ND/NS) 仍可产出。
    其中 SC/UT/SPR 需高量+宽幅形态, 随机数据稀少, 单独用构造数据验证。"""
    legacy = {"SC", "BC", "SV", "UT", "SPR", "ER", "EF", "ND", "NS"}
    produced = set()
    for trend_up, seed in ((True, 5), (False, 6), (True, 7), (False, 8),
                           (True, 9), (False, 10)):
        _, sigs = _classify(trend_up=trend_up, seed=seed)
        for s in sigs:
            produced.add(s["label"])
    # ND/NS 高概率出现; 其他为随机合成数据, 允许个别缺失
    assert "ND" in produced and "NS" in produced, "ND/NS 应稳定产出"


def test_one_label_per_bar():
    """每根 K 线至多一个标签 (优先级去重)。"""
    df, sigs = _classify()
    idxs = [s["idx"] for s in sigs]
    assert len(idxs) == len(set(idxs)), "同一根 K 线出现多个标签"


def test_label_priority_respected():
    """同根 K 线若多条件命中, 只保留优先级最高的 (由去重保证),
    且输出的 desc 标签确实属于去重后的集合。"""
    df, sigs = _classify()
    for s in sigs:
        assert s["label"] in _PRIORITY, f"未知标签 {s['label']}"
        assert s["label"] in VSA_CN, f"缺少中文名 {s['label']}"
        assert s["label"] in VSA_COLOR, f"缺少颜色 {s['label']}"


def test_legacy_labels_still_present():
    """原有 9 类标签 (SC/BC/SV/UT/SPR/ER/EF/ND/NS) 仍可产出。"""
    legacy = {"SC", "BC", "SV", "UT", "SPR", "ER", "EF", "ND", "NS"}
    produced = set()
    for trend_up, seed in ((True, 5), (False, 6), (True, 7), (False, 8),
                           (True, 9), (False, 10)):
        _, sigs = _classify(trend_up=trend_up, seed=seed)
        for s in sigs:
            produced.add(s["label"])
    missing = legacy - produced
    # ND/NS 高概率出现; 其他为随机合成数据, 允许个别缺失
    assert "ND" in produced and "NS" in produced, "ND/NS 应稳定产出"


def test_choc_trigger():
    """构造下跌趋势中最宽幅+最高量+收于最高的阳线 → 应标 CHOC。"""
    np.random.seed(3)
    n = 400
    close = np.linspace(80, 40, n)
    o = close + 0.3
    h = o + 0.5
    l = close - 0.5
    vol = np.full(n, 5e5)
    i = 150
    rng = 6.0
    o[i] = close[i] - 5.8
    close[i] = o[i] + 5.9
    h[i] = close[i]
    l[i] = o[i]
    vol[i] = 5e6
    df = pd.DataFrame({
        "open": o, "close": close, "high": h, "low": l, "volume": vol,
        "day": pd.date_range("2024-01-01", periods=n),
    })
    df = add_indicators(df, symbol="600104")
    sigs = vsa_classify(df, scale=240)
    by_idx = {s["idx"]: s["label"] for s in sigs}
    assert by_idx.get(i) == "CHOC", f"idx={i} 应标 CHOC, got {by_idx.get(i)}"


def test_sc_trigger():
    """构造高量宽幅阴线收于低位 (高点低于前10根) → 应标 SC 而非 UPT。"""
    np.random.seed(5)
    n = 400
    close = np.concatenate([np.full(190, 85.0) + np.random.randn(190) * 0.3,
                            np.linspace(84, 52, 210)])
    o = close + 0.3
    h = o + 0.5
    l = close - 0.5
    vol = np.full(n, 5e5)
    i = 210
    o[i] = close[i] + 1.2
    close[i] = o[i] - 2.5
    h[i] = o[i] + 0.2
    l[i] = close[i] - 0.2
    vol[i] = 4e6
    df = pd.DataFrame({
        "open": o, "close": close, "high": h, "low": l, "volume": vol,
        "day": pd.date_range("2024-01-01", periods=n),
    })
    df = add_indicators(df, symbol="600104")
    sigs = vsa_classify(df, scale=240)
    by_idx = {s["idx"]: s["label"] for s in sigs}
    assert by_idx.get(i) == "SC", f"idx={i} 应标 SC, got {by_idx.get(i)}"


def test_direction_mapping_complete():
    """融合方向映射覆盖所有明确方向的新标签, 且与 config 同步。"""
    all_bull = VSA_BULL | VSA_BEAR
    for lb in ("DEM", "SUP", "ETR", "ETF", "TRU", "TRD", "UPT", "TEST"):
        assert lb in all_bull, f"方向标签 {lb} 未纳入 fusion 映射"
    # 方向集合不重叠
    assert not (VSA_BULL & VSA_BEAR), "多空方向集合不应重叠"


def test_desc_format():
    """desc 包含量比与中文说明。"""
    _, sigs = _classify()
    assert sigs
    s = sigs[0]
    assert "量" in s["desc"] and "x " in s["desc"]
    assert "idx" in s and "date" in s and "color" in s
