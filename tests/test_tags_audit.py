"""威科夫全套标签补标回归测试 (范围B: TSO/BUEC/BOI/BUI/PSUP)。

覆盖:
1. TSO 终极震仓: detect_sow 深度假破位+快速收回 → TSO;
2. BOI/BUI 边界事件: market.boundary_events 按区间冰线产出破冰/回测;
3. PSUP 初次供应: detect_psup 在 BC 之前找顶端供给峰值;
4. 配置/解释闭环: EVENT_COLORS/EVENT_CN/EVENT_EXPLAIN 覆盖所有新标签,
   BU 保留兼容 + BUEC 别名收录。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import events as E
from wyckoff.config import EVENT_CN, EVENT_COLORS
from wyckoff.indicators import add_indicators
from wyckoff.market import boundary_events
from wyckoff.vsa_explain import EVENT_EXPLAIN

_NEW_TAGS = ("TSO", "BUEC", "BOI", "BUI", "PSUP")


def _mkdf(n=180):
    rng = np.random.default_rng(7)
    closes = 20 + np.cumsum(rng.normal(0, 0.08, n))
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.002, n)),
        "close": closes,
        "high": closes * 1.01, "low": closes * 0.99,
        "volume": np.full(n, 5e5),
    })


def _boi_df():
    """区间内震荡后放量收盘跌破冰线, 随后反弹回测冰线下方。"""
    n = 120
    closes = np.full(n, 12.0)
    closes[60:100] = np.linspace(12.0, 9.2, 40)
    # 破冰后先反抽触及冰线 (10.0) 再回落, 未收复 → BUI
    closes[100:112] = np.linspace(9.2, 10.2, 12)
    closes[112:] = np.linspace(10.2, 9.7, 8)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    vols = np.full(n, 5e5)
    vols[95] = 2e6
    df = pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": opens, "high": np.maximum(opens, closes) * 1.01,
        "low": np.minimum(opens, closes) * 0.99, "close": closes,
        "volume": vols,
    })
    df = add_indicators(df, symbol="600104")
    return df, {"top": 12.8, "bottom": 10.0, "top_tests": 2, "bottom_tests": 2}


def _breakdown_df(lo, i0=100, floor=22.0):
    """构造 floor=22 支撑下 i0 处放量破位、其后 8 根快速反弹回 floor 上方。

    lo 为破位低点: lo <= floor*0.94 触发 TSO; floor*0.94 < lo < floor*0.97
    触发 Shakeout。
    """
    df = _mkdf()
    df = add_indicators(df, symbol="600104")
    rebound = np.linspace(lo + 0.3, floor + 1.0, 8)
    df.loc[i0, "open"] = floor
    df.loc[i0, "high"] = floor + 0.2
    df.loc[i0, "close"] = floor - 0.3
    df.loc[i0, "low"] = lo
    df.loc[i0, "volume"] = df["vol_ma20"].iloc[i0] * 2.0
    df.loc[i0 + 1:i0 + 8, "close"] = rebound
    df.loc[i0 + 1:i0 + 8, "low"] = rebound - 0.1
    df.loc[i0 + 1:i0 + 8, "high"] = rebound + 0.1
    base = [{"type": "UTAD", "idx": i0 - 1, "price": floor,
             "date": df["day"].iloc[i0 - 1]}]
    pivots = [{"type": "low", "idx": i0, "price": lo,
               "date": df["day"].iloc[i0]}]
    return df, base, pivots


def test_tso_deep_fake_breakdown_labeled():
    """深度假破位+快速收回 → TSO (非 SOW / 非普通 Shakeout)。"""
    df, base, pivots = _breakdown_df(lo=20.0)   # 破位 ~9% (<= 22*0.94)
    ev = E.detect_sow(df, pivots, base)
    tso = [e for e in ev if e["type"] == "TSO"]
    assert tso, "深度假破位+快速收回应归为 TSO"
    assert not [e for e in ev if e["type"] == "SOW"], "快速收回不归 SOW"


def test_shakeout_not_tso_for_shallow_break():
    """浅破位快速收回仍是 Shakeout, 不升级 TSO。"""
    df, base, pivots = _breakdown_df(lo=21.0)   # 破位 ~4.5% (> 22*0.94)
    ev = E.detect_sow(df, pivots, base)
    assert not [e for e in ev if e["type"] == "TSO"], "浅破位不升级 TSO"
    assert [e for e in ev if e["type"] == "Shakeout"], "浅假破位仍是 Shakeout"


def test_boundary_events_boi_then_bui():
    """放量收盘跌破冰线 → BOI, 随后反弹回测冰线下方 → BUI。"""
    df, tr = _boi_df()
    ev = boundary_events(df, tr)
    types = [e["type"] for e in ev]
    assert "BOI" in types, "应检出破冰 BOI"
    assert "BUI" in types, "破冰后应有冰层回测 BUI"
    boi = next(e for e in ev if e["type"] == "BOI")
    bui = next(e for e in ev if e["type"] == "BUI")
    assert boi["idx"] < bui["idx"], "BOI 先于 BUI"
    assert boi["price"] < tr["bottom"], "BOI 收于冰线下方"
    assert bui["price"] >= tr["bottom"], "BUI 回测触及/站回冰线附近"


def test_boundary_events_none_without_tr():
    """无交易区间 → 无边界事件。"""
    df, _ = _boi_df()
    assert boundary_events(df, None) == []


def test_psup_emitted_before_bc():
    """BC 之前的走势顶端供给峰值 → PSUP 初次供应。"""
    df = _mkdf(140)
    df = add_indicators(df, symbol="600104")
    pivots = [
        {"type": "high", "idx": 90, "price": float(df["close"].iloc[90]),
         "date": df["day"].iloc[90]},
        {"type": "low", "idx": 95, "price": float(df["close"].iloc[95]),
         "date": df["day"].iloc[95]},
        {"type": "high", "idx": 100, "price": float(df["close"].iloc[100]),
         "date": df["day"].iloc[100]},
    ]
    ev = E.detect_psup(df, pivots, bc_idx=100)
    assert ev and ev[0]["type"] == "PSUP"
    assert ev[0]["idx"] < 100, "PSUP 应在 BC 之前"


def test_new_tags_config_explain_closed_loop():
    """新标签在 EVENT_COLORS/EVENT_CN/EVENT_EXPLAIN 中均有收录。"""
    for t in _NEW_TAGS:
        assert t in EVENT_COLORS, f"{t} 缺 EVENT_COLORS"
        assert t in EVENT_CN, f"{t} 缺 EVENT_CN"
        assert EVENT_EXPLAIN.get(t), f"{t} 缺 EVENT_EXPLAIN"


def test_bu_alias_buec_explained():
    """BU (回测小溪显示别名 BUEC) 两个键的解释均存在。"""
    assert EVENT_EXPLAIN.get("BU"), "BU 解释缺失"
    assert EVENT_EXPLAIN.get("BUEC"), "BUEC 解释缺失"
    assert EVENT_CN.get("BUEC") == "回测小溪"


# ── SC/BC 环境门 (docs/event_env_gate_progress.md 全量调查分桶) ──

def _climax_conf(typ, prior_r20, i=120):
    """在可控前置 20 根收益下求 SC/BC 的 conf (event_confidence)。"""
    rng = np.random.default_rng(7)
    closes = 20 + np.cumsum(rng.normal(0, 0.05, 200))
    closes[i] = closes[i - 20] * (1 + prior_r20)
    df = pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=200, freq="D"),
        "open": np.roll(closes, 1), "close": closes,
        "high": np.maximum(np.roll(closes, 1), closes) * 1.02,
        "low": np.minimum(np.roll(closes, 1), closes) * 0.98,
        "volume": np.full(200, 5e5),
    })
    df.loc[0, "open"] = closes[0]
    df = add_indicators(df, symbol="600104")
    ctx = E._EventContext(df)
    ev = E.event_confidence(ctx, [{"type": typ, "idx": i, "price": float(closes[i])}])[0]
    return ev["conf"], ev["feat"].get("prior_r20")


def test_sc_env_gate_prior_drop_boosts_conf():
    """SC 前置大跌 (≤-15%) 环境 conf 明显高于无大跌 (随机环境)。
    全量调查: 前置 ≤-15% → 20根上涨命中 70.4%; 前置 >-8% 命中仅 ~51%。"""
    deep_c, deep_p = _climax_conf("SC", -0.20)
    weak_c, weak_p = _climax_conf("SC", 0.02)
    assert deep_p == -0.20 and weak_p == 0.02, "prior_r20 特征应记录"
    assert deep_c > weak_c, f"深跌环境 SC 应更高置信: {deep_c} vs {weak_c}"


def test_sc_env_gate_middle_zone_neutral():
    """SC 前置小跌 (-15% ~ -8%) 介于深跌/随机之间, 应有固定增益差。"""
    strong_c, _ = _climax_conf("SC", -0.20)   # +8
    mid_c, _ = _climax_conf("SC", -0.10)       # 无调整
    weak_c, _ = _climax_conf("SC", 0.02)       # -15
    assert strong_c > mid_c > weak_c, "SC 环境门单调: 深跌>中跌>无跌"


def test_bc_env_gate_prior_rise_boosts_conf():
    """BC 前置大涨 (≥+15%) 环境 conf 明显高于横盘 (反向失效)。
    全量调查: 前置 ≥+15% → 20根下跌命中 60.5%; 横盘 ≤+4% 命中 ~47.8%。"""
    deep_c, deep_p = _climax_conf("BC", 0.20)
    weak_c, weak_p = _climax_conf("BC", -0.02)
    assert deep_p == 0.20 and weak_p == -0.02, "prior_r20 特征应记录"
    assert deep_c > weak_c, f"深涨环境 BC 应更高置信: {deep_c} vs {weak_c}"


# ── SOW 放量门 (sow_tighten_survey: vol_ratio≥2.2 命中85%, <1.6 仅66.5%) ──

def _sow_conf(vol_ratio):
    """在可控放量强度下求 SOW 的 conf (event_confidence)。"""
    rng = np.random.default_rng(11)
    closes = 20 + np.cumsum(rng.normal(0, 0.05, 200))
    df = pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=200, freq="D"),
        "open": np.roll(closes, 1), "close": closes,
        "high": np.maximum(np.roll(closes, 1), closes) * 1.02,
        "low": np.minimum(np.roll(closes, 1), closes) * 0.98,
        "volume": np.full(200, 5e5),
    })
    df.loc[0, "open"] = closes[0]
    i = 120
    df.loc[i, "volume"] = 5e5 * vol_ratio  # 事件当日放量
    df = add_indicators(df, symbol="600104")
    ctx = E._EventContext(df)
    ev = E.event_confidence(ctx, [{"type": "SOW", "idx": i, "price": float(closes[i])}])[0]
    return ev["conf"], ev["feat"].get("sow_vol_gate")


def test_sow_vol_gate_deep_volume_boosts_conf():
    """SOW 深层放量 (vol_ratio≥2.2) conf 明显高于平凡放量。
    全量调查: ≥2.2 → 20根下跌命中 85.0%; <1.6 → 66.5%。"""
    deep_c, deep_v = _sow_conf(2.6)
    weak_c, weak_v = _sow_conf(1.3)
    assert deep_v > weak_v > 0, "sow_vol_gate 特征应记录且随放量单调"
    assert deep_c > weak_c, f"深层放量 SOW 应更高置信: {deep_c} vs {weak_c}"


def test_sow_vol_gate_mid_volume_neutral():
    """SOW 中量 (1.6~2.2) 介于深层/平凡之间, 应有固定增益差。"""
    deep_c, _ = _sow_conf(2.6)   # +8
    mid_c, _ = _sow_conf(1.9)     # +2
    weak_c, _ = _sow_conf(1.3)    # -8
    assert deep_c > mid_c > weak_c, "SOW 放量门单调: 深层>中量>平凡"