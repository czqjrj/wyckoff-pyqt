"""L5 语境特征 (wyckoff/context.py) 单元测试。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.context import CONTEXT_FEAT_KEYS, SAFE_FILL, enrich


def _osc_df(n=320, seed=7):
    """正弦震荡 K 线 + 生产同款指标列 (add_indicators)。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = 10 + np.sin(t / 18.0) * 0.8
    noise = rng.normal(0, 0.05, n)
    close = base + noise
    high = close + np.abs(rng.normal(0.08, 0.03, n))
    low = close - np.abs(rng.normal(0.08, 0.03, n))
    open_ = np.roll(close, 1) + rng.normal(0, 0.02, n)
    open_[0] = close[0]
    vol = 1e6 + rng.normal(0, 1e5, n)
    day = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "day": day, "open": open_, "high": high, "low": low,
        "close": close, "volume": np.abs(vol),
    })
    from wyckoff.indicators import add_indicators
    return add_indicators(df)


def _ev(type_, idx):
    return {"type": type_, "idx": int(idx), "date": None,
            "price": 10.0, "desc": "", "color": "", "conf": 60}


def _pivots(df):
    from wyckoff.indicators import find_pivots
    return find_pivots(df)


def test_enrich_fills_all_keys():
    df = _osc_df()
    evs = [_ev("Spring", 250), _ev("SOS", 262)]
    enrich(df, _pivots(df), evs)
    f = evs[0]["feat"]
    for k in CONTEXT_FEAT_KEYS:
        assert k in f, f"缺语境特征 {k}"
    # 震荡序列应能识别出阶段或至少不崩溃: one-hot 至多一个为 1
    hot = sum(f[k] for k in ("ph_acc", "ph_dis", "ph_mup", "ph_mkd"))
    assert hot <= 1.0
    # 区间特征: 震荡序列应检出交易区间 → tr_pos 有效值
    assert f["tr_pos"] is None or 0.0 <= f["tr_pos"] <= 1.0
    assert f["vol_shrink"] is None or 0.0 <= f["vol_shrink"] <= 1.0
    # 离线环境 (WYCKOFF_NO_NET=1): 指数相关特征安全缺省
    assert f["rs_pct"] is None
    assert f["idx_align"] == 0
    assert f["sec_pct"] is None


def test_base_len_uses_same_direction_climax():
    df = _osc_df()
    sc_i, spring_i = 150, 250
    evs = [_ev("SC", sc_i), _ev("Spring", spring_i)]
    enrich(df, _pivots(df), evs)
    expect = round(min(1.0, (spring_i - sc_i) / 120.0), 4)
    assert evs[1]["feat"]["base_len_n"] == expect
    # 无前置高潮 → 0
    lone = [_ev("Spring", spring_i)]
    enrich(df, [], lone)
    assert lone[0]["feat"]["base_len_n"] == 0.0


def test_no_lookahead_beyond_local_window():
    """信号日之后远处的数据不得影响其语境特征。

    在 280 之后制造剧烈行情突变, 与截断到 278 的前缀重算对比:
    事件 (idx=250) 的全部语境特征必须一致。
    """
    df = _osc_df()
    evs_a = [_ev("Spring", 250)]
    enrich(df, _pivots(df), evs_a)

    df2 = df.copy()
    df2.loc[280:, "close"] *= 3
    df2.loc[280:, "high"] *= 3
    df2.loc[280:, "low"] *= 3
    df2.loc[280:, "volume"] *= 10
    evs_b = [_ev("Spring", 250)]
    enrich(df2, _pivots(df2), evs_b)

    for k in CONTEXT_FEAT_KEYS:
        assert evs_a[0]["feat"][k] == evs_b[0]["feat"][k], \
            f"{k} 受信号日后远处数据影响: {evs_a[0]['feat'][k]} vs {evs_b[0]['feat'][k]}"


def test_detect_all_integration_offline():
    """detect_all 管线离线冒烟: 产物事件带全语境键, 不因网络缺失报错。"""
    from wyckoff.events import detect_all
    df = _osc_df(n=200)
    out = detect_all(df, _pivots(df))
    for e in out:
        for k in CONTEXT_FEAT_KEYS:
            assert k in e.get("feat", {}), f"{e['type']} 缺 {k}"


def test_safe_fill_contract():
    """安全填充表覆盖所有可能为 None 的键, 值域合理。"""
    for k, v in SAFE_FILL.items():
        assert isinstance(v, (int, float)) and 0.0 <= v <= 1.0, k
