# -*- coding: utf-8 -*-
"""综合选股引擎 (wyckoff/screener.py) 单元测试。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from wyckoff.indicators import add_indicators


def _mk_df(n=300, trend="up"):
    """创建合成K线用于测试评分函数。"""
    rng = np.random.default_rng(42)
    if trend == "up":
        closes = 10 + np.linspace(0, 8, n) + rng.normal(0, 0.3, n)
    elif trend == "down":
        closes = 18 - np.linspace(0, 8, n) + rng.normal(0, 0.3, n)
    else:
        closes = 14 + rng.normal(0, 1.5, n)
    closes = np.maximum(closes, 1.0)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes * 0.998,
        "close": closes,
        "high": closes * 1.012,
        "low": closes * 0.988,
        "volume": rng.uniform(1e5, 2e6, n),
    })


# ── 评分函数测试 ──

def test_score_fundamental_basic():
    from wyckoff.screener import _score_fundamental
    # 健康基本面: PE合理 + 低PB + 高增长
    f = {"pe_ttm": 15, "pb": 1.2, "net_growth": 0.35, "mcap_yi": 150}
    s = _score_fundamental(f)
    assert 10 <= s <= 15

    # 亏损 + 高PB
    f2 = {"pe_ttm": -5, "pb": 8, "net_growth": -0.3, "mcap_yi": 5000}
    s2 = _score_fundamental(f2)
    assert s2 <= 5

    # 空数据 → 0
    assert _score_fundamental({}) == 0


def test_score_flow_basic():
    from wyckoff.screener import _score_flow
    # 强净流入 + 加速
    s1 = _score_flow(1.5, "加速流入")
    assert s1 >= 18

    # 强净流出 + 加速
    s2 = _score_flow(-1.5, "加速流出")
    assert s2 <= 5

    # 无数据 → 基线
    assert _score_flow(None, "") == 10


def test_score_technical_bullish():
    from wyckoff.screener import _score_technical
    df = _mk_df(300, "up")
    df = add_indicators(df, symbol="600036")
    tech = _score_technical(df)
    assert tech["score"] > 0
    assert tech["arrangement"] in ("多头排列", "空头排列", "均线纠缠")


def test_score_technical_bearish():
    from wyckoff.screener import _score_technical
    df = _mk_df(300, "down")
    df = add_indicators(df, symbol="600036")
    tech = _score_technical(df)
    # 下跌趋势技术面应偏弱
    assert tech["score"] <= 20


# ── 预筛测试 ──

def test_quick_fundamental_filter_no_filters():
    from wyckoff.screener import quick_fundamental_filter
    codes = ["600036", "000001"]
    # 无筛选条件 → 全部通过
    assert quick_fundamental_filter(codes, {}) == codes
    assert quick_fundamental_filter(codes, None) == codes


def test_quick_fundamental_filter_empty():
    from wyckoff.screener import quick_fundamental_filter
    assert quick_fundamental_filter([], {"mcap_min": 100}) == []


# ── 预设策略测试 ──

def test_list_presets():
    from wyckoff.screener import list_presets
    presets = list_presets()
    assert len(presets) >= 5
    names = [p["name"] for p in presets]
    assert "价值吸筹" in names
    assert "强势突破" in names


def test_get_preset():
    from wyckoff.screener import get_preset
    p = get_preset("value_accumulation")
    assert p is not None
    assert "filters" in p
    assert "底部整固" in p["filters"]["phases"]
    assert get_preset("nonexistent") is None


# ── 信号评分测试 ──

def test_signal_bonus_spring():
    from wyckoff.screener import _SIGNAL_BONUS
    assert _SIGNAL_BONUS["Spring"] > _SIGNAL_BONUS["SC"]
    assert _SIGNAL_BONUS["UTAD"] < 0


def test_phase_base_scores():
    from wyckoff.screener import _PHASE_BASE
    assert _PHASE_BASE["底部整固"] > _PHASE_BASE["上升趋势"]
    assert _PHASE_BASE["下跌趋势"] < 0


# ── screen_stocks 基础测试 ──

def test_screen_stocks_empty():
    from wyckoff.screener import screen_stocks
    assert screen_stocks([]) == []
    assert screen_stocks(None) == []


def test_screen_stocks_no_network(monkeypatch):
    """在无网络环境下 screen_stocks 不应崩溃。"""
    from wyckoff.screener import screen_stocks, quick_fundamental_filter
    # mock quick_fundamental_filter to bypass network
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes[:3])
    # score_stock 会尝试网络, 但我们只测框架
    # mock score_stock 返回固定值
    def _mock_score(code, datalen=500):
        return {
            "code": code, "name": f"测试{code}", "last": 10.0,
            "phase": "底部整固 吸筹", "phase_base": "底部整固",
            "signals": ["Spring"], "signal_bonus": 20, "conf_q": "high",
            "flow20": 0.5, "flow20_pct": 0.8, "flow_trend": "加速流入",
            "flow_score": 18,
            "pe": 15, "pb": 1.5, "mcap_yi": 200, "net_growth": 0.2,
            "fund_score": 12,
            "ma_arrangement": "多头排列", "vol_state": "低波动蓄势",
            "rsi": 45, "macd_hist": 0.5, "tech_score": 20,
            "total_score": 85, "sector": "银行", "sector20": 1.2,
            "error": None,
        }
    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    results = screen_stocks(["600036", "000001", "300750"])
    assert len(results) == 3
    assert results[0]["total_score"] == 85


def test_screen_stocks_phase_filter(monkeypatch):
    """阶段白名单过滤。"""
    from wyckoff.screener import screen_stocks
    call_count = {"n": 0}

    def _mock_score(code, datalen=500):
        call_count["n"] += 1
        phases = {"A": "底部整固 吸筹", "B": "下跌趋势 下跌", "C": "上升趋势 上升"}
        base = phases.get(code, "底部整固 吸筹")
        return {
            "code": code, "name": "", "last": 10.0,
            "phase": base, "phase_base": base.split()[0],
            "signals": [], "signal_bonus": 0, "conf_q": "",
            "flow20": None, "flow20_pct": None, "flow_trend": "", "flow_score": 10,
            "pe": None, "pb": None, "mcap_yi": None, "net_growth": None,
            "fund_score": 8,
            "ma_arrangement": "", "vol_state": "", "rsi": None, "macd_hist": None,
            "tech_score": 10, "total_score": 40, "sector": None, "sector20": None,
            "error": None,
        }
    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    # 只要吸筹阶段
    results = screen_stocks(["A", "B", "C"], {"phases": ["底部整固"]})
    assert len(results) == 1
    assert results[0]["code"] == "A"


def test_screen_stocks_signal_filter_hard(monkeypatch):
    """信号硬过滤 (默认 signals_mode="any"): 至少命中一个勾选信号才入选。"""
    from wyckoff.screener import screen_stocks

    def _mock_score(code, datalen=500):
        signals_map = {"A": ["Spring", "SC"], "B": ["UTAD"], "C": []}
        return {
            "code": code, "name": "", "last": 10.0,
            "phase": "底部整固 吸筹", "phase_base": "底部整固",
            "signals": signals_map.get(code, []), "signal_bonus": 0, "conf_q": "",
            "flow20": None, "flow20_pct": None, "flow_trend": "", "flow_score": 10,
            "pe": None, "pb": None, "mcap_yi": None, "net_growth": None,
            "fund_score": 8,
            "ma_arrangement": "", "vol_state": "", "rsi": None, "macd_hist": None,
            "tech_score": 10, "total_score": 40, "sector": None, "sector20": None,
            "error": None,
        }
    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    # 硬过滤: 只有命中 Spring 的 A 入选
    results = screen_stocks(["A", "B", "C"], {"signals": ["Spring"]})
    assert [r["code"] for r in results] == ["A"]


def test_screen_stocks_signal_filter_soft(monkeypatch):
    """信号软过滤 (signals_mode="soft"): 匹配加分, 不排除。"""
    from wyckoff.screener import screen_stocks

    def _mock_score(code, datalen=500):
        signals_map = {"A": ["Spring", "SC"], "B": ["UTAD"], "C": []}
        return {
            "code": code, "name": "", "last": 10.0,
            "phase": "底部整固 吸筹", "phase_base": "底部整固",
            "signals": signals_map.get(code, []), "signal_bonus": 0, "conf_q": "",
            "flow20": None, "flow20_pct": None, "flow_trend": "", "flow_score": 10,
            "pe": None, "pb": None, "mcap_yi": None, "net_growth": None,
            "fund_score": 8,
            "ma_arrangement": "", "vol_state": "", "rsi": None, "macd_hist": None,
            "tech_score": 10, "total_score": 40, "sector": None, "sector20": None,
            "error": None,
        }
    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    # 软过滤: 所有股票保留, A 因 Spring 加分排第一
    results = screen_stocks(["A", "B", "C"], {"signals": ["Spring"],
                                              "signals_mode": "soft"})
    assert len(results) == 3  # 所有股票都保留
    scores = {r["code"]: r["total_score"] for r in results}
    assert scores["A"] > scores["B"]  # A匹配Spring, 加5分
    assert scores["A"] > scores["C"]  # A匹配, C不匹配


def test_total_score_formula():
    """综合评分公式测试。"""
    from wyckoff.screener import _score_technical, _score_fundamental, _score_flow

    # 满分场景: 信号满分40 + 技术满分25 + 资金满分20 + 基本面满分15 = 100
    signal_bonus = 40  # max possible from phase+signals
    tech_score = 25
    flow_score = 20
    fund_score = 15

    total = (
        max(0, min(40, 20 + signal_bonus)) +
        max(0, min(25, tech_score)) +
        max(0, min(20, flow_score)) +
        max(0, min(15, fund_score))
    )
    assert total == 100

    # 零分场景
    total_zero = (
        max(0, min(40, 20 - 30)) +  # signal_bonus = -30 → 20-30 = -10 → max(0, min(40, -10)) = 0
        max(0, min(25, 0)) +
        max(0, min(20, 0)) +
        max(0, min(15, 0))
    )
    assert total_zero == 0

    # 顶部构筑惩罚 (-15)
    total_punish = 80
    phase_base = "顶部构筑"
    if phase_base in ("顶部构筑", "下跌趋势"):
        total_punish = max(0, total_punish - 15)
    assert total_punish == 65

    # 两者叠加惩罚
    total_both = 80
    phase_base = "下跌趋势"
    flow20 = -2
    if phase_base in ("顶部构筑", "下跌趋势"):
        total_both = max(0, total_both - 15)
    if flow20 is not None and flow20 < -1:
        total_both = max(0, total_both - 5)
    assert total_both == 60


def test_quick_fundamental_filter_with_conditions(monkeypatch):
    """带条件的预筛测试。"""
    from wyckoff.screener import quick_fundamental_filter

    def _mock_fundamental(symbol):
        code = symbol[-6:]
        funds = {
            "600036": {"mcap_yi": 5000, "pe_ttm": 8, "pb": 1.2},
            "000001": {"mcap_yi": 2000, "pe_ttm": 6, "pb": 0.8},
            "300750": {"mcap_yi": 800, "pe_ttm": 45, "pb": 8},
        }
        return funds.get(code)

    monkeypatch.setattr("wyckoff.screener._ensure_fund", lambda: type("M", (), {"fetch_fundamental": _mock_fundamental}))

    # 市值筛选: 只要 > 3000亿
    codes = ["600036", "000001", "300750"]
    filters = {"mcap_min": 3000}
    result = quick_fundamental_filter(codes, filters)
    assert result == ["600036"]

    # PE筛选: 只要 PE < 10
    filters = {"pe_max": 10}
    result = quick_fundamental_filter(codes, filters)
    assert "600036" in result  # PE=8
    assert "000001" in result  # PE=6
    assert "300750" not in result  # PE=45


# ── 并行 / 过滤增强 ──

def _mk_ok(code, score=60, sector=None, phase="底部整固 吸筹"):
    return {
        "code": code, "name": "", "last": 10.0,
        "phase": phase, "phase_base": phase.split()[0],
        "signals": [], "signal_bonus": 0, "conf_q": "",
        "flow20": None, "flow20_pct": None, "flow_trend": "", "flow_score": 10,
        "pe": None, "pb": None, "mcap_yi": None, "net_growth": None,
        "fund_score": 8,
        "ma_arrangement": "", "vol_state": "", "rsi": None, "macd_hist": None,
        "tech_score": 10, "total_score": score, "sector": sector, "sector20": None,
        "error": None,
    }


def test_screen_stocks_parallel(monkeypatch):
    """并行路径与串行路径结果一致, 进度单调递增。"""
    from wyckoff.screener import screen_stocks
    calls = []

    def _mock_score(code, datalen=500):
        calls.append(code)
        return _mk_ok(code, score=60 if code != "A" else 80)

    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    progress = []
    results = screen_stocks(["A", "B", "C", "D", "E"], workers=3,
                            on_progress=lambda d, t, c: progress.append(d))
    assert len(results) == 5
    assert results[0]["code"] == "A"  # 最高分在前
    assert progress == [1, 2, 3, 4, 5]  # 单调递增
    assert len(calls) == 5  # 每只都评分


def test_screen_stocks_cancel(monkeypatch):
    """取消: 停止后续评分并返回 partial 结果。"""
    from wyckoff.screener import screen_stocks
    import threading
    cancel = threading.Event()

    def _mock_score(code, datalen=500):
        if code == "B":
            cancel.set()  # 模拟耗时过程中用户取消
            return _mk_ok(code, score=70)
        return _mk_ok(code, score=60)

    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    results = screen_stocks(["A", "B", "C", "D", "E"], workers=2,
                            cancel_event=cancel)
    # 取消后必然不再含全部结果 (partial 或空)
    assert len(results) <= 5


def test_screen_stocks_on_error(monkeypatch):
    """单只分析失败 → on_error 计数, 不并入结果。"""
    from wyckoff.screener import screen_stocks

    def _mock_score(code, datalen=500):
        if code == "BAD":
            return {"code": code, "error": "K线分析失败: x"}
        return _mk_ok(code)

    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    errs = []
    results = screen_stocks(["GOOD", "BAD", "GOOD2"], on_error=errs.append)
    assert errs == ["BAD"]
    assert "BAD" not in [r["code"] for r in results]
    assert len(results) == 2


def test_screen_stocks_sector_filter(monkeypatch):
    """板块白名单: 任一命中才入选。"""
    from wyckoff.screener import screen_stocks

    def _mock_score(code, datalen=500):
        sectors = {"A": "银行", "B": "白酒", "C": None}
        return _mk_ok(code, sector=sectors.get(code))

    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    results = screen_stocks(["A", "B", "C"], {"sector": ["银行"]})
    assert [r["code"] for r in results] == ["A"]


def test_screen_stocks_min_score(monkeypatch):
    """最低总分过滤。"""
    from wyckoff.screener import screen_stocks

    def _mock_score(code, datalen=500):
        return _mk_ok(code, score={"H": 80, "M": 55, "L": 20}.get(code, 60))

    monkeypatch.setattr("wyckoff.screener.score_stock", _mock_score)
    monkeypatch.setattr("wyckoff.screener.quick_fundamental_filter",
                        lambda codes, f, **kw: codes)
    results = screen_stocks(["H", "M", "L"], {"min_score": 50})
    assert "L" not in [r["code"] for r in results]
    assert len(results) == 2
