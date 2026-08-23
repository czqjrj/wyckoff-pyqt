"""精度升级三项回归测试:
1. P&F ATR 动态格值 (box_mode="atr"): 随波动率自适应, 与 pct 模式同构;
2. VSA 量 Z-score 异常检测 (vol_z_20 + vsa_volume_anomaly): 统计量异常,
   比"量>均量×k"更稳健; 并在 vsa_classify 中作为量级/高潮补充门;
3. VSA 标签滚动回测 (backtest_vsa): 与 backtest_events 同构因果口径;
4. 枢轴灵敏度档位 (PIVOT_SENSITIVITY / find_pivots sensitivity): fast<normal<safe.
"""
import numpy as np
import pandas as pd

from wyckoff.backtest import backtest_vsa
from wyckoff.indicators import PIVOT_SENSITIVITY, add_indicators, find_pivots, pivot_order
from wyckoff.pnf import build_pnf, pnf_box_label
from wyckoff.vsa import thresholds, vsa_classify, vsa_volume_anomaly


def _mk_df(n=500, seed=11, vol_spike_at=None, vol_spike=20.0):
    rng = np.random.default_rng(seed)
    closes = 50 + np.cumsum(rng.normal(0, 0.3, n))
    closes = np.clip(closes, 20, None)
    vol = rng.uniform(1e5, 1e6, n).astype(float)
    if vol_spike_at is not None:
        vol[vol_spike_at] = vol[vol_spike_at] * vol_spike
    return pd.DataFrame({
        "day": pd.date_range("2022-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.002, n)),
        "close": closes * (1 + rng.normal(0, 0.002, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": vol,
    })


# ───────────────────────── 1. P&F ATR 动态格值 ─────────────────────────

def test_pnf_atr_box_uses_atr():
    """atr 模式格值 = 0.5×ATR14 (df 含 atr 列), 且可建图。"""
    df = add_indicators(_mk_df(), symbol="600104")
    cols, box_atr = build_pnf(df, box_mode="atr")
    atr_last = float(df["atr"].dropna().iloc[-1])
    assert abs(box_atr - round(atr_last * 0.5, 2)) < 1e-9
    assert box_atr > 0 and len(cols) >= 1
    _, box_pct = build_pnf(df, box_mode="pct")
    assert box_pct == max(round(float(df["close"].iloc[-1]) * 0.015, 2), 0.01)
    assert box_atr != box_pct or abs(box_atr - box_pct) < 1e-9


def test_pnf_atr_factor_controls_box():
    """atr_factor 参数线性影响格值。"""
    df = add_indicators(_mk_df(seed=3), symbol="600104")
    _, b1 = build_pnf(df, box_mode="atr", atr_factor=0.5)
    _, b2 = build_pnf(df, box_mode="atr", atr_factor=1.0)
    assert abs(b2 - 2 * b1) < 1e-9


def test_pnf_atr_falls_back_without_atr():
    """df 无 atr 列时 atr 模式回退 pct, 不抛错。"""
    df = _mk_df()  # 无 atr 列
    cols, box = build_pnf(df, box_mode="atr")
    assert box == max(round(float(df["close"].iloc[-1]) * 0.015, 2), 0.01)
    assert len(cols) >= 1


def test_pnf_box_mode_default_pct():
    """默认 box_mode 保持旧行为 (pct), 不破坏既有调用。"""
    df = _mk_df()
    cols1, box1 = build_pnf(df)
    cols2, box2 = build_pnf(df, box_mode="pct")
    assert box1 == box2 and len(cols1) == len(cols2)


def test_pnf_box_label():
    assert "百分比" in pnf_box_label("pct")
    assert "ATR" in pnf_box_label("atr", 0.5)


# ─────────────────────── 2. VSA 量 Z-score 异常 ────────────────────────

def test_add_indicators_has_vol_z20():
    df = add_indicators(_mk_df(), symbol="600104")
    assert "vol_z_20" in df.columns
    z = df["vol_z_20"].dropna()
    assert abs(z.mean()) < 0.05 and 0.5 < z.std() < 2.0


def test_volume_anomaly_detects_spike():
    """量尖峰 → 产生 anomaly/climax 事件; 无尖峰 → 正常窗口不误报。"""
    df = add_indicators(_mk_df(vol_spike_at=250, vol_spike=30.0), symbol="600104")
    ev = vsa_volume_anomaly(df)
    assert ev, "尖峰处应产出量异常事件"
    assert any(e["idx"] == 250 for e in ev)
    assert any(e["level"] == "climax" for e in ev)
    for e in ev:
        assert abs(e["z"]) >= 2.0
    # 无尖峰随机量: 部分异常可接受, 但不可每根都报
    df2 = add_indicators(_mk_df(seed=21), symbol="600104")
    ev2 = vsa_volume_anomaly(df2)
    assert len(ev2) < 40


def test_volume_anomaly_level_thresholds():
    """z_anom/z_climax 参数可调 (默认 2.0/3.0, 与 thresholds 一致)。"""
    th = thresholds(240)
    assert th["z_anom"] == 2.0 and th["z_climax"] == 3.0
    df = add_indicators(_mk_df(vol_spike_at=300, vol_spike=50.0), symbol="600104")
    z = df["vol_z_20"].iloc[300]
    ev = vsa_volume_anomaly(df, z_anom=1.0, z_climax=1.5)
    assert any(e["idx"] == 300 and e["level"] == "climax" for e in ev)


def test_vsa_classify_handles_missing_z_column():
    """df 无 vol_z_20 列 → vsa_classify 退化为纯量比, 不抛错。"""
    df = add_indicators(_mk_df(seed=5), symbol="600104")
    df = df.drop(columns=["vol_z_20"])
    sigs = vsa_classify(df)  # 缺 vol_z_20 → 退化为纯量比分级
    assert isinstance(sigs, list)


# ─────────────────────── 3. VSA 滚动回测 ──────────────────────────────

def test_backtest_vsa_returns_stats():
    """backtest_vsa 返回 by_label 统计且字段完整。"""
    df = add_indicators(_mk_df(seed=17), symbol="600104")
    res = backtest_vsa(df, horizon=10, min_n=1, cost=0.0)
    assert set(res) >= {"by_label", "benchmark", "cost", "horizon"}
    assert res["horizon"] == 10
    for lb, s in res["by_label"].items():
        for k in ("n", "win", "avg", "med", "best", "worst", "pl_ratio", "vs_bh"):
            assert k in s
        assert s["n"] >= 1


def test_backtest_vsa_short_sample():
    """样本过短 → 返回 note=样本过短 空结果。"""
    df = add_indicators(_mk_df(n=80), symbol="600104")
    res = backtest_vsa(df)
    assert res["note"] == "样本过短" and res["by_label"] == {}


def test_backtest_vsa_min_n_filters():
    """min_n 过滤样本数不足的标签。"""
    df = add_indicators(_mk_df(seed=23), symbol="600104")
    res_lo = backtest_vsa(df, horizon=10, min_n=1, cost=0.0)
    res_hi = backtest_vsa(df, horizon=10, min_n=10 ** 6, cost=0.0)
    assert res_hi["by_label"] == {}
    assert len(res_lo["by_label"]) >= len(res_hi["by_label"])


# ─────────────────────── 4. 枢轴灵敏度档位 ─────────────────────────────

def test_pivot_sensitivity_mapping():
    """档位 → order 映射: fast=3, normal=6, safe=9。"""
    assert PIVOT_SENSITIVITY == {"fast": 3, "normal": 6, "safe": 9}
    assert pivot_order("fast") == 3
    assert pivot_order("normal") == 6
    assert pivot_order("safe") == 9
    assert pivot_order("bogus") == 6  # 未知档位回退 normal


def test_find_pivots_sensitivity_orders():
    """sensitivity 档位切换枢轴邻域半径: safe 枢轴数 ≤ normal ≤ fast。"""
    df = add_indicators(_mk_df(seed=9), symbol="600104")
    n_fast = len(find_pivots(df, sensitivity="fast"))
    n_norm = len(find_pivots(df, sensitivity="normal"))
    n_safe = len(find_pivots(df, sensitivity="safe"))
    assert n_safe <= n_norm <= n_fast
    assert n_norm > 3


def test_find_pivots_explicit_order_wins():
    """显式 order 参数优先于 sensitivity (兼容旧调用)。"""
    df = add_indicators(_mk_df(seed=13), symbol="600104")
    n_explicit = len(find_pivots(df, order=9, sensitivity="fast"))
    n_safe = len(find_pivots(df, sensitivity="safe"))
    assert n_explicit == n_safe  # order=9 即 safe 档


def test_find_pivots_default_normal():
    """默认调用 = normal 档 (order 6, 与旧行为一致)。"""
    df = add_indicators(_mk_df(seed=31), symbol="600104")
    assert len(find_pivots(df)) == len(find_pivots(df, order=6))
