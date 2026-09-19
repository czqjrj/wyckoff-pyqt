"""PnF 三档目标到达概率 · 校准回归测试。

覆盖 _pnf_targets_at 的概率产出契约与 _low_prob_discount 低概率端收缩折扣:
  - 三档概率必须落 [0.15, 0.95] 且保持 保守 ≥ 中 ≥ 激进;
  - 折扣系数单调连续、档位排序稳定、≥_DISCOUNT_HI 不收缩、_DISCOUNT_LO 处取满档。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.pnf import (
    _DISCOUNT_HI,
    _DISCOUNT_LO,
    _LOW_DISCOUNT,
    _enforce_tier_order,
    _low_prob_discount,
    build_pnf,
    pnf_targets,
)

TIERS = ("保守", "中", "激进")


def _df_with_trends():
    np.random.seed(7)
    close = []
    p = 80.0
    for _nbar, _base, drift in [(300, 100, 0.15), (120, 135, 0.1),
                                (200, 100, -0.15), (180, 85, 0.1)]:
        for _ in range(_nbar):
            p += drift + np.random.randn() * 1.2
            close.append(p)
    close = np.array(close)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=len(close)),
        "open": close * 0.999, "close": close,
        "high": close * 1.008, "low": close * 0.992,
        "volume": np.random.rand(len(close)) * 1e6,
    })


def test_tier_probs_bounded_and_ordered():
    """三档概率在 [0.15, 0.95] 且每方向单调 保守 ≥ 中 ≥ 激进。"""
    df = _df_with_trends()
    cols, box = build_pnf(df)
    cur = pnf_targets(df, cols, box)
    assert cur, "应产出目标测算"
    for direction in ("上方", "下方"):
        vals = [cur.get(f"{direction}概率_{t}") for t in TIERS]
        assert all(isinstance(v, (int, float)) and 0.15 <= v <= 0.95 for v in vals), \
            f"{direction} 概率越界: {vals}"
        assert vals[0] >= vals[1] >= vals[2], \
            f"{direction} 档位顺序被破坏: {vals}"


def test_discount_full_factor_below_threshold():
    """p<_DISCOUNT_LO 施加全档折扣; p=_DISCOUNT_LO 恰取 _LOW_DISCOUNT。"""
    for tier in TIERS:
        assert _low_prob_discount(0.30, tier) == _LOW_DISCOUNT[tier]
        assert _low_prob_discount(_DISCOUNT_LO - 0.01, tier) == _LOW_DISCOUNT[tier]
        assert _low_prob_discount(_DISCOUNT_LO, tier) == _LOW_DISCOUNT[tier]


def test_discount_ramps_to_one_at_hi():
    """_DISCOUNT_LO→_DISCOUNT_HI 线性过渡回 1.0, ≥_DISCOUNT_HI 不收缩 (连续无台阶)。"""
    for tier in TIERS:
        assert _low_prob_discount(_DISCOUNT_LO + (_DISCOUNT_HI - _DISCOUNT_LO) / 2,
                                  tier) > _LOW_DISCOUNT[tier]
        assert _low_prob_discount(_DISCOUNT_HI - 0.01, tier) < 1.0
        assert _low_prob_discount(_DISCOUNT_HI, tier) == 1.0
        assert _low_prob_discount(_DISCOUNT_HI + 0.1, tier) == 1.0
    # 过渡段单调递增
    lo = _low_prob_discount(_DISCOUNT_LO, "保守")
    for p in np.linspace(_DISCOUNT_LO + 0.01, _DISCOUNT_HI - 0.01, 9):
        assert _low_prob_discount(p, "保守") > lo


def test_discount_tier_order_stable():
    """任意同概率下 保守 惩罚最重, 激进 最轻。"""
    for p in (0.10, 0.30, 0.50, 0.55, 0.60, 0.65):
        f = [_low_prob_discount(p, t) for t in TIERS]
        assert f[0] <= f[1] <= f[2], f"(p={p}) 档位折扣排序被破坏: {f}"


def test_discount_calibration_snapshot():
    """校准快照 (2026-09-19 重标定, 608 段): 系数/过渡窗必须与归档一致。

    防回归: 任何调整都需先跑 scripts/eval_pnf_tier_accuracy.py 重新量化。
    """
    assert _LOW_DISCOUNT == {"保守": 0.84, "中": 0.94, "激进": 0.96}
    assert _DISCOUNT_LO == 0.55
    assert _DISCOUNT_HI == 0.72
    # 全档折扣关键点抽查 (p=0.50 < LO → 满档; p=0.70 过渡中; p=0.72 → 1.0)
    for tier in TIERS:
        assert _low_prob_discount(0.50, tier) == _LOW_DISCOUNT[tier]
    assert _low_prob_discount(0.72, "保守") == 1.0


def test_enforce_tier_order_lowers_only():
    """顺序守卫只降不升: 越界档降到上一档, 未越界行不动。"""
    # 倒序行 (折扣翻转) → 收敛到最保守档值
    t_inv = {"上方概率_保守": 0.30, "上方概率_中": 0.35, "上方概率_激进": 0.38}
    t_out = _enforce_tier_order(dict(t_inv))
    assert t_out["上方概率_保守"] == 0.30
    assert t_out["上方概率_中"] == 0.30
    assert t_out["上方概率_激进"] == 0.30
    # 正常行不受影响
    t_ok = {"下方概率_保守": 0.80, "下方概率_中": 0.62, "下方概率_激进": 0.55}
    t_out2 = _enforce_tier_order(dict(t_ok))
    assert t_out2 == t_ok
    # 缺档/非数值 → 原样返回不崩溃
    t_missing = {"上方概率_保守": 0.50}
    assert _enforce_tier_order(t_missing) == t_missing
