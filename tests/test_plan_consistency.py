"""交易计划方向/现价/止损/目标自洽性回归测试。

背景: 用户实测复现"方向=多头/低吸, 现价10.27, 止损却给10.34"的自相矛盾计划。
根因: 现价已跌破 Spring/SC 低点 (结构买点失效) 时, 结构止损自然落在现价上方,
原代码未做自洽性校验直接输出。本测试固化修复行为。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.analysis import build_trade_plan
from wyckoff.risk import position_lines
from wyckoff.waves import elliott_wave


def _df(closes, atr=0.12):
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({"close": closes, "high": closes + 0.08,
                       "low": closes - 0.08, "volume": 1e6})
    df["atr"] = atr
    return df


def _trend(closes_len=150, end_level=10.60, seed=42):
    np.random.seed(seed)
    base = np.random.normal(0, 0.05, closes_len).cumsum() + 10.0
    base[-10:] = np.linspace(base[-11] - 0.05, end_level, 10)
    return base


def test_long_stop_above_price_downgrades_to_watch():
    """用户实测复现用例: Spring低点10.44, 现价10.27 (已破位)。
    原输出'多头/低吸 + 止损10.34'自相矛盾, 修复后应降级为观望。"""
    closes = _trend(end_level=10.27)
    df = _df(closes)
    events = [{"type": "Spring", "price": 10.44, "idx": 100},
              {"type": "ST", "price": 10.55, "idx": 105}]
    lines = build_trade_plan(df, [], events, "底部整固 (Accumulation)",
                             structure="", targets={}, pnf_t=None, tr=None,
                             last_close=10.27)
    joined = "\n".join(lines)
    assert "方向: 观望" in joined
    assert "止损:" not in joined
    assert "已被跌破" in joined
    assert "多头/低吸" not in joined


def test_short_stop_below_price_downgrades_to_watch():
    """对称场景: 派发高点已被现价突破时, '空头/减仓'止损低于现价 → 观望。"""
    closes = _trend(end_level=10.60)
    df = _df(closes)
    events = [{"type": "UTAD", "price": 10.44, "idx": 100},
              {"type": "BC", "price": 10.40, "idx": 105}]
    lines = build_trade_plan(df, [], events, "顶部构筑 (Distribution)",
                             structure="", targets={}, pnf_t=None, tr=None,
                             last_close=10.60)
    joined = "\n".join(lines)
    assert "方向: 观望" in joined
    assert "止损:" not in joined
    assert "已被突破" in joined


def test_valid_long_preserved():
    """有效多头: Spring低点10.40, 现价10.60 → 维持多头/低吸, 止损<现价<目标。"""
    closes = _trend(end_level=10.60)
    df = _df(closes)
    events = [{"type": "Spring", "price": 10.40, "idx": 100}]
    lines = build_trade_plan(df, [], events, "底部整固 (Accumulation)",
                             structure="", targets={"近端上涨目标(上方)": 11.20},
                             pnf_t=None, tr=None, last_close=10.60)
    joined = "\n".join(lines)
    assert "方向: 多头/低吸" in joined
    assert "止损: 10.30" in joined
    assert "目标1: 11.20" in joined


def test_valid_short_preserved():
    """有效空头/减仓: 现价9.80 < 上空位9.90, 保留减仓指引。

    A股只能做多、不能做空: 空头/减仓只输出 上空确认位/下方回踩支撑 这类
    离场指引, 不再输出 止损/目标/盈亏比 的可执行做空交易规格 (避免被解读成
    '现价距止损近而盈亏比不足'的矛盾交易)。
    """
    closes = _trend(end_level=9.80)
    df = _df(closes)
    events = [{"type": "UTAD", "price": 9.90, "idx": 100},
              {"type": "BC", "price": 9.85, "idx": 105}]
    lines = build_trade_plan(df, [], events, "顶部构筑 (Distribution)",
                             structure="", targets={}, pnf_t=None, tr=None,
                             last_close=9.80)
    joined = "\n".join(lines)
    assert "方向: 空头/减仓" in joined
    assert "上空确认位: 9.90" in joined
    assert "不能做空" in joined
    assert "已突破" not in joined
    for bad in ("止损:", "目标1:", "目标2:", "盈亏比:", "ATR止损:", "仓位参考:"):
        assert bad not in joined


def test_short_plan_downside_support_not_trade_specs():
    """空头/减仓即便有下方目标, 也只作支撑参考, 绝无止损/目标/盈亏比交易规格。"""
    closes = _trend(end_level=10.06)
    df = _df(closes)
    events = [{"type": "UTAD", "price": 10.49, "idx": 100},
              {"type": "BC", "price": 10.45, "idx": 105}]
    lines = build_trade_plan(df, [], events, "顶部构筑 (Distribution)",
                             structure="", targets={"下跌目标(下方)": 9.75},
                             pnf_t=None, tr=None, last_close=10.06)
    joined = "\n".join(lines)
    assert "方向: 空头/减仓" in joined
    assert "下方回踩支撑参考: 9.75" in joined
    for bad in ("止损:", "目标1:", "目标2:", "盈亏比:", "ATR止损:", "仓位参考:"):
        assert bad not in joined


def test_position_lines_rejects_contradictory_plan():
    """防御层: 即便上游漏出矛盾计划, 仓位计算也要拒绝而非输出误导数字。"""
    plan = {"direction": "多头/低吸", "entry": 10.27, "stop": 10.34, "t1": 11.49}
    lines = position_lines(plan, 10.27, portfolio_value=100000.0)
    joined = "\n".join(lines)
    assert "自相矛盾" in joined
    assert "建议仓位" not in joined


def test_elliott_wave_up_stop_already_breached():
    """波浪侧: 上升波中现价已跌破0.618回撤 → 输出失效提示, 不再给'严格止损'。"""
    df = _df([10.0, 10.4, 10.9, 10.5])  # 末收10.5 < 0.618回撤10.62
    pivots = [{"type": "low", "price": 10.0, "idx": 0},
              {"type": "high", "price": 11.0, "idx": 1}]
    joined = "\n".join(elliott_wave(df, pivots))
    assert "已跌破0.618" in joined
    assert "严格止损" not in joined


def test_elliott_wave_up_valid_stop():
    """波浪侧: 现价位于0.618回撤上方 → 正常输出严格止损与扩展目标。"""
    df = _df([10.0, 10.4, 10.9, 10.9])
    pivots = [{"type": "low", "price": 10.0, "idx": 0},
              {"type": "high", "price": 11.0, "idx": 1}]
    joined = "\n".join(elliott_wave(df, pivots))
    assert "严格止损" in joined
    assert "1.618扩展位" not in joined


def test_lps_qualifies_as_buy_point():
    """LPS (Phase D 标准买点) 应作为多头/低吸确认 (修复 detect_joc_lps_bu 后)。"""
    closes = _trend(end_level=10.60)
    df = _df(closes)
    events = [{"type": "SC", "price": 9.9, "idx": 90},
              {"type": "Spring", "price": 10.20, "idx": 100},
              {"type": "SOS", "price": 10.80, "idx": 108},
              {"type": "LPS", "price": 10.42, "idx": 112}]
    lines = build_trade_plan(df, [], events, "底部整固 (Accumulation)",
                             structure="", targets={}, pnf_t=None, tr=None,
                             last_close=10.60)
    joined = "\n".join(lines)
    assert "方向: 多头/低吸" in joined
    assert "止损:" in joined


def test_isolated_lps_without_spring_still_buy():
    """仅有 LPS 而无 Spring/ST 时也构成买点 (LPS 自身即吸筹低吸信号)。"""
    closes = _trend(end_level=10.60)
    df = _df(closes)
    events = [{"type": "LPS", "price": 10.42, "idx": 112}]
    lines = build_trade_plan(df, [], events, "底部整固 (Accumulation)",
                             structure="", targets={}, pnf_t=None, tr=None,
                             last_close=10.60)
    joined = "\n".join(lines)
    assert "多头/低吸" in joined
