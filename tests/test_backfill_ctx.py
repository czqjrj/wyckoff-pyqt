# -*- coding: utf-8 -*-
"""backfill_ctx 回填逻辑测试 (离线: conftest 设 WYCKOFF_NO_NET=1)。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.backfill_ctx import (_has_ctx, backfill_feedback,
                                  backfill_one_signal, backfill_signals)
from wyckoff.context import CONTEXT_FEAT_KEYS


def _kline(n=320, seed=3):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 10 + np.sin(t / 18.0) * 0.8 + rng.normal(0, 0.05, n)
    high = close + np.abs(rng.normal(0.08, 0.03, n))
    low = close - np.abs(rng.normal(0.08, 0.03, n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.abs(1e6 + rng.normal(0, 1e5, n))
    day = pd.date_range("2024-01-01", periods=n, freq="B")
    from wyckoff.indicators import add_indicators
    return add_indicators(pd.DataFrame({
        "day": day, "open": open_, "high": high, "low": low,
        "close": close, "volume": vol}))


def test_backfill_one_signal_fills_ctx():
    df = _kline()
    sig_day = df["day"].iloc[250]
    rec = {"kind": "event", "type": "Spring", "date": str(sig_day.date()),
           "conf": 60, "scale": 240, "symbol": "sh600104",
           "features": {"vr": 1.4, "dir": 1}, "results": {}}
    assert backfill_one_signal(rec, df)
    f = rec["features"]
    for k in CONTEXT_FEAT_KEYS:
        assert k in f, f"缺 {k}"
    assert f["dir"] == 1 and f["vr"] == 1.4  # 原特征保留


def test_backfill_one_signal_is_causal():
    """信号日之后的行情突变不得影响回填出的语境特征。"""
    df = _kline()
    sig_day = df["day"].iloc[250]
    rec_a = {"kind": "event", "type": "Spring", "date": str(sig_day.date()),
             "conf": 60, "scale": 240, "features": {"dir": 1}}
    rec_b = dict(rec_a, features={"dir": 1})
    backfill_one_signal(rec_a, df)

    df2 = df.copy()
    df2.loc[290:, ["open", "high", "low", "close"]] *= 3
    df2.loc[290:, "volume"] *= 10
    backfill_one_signal(rec_b, df2)

    fa, fb = rec_a["features"], rec_b["features"]
    for k in CONTEXT_FEAT_KEYS:
        assert fa[k] == fb[k], f"{k}: {fa[k]} vs {fb[k]}"


def test_backfill_signals_stats():
    df = _kline()
    day = str(df["day"].iloc[250].date())
    recs = [
        {"kind": "event", "type": "SOS", "date": day, "scale": 240,
         "symbol": "sh600104", "features": {"dir": 1}},
        {"kind": "event", "type": "SC", "date": day, "scale": 240,
         "symbol": "sz000001", "features": {}},
        {"kind": "vsa", "type": "BU", "date": day, "scale": 240,
         "symbol": "sh600104", "features": None},
        {"kind": "event", "type": "LPS", "date": day, "scale": 30,
         "symbol": "sh600104", "features": {}},
    ]
    pool = {"sh600104": df}  # sz000001 无数据

    def kfn(sym):
        return pool.get(sym)

    st = backfill_signals(recs, kfn)
    assert st["scan"] == 3          # vsa 不算 event
    assert st["todo"] == 1          # 只有 sh600104 那条可回填
    assert st["ok"] == 1
    assert st["skip"] >= 2          # 无数据 + 分钟级
    assert _has_ctx(recs[0]["features"])
    assert not _has_ctx(recs[1].get("features"))


def test_backfill_feedback_relocates_by_date():
    from wyckoff.storage import build_feedback_record  # noqa: F401 确认可导入
    df = _kline()
    a_day = str(df["day"].iloc[100].date())
    e_day = str(df["day"].iloc[200].date())
    recs = [{"symbol": "sh600104", "scale": 240, "label": "accumulation",
             "label_cn": "吸筹", "start_dt": a_day, "end_dt": e_day,
             "net": 0.05, "verdict": "correct", "features": {}}]

    def kfn(sym):
        return df if sym == "sh600104" else None

    st = backfill_feedback(recs, kfn)
    assert st["ok"] == 1
    f = recs[0]["features"]
    for k in ("lo1", "lo2", "hi1", "hi2"):
        assert k in f
