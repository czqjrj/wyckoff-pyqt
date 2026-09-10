"""技术指标 (wyckoff/indicators.py) 回归测试。

核心: 数据中混入单根缺失 (NaN) 时, 指标列不得被 NaN 传染成整列空 —
_ewma 使用标准前向递推 EMA, NaN 位置 carry-forward 前值。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.indicators import _ewma, add_indicators


def _reference_ema(arr, span):
    """标准前向递推 EMA (参考实现, 用于回归验证)。"""
    arr = np.asarray(arr, dtype=float)
    alpha = 2.0 / (span + 1)
    out = np.empty(len(arr), dtype=float)
    out[:] = np.nan
    first = 0
    while first < len(arr) and not np.isfinite(arr[first]):
        first += 1
    if first >= len(arr):
        return out
    out[first] = arr[first]
    for i in range(first + 1, len(arr)):
        if np.isfinite(arr[i]):
            out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
        else:
            out[i] = out[i - 1]
    return out


def test_ewma_nan_does_not_poison_whole_column():
    """NaN 位置 carry-forward 前值, 不产生新 NaN (首段 NaN 除外)。"""
    rng = np.random.default_rng(0)
    arr = 30 + np.cumsum(rng.normal(0, 1, 400))
    arr[250] = np.nan
    arr[-1] = np.nan
    out = _ewma(arr, 26)
    ref = _reference_ema(arr, 26)
    np.testing.assert_allclose(out, ref, atol=1e-12)
    # NaN carry-forward: 所有位置都有值 (首个有效值之后)
    assert np.isfinite(out).sum() == len(arr)
    assert np.isfinite(out[3]) and np.isfinite(out[250])


def test_ewma_matches_reference_on_clean_data():
    """无 NaN 时 _ewma 与标准前向递推 EMA 逐位一致。"""
    rng = np.random.default_rng(1)
    arr = 20 + np.cumsum(rng.normal(0, 0.5, 500))
    for span in (3, 9, 12, 26):
        ref = _reference_ema(arr, span)
        np.testing.assert_allclose(_ewma(arr, span), ref, atol=1e-9)


def test_macd_not_blank_with_trailing_nan_close():
    """尾部 close=NaN 时 MACD 列不全空 (carry-forward 保留有效值)。"""
    n = 200
    rng = np.random.default_rng(2)
    close = 20 + np.cumsum(rng.normal(0, 0.5, n))
    base = pd.DataFrame({
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.full(n, 1e6),
        "day": [f"D{i}" for i in range(n)]})
    d = base.copy()
    d.loc[d.index[-1], "close"] = np.nan  # 最新一根 close 缺失 (未收盘/停牌)
    ind = add_indicators(d)
    for col in ("macd_dif", "macd_dea", "macd_hist"):
        assert ind[col].notna().sum() == n  # carry-forward: 全部有值