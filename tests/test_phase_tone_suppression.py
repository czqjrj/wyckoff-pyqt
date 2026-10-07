"""阶段→方向 tone 的实测抑制回归。

背景 (docs/accuracy_improvements_todo.md 5.10): 全市场主板回填 2913 条已评估
记录测出 `下跌趋势` 的方向断言在 10/20/40 根上的净期望分别为
-3.42% / -4.68% / -3.90% (逐标的聚类稳健, 扣双边 0.15% 成本), 且调用点
pos=0.211 —— 在下一段 20 根区间的最低点看空, 20 根内 68.3% 上涨。属接刀。

因此 `下跌趋势` 阶段照常识别与展示, 但 tone 降为 neutral, 不再输出方向断言。
这些用例的作用是: 防止有人"顺手修复" tone 映射表时把它悄悄复活。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.phases import PHASE_TONE, PHASE_TONE_SUPPRESSED, phase_tone_of


def _mk_df(n=120):
    closes = np.linspace(20, 22, n) + np.sin(np.arange(n) / 5) * 0.2
    o = closes * 0.999
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": o, "close": closes, "high": np.maximum(closes, o) * 1.005,
        "low": np.minimum(closes, o) * 0.995, "volume": np.full(n, 1e6),
        "price_ma20": np.full(n, np.mean(closes)),
        "price_ma50": np.full(n, np.mean(closes)),
        "price_ma200": np.full(n, np.mean(closes)),
        "atr": np.full(n, 0.3),
    })
    df["direction"] = np.where(df["close"] >= df["open"], 1, -1)
    return df


def test_markdown_is_suppressed():
    assert "下跌趋势" in PHASE_TONE_SUPPRESSED
    assert phase_tone_of("下跌趋势") == "neutral"


def test_other_phase_tones_unchanged():
    # 抑制只针对已实测为负的阶段, 其余不得被误伤 (尤其 10 根净期望为正的
    # 顶部构筑 +1.66% 与 底部整固 +0.57%)。
    assert phase_tone_of("底部整固") == "bullish"
    assert phase_tone_of("上升趋势") == "bullish"
    assert phase_tone_of("顶部构筑") == "bearish"
    assert phase_tone_of("区间整理") == "neutral"
    assert phase_tone_of("待定") == "neutral"


def test_suppressed_phases_are_still_in_the_base_table():
    # 抑制是叠加在原表之上的, 不是把阶段从表里删掉 —— 阶段识别本身仍需要它。
    for p in PHASE_TONE_SUPPRESSED:
        assert p in PHASE_TONE


def test_both_call_sites_share_one_tone_table():
    """实盘路径 (conclusion) 与评估路径 (accuracy) 必须共用同一张表。

    修复前两处各有一份 phase→tone 字面量副本, 可各自漂移 —— 这条用例防止
    有人把其中一处改回内联字典, 导致实盘与回测口径不一致。
    """
    import wyckoff.accuracy as acc
    import wyckoff.conclusion as conclusion
    import wyckoff.phases as phases
    assert acc.phase_tone_of is phases.phase_tone_of
    assert conclusion.phase_tone_of is phases.phase_tone_of


def test_summary_card_tone_neutral_for_markdown(monkeypatch):
    """实盘路径: build_signal_summary 的"阶段"卡片必须给 neutral。

    阶段由 judge_phase 自行判定 (不吃 structure 入参), 故直接替换判阶结果,
    这样用例只覆盖"阶段名 → 卡片 tone"这一段接线。
    """
    import wyckoff.conclusion as conclusion
    monkeypatch.setattr(conclusion, "judge_phase",
                        lambda df, pivots, events: ("下跌趋势 (Markdown)", "d"))
    summary = conclusion.build_signal_summary(_mk_df(), [], [])
    card = next(s for s in summary if s["label"] == "阶段")
    assert "下跌趋势" in card["value"]
    assert card["tone"] == "neutral"


def test_summary_card_tone_bearish_for_distribution(monkeypatch):
    """对照组: 未被抑制的顶部构筑仍应输出 bearish。"""
    import wyckoff.conclusion as conclusion
    monkeypatch.setattr(conclusion, "judge_phase",
                        lambda df, pivots, events: ("顶部构筑 (Distribution)", "d"))
    summary = conclusion.build_signal_summary(_mk_df(), [], [])
    card = next(s for s in summary if s["label"] == "阶段")
    assert card["tone"] == "bearish"
