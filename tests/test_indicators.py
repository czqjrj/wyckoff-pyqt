"""技术指标 (wyckoff/indicators.py) 回归测试。

核心: 数据中混入单根缺失 (NaN) 时, 指标列不得被 cumsum 传染成整列 NA —
早期 _ewma 用 np.cumsum 实现, 一根 NaN 会把整条 MACD 刷成空白。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.indicators import _ewma, add_indicators


def test_ewma_nan_does_not_poison_whole_column():
    rng = np.random.default_rng(0)
    arr = 30 + np.cumsum(rng.normal(0, 1, 400))
    arr[250] = np.nan
    arr[-1] = np.nan
    out = _ewma(arr, 26)
    assert np.isfinite(out).sum() == len(arr) - 1  # 仅尾部无后续有效点时缺失
    assert np.isnan(out[-1])
    assert np.isfinite(out[3]) and np.isfinite(out[250])


def test_ewma_matches_legacy_values_on_clean_data():
    rng = np.random.default_rng(1)
    arr = 20 + np.cumsum(rng.normal(0, 0.5, 500))
    for span in (3, 9, 12, 26):
        legacy = _ewma(np.nan_to_num(arr), span)
        np.testing.assert_allclose(_ewma(arr, span), legacy, atol=1e-9)


def test_macd_not_blank_with_trailing_nan_close():
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
        assert ind[col].notna().sum() == n - 1