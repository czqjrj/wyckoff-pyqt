"""增强波浪分析 (waves.enhanced_wave_analysis) 集成测试。

覆盖: 完整5浪/ABC 输出行、斐波那契汇聚、交叉验证、机会/风险、图标注数据。
"""

import pandas as pd

from wyckoff.waves import enhanced_wave_analysis


def _df_from_pivots(pivots, n=60):
    """构造覆盖所有枢轴的合成 DataFrame。"""
    closes = []
    for p in sorted(pivots, key=lambda x: x["idx"]):
        while len(closes) <= p["idx"]:
            closes.append(closes[-1] if closes else p["price"])
        closes[p["idx"]] = p["price"]
    while len(closes) < n:
        closes.append(closes[-1])
    return pd.DataFrame({
        "day": [f"d{i}" for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1e6] * n,
    })


def _pivots(seq):
    return [{"type": t, "price": p, "idx": i}
            for i, (t, p, i) in enumerate(seq)]


# ── 完整5浪上升推动 ──
UP_IMPULSE = [("low", 10.0, 0), ("high", 15.0, 1), ("low", 11.0, 2),
              ("high", 20.0, 3), ("low", 12.0, 4)]


def test_enhanced_impulse_basic():
    """完整5浪上升 → 输出推动浪/当前浪位。"""
    pivots = _pivots(UP_IMPULSE)
    df = _df_from_pivots(pivots)
    lines, wd = enhanced_wave_analysis(df, pivots, phase="上涨阶段")
    joined = "\n".join(lines)
    assert "推动浪" in joined and "上升" in joined
    assert "当前上升浪位" in joined
    assert "斐波那契汇聚" in joined
    assert "机会" in joined and "风险" in joined


def test_enhanced_impulse_wave_data():
    """wave_data 含图标注点、失效位、扩展目标。"""
    pivots = _pivots(UP_IMPULSE)
    df = _df_from_pivots(pivots)
    _lines, wd = enhanced_wave_analysis(df, pivots)
    assert wd["points"] and all(len(pt) == 3 for pt in wd["points"])
    assert wd["invalidation"] is not None
    assert wd["next_target"] is not None
    assert wd["fib"]


def test_enhanced_cross_with_accumulation_events():
    """5浪下跌 + Spring/SC 事件 → 筑底概率上升。"""
    seq = [("high", 20.0, 0), ("low", 16.0, 1), ("high", 18.0, 2),
           ("low", 12.0, 3), ("high", 14.0, 4), ("low", 9.0, 5)]
    pivots = _pivots(seq)
    df = _df_from_pivots(pivots)
    events = [{"type": "Spring", "idx": 5, "price": 9.0}]
    lines, _wd = enhanced_wave_analysis(df, pivots, phase="吸筹阶段", events=events)
    joined = "\n".join(lines)
    assert "5浪下跌完成" in joined
    assert "筑底概率上升" in joined


def test_enhanced_corrective_abc():
    """向下 ABC 回调识别 + 低吸机会。"""
    seq = [("low", 10.0, 0), ("high", 20.0, 1), ("low", 16.0, 2),
           ("high", 18.0, 3), ("low", 15.0, 4)]
    pivots = _pivots(seq)
    df = _df_from_pivots(pivots)
    events = [{"type": "SC", "idx": 0, "price": 10.0}]
    lines, wd = enhanced_wave_analysis(df, pivots, phase="吸筹阶段", events=events)
    joined = "\n".join(lines)
    assert "修正浪" in joined or "ABC" in joined
    assert "回调" in joined
    assert "低吸机会" in joined
    assert wd["direction"] == "down"


def test_enhanced_corrective_abc_up_rebound():
    """向上 ABC 反弹识别 + 减仓建议。"""
    seq = [("low", 20.0, 0), ("high", 30.0, 1), ("low", 22.0, 2),
           ("high", 28.0, 3)]
    pivots = _pivots(seq)
    df = _df_from_pivots(pivots)
    lines, wd = enhanced_wave_analysis(df, pivots, phase="下跌趋势")
    joined = "\n".join(lines)
    assert "反弹中" in joined
    assert "减仓" in joined or "高抛" in joined
    assert wd["direction"] == "up"


def test_enhanced_down_branch_no_short_specs():
    """A股只能做多: 下跌波段的操作参考不得出现 做空/空头止损/空头目标 交易规格,
    只给 减仓/离场 指引与 上方确认位/下方回踩支撑参考。"""
    import numpy as np

    from wyckoff.waves import elliott_wave
    df = pd.DataFrame({"close": [20.0, 19.0, 18.0, 17.0],
                       "high": [20.5, 19.5, 18.5, 17.5],
                       "low": [19.5, 18.5, 17.5, 16.5],
                       "volume": np.full(4, 1e6)})
    pivots = [{"type": "high", "price": 20.0, "idx": 0},
              {"type": "low", "price": 17.0, "idx": 2}]
    lines = elliott_wave(df, pivots)
    joined = "\n".join(lines)
    assert "下跌波段" in joined
    assert "上方确认位:" in joined
    assert "下方回踩支撑参考:" in joined
    assert "减仓" in joined or "离场" in joined
    assert "不能做空" in joined
    for bad in ("空头止损", "空头目标", "不宜追空"):
        assert bad not in joined


def test_enhanced_none_structure():
    """无结构 → 保守提示。"""
    pivots = _pivots([("low", 10.0, 0), ("high", 15.0, 1)])
    df = _df_from_pivots(pivots)
    lines, wd = enhanced_wave_analysis(df, pivots)
    joined = "\n".join(lines)
    assert "结构不足以计数" in joined or "波浪" in joined
    assert wd["points"] == []
