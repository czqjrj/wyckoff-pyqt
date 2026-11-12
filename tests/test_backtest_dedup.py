"""因果回测去重回归测试 (wyckoff/backtest.py)。

同一历史事件会在多个采样窗口 t 内被重复检出, 旧实现逐次追加收益, 把
1 个事件放大成几十个样本 (UTAD 6 个 → n=264)。这里用固定的假事件验证
按 (类型, idx) 去重后 n 等于真实事件数。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

import wyckoff.backtest as bt


def _mk_df(n=400):
    rng = np.random.default_rng(7)
    close = 50 + np.cumsum(rng.normal(0, 0.3, n))
    return pd.DataFrame({
        "day": pd.date_range("2022-01-01", periods=n, freq="D"),
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "close": close,
        "high": close * 1.02, "low": close * 0.98,
        "volume": rng.uniform(8e5, 1.2e6, n),
    })


def test_backtest_events_dedup_by_event(monkeypatch):
    df = _mk_df()
    ev = [{"type": "UTAD", "idx": 100, "price": 55.0, "confirmed": True},
          {"type": "UTAD", "idx": 130, "price": 56.0, "confirmed": True}]
    monkeypatch.setattr(bt, "find_pivots", lambda d, order=6: [])
    monkeypatch.setattr(bt, "detect_all", lambda d, p: ev)

    res = bt.backtest_events(df, [], horizon=20, min_n=1, cost=0.0)
    assert res["by_type"]["UTAD"]["n"] == 2
    assert res["by_type"]["UTAD"]["n_confirmed"] == 2


def test_backtest_events_confirmed_or(monkeypatch):
    """同一事件早期检出未确认、后期检出已确认 → 去重后仍 n=2 且 confirmed 取或。"""
    df = _mk_df()
    calls = {"n": 0}

    def fake_detect(d, p):
        calls["n"] += 1
        return [{"type": "UTAD", "idx": 100, "price": 55.0,
                 "confirmed": calls["n"] >= 3},
                {"type": "UTAD", "idx": 130, "price": 56.0,
                 "confirmed": True}]

    monkeypatch.setattr(bt, "find_pivots", lambda d, order=6: [])
    monkeypatch.setattr(bt, "detect_all", fake_detect)

    res = bt.backtest_events(df, [], horizon=20, min_n=1, cost=0.0)
    s = res["by_type"]["UTAD"]
    assert s["n"] == 2
    assert s["n_confirmed"] == 2


def test_backtest_vsa_dedup_by_signal(monkeypatch):
    df = _mk_df()
    sig = [{"label": "SC", "idx": 100}]

    def fake_vsa(d, scale=240):
        return sig

    monkeypatch.setattr("wyckoff.vsa.vsa_classify", fake_vsa)
    res = bt.backtest_vsa(df, horizon=20, min_n=1, cost=0.0)
    assert res["by_label"]["SC"]["n"] == 1
