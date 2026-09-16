"""涨跌停成交约束 fail-closed 行为测试。

回归背景: market_rules.limit_blocked 曾在行情数据缺失/解析异常时返回 False
(fail-open = 视为可成交), 数据断档时引擎会以为涨停还能买进、跌停还能卖出,
实盘即为真实资金风险。收紧后: 无法确认封板状态一律按封板处理 (True),
仅"带指标列且确认未封板"放行 (False); 合成/裸 K 线 (无 limit_up/limit_dn)
仍按未封板, 由调用方保证真实撮合路径先 attach 指标。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from wyckoff import market_rules as mr  # noqa: E402


def _df_with_flags(limit_up=False, limit_dn=False, rows=3):
    df = pd.DataFrame({
        "open": [10.0] * rows,
        "high": [11.0] * rows,
        "low": [9.5] * rows,
        "close": [10.5] * rows,
    })
    df["limit_up"] = [False] * (rows - 1) + [limit_up]
    df["limit_dn"] = [False] * (rows - 1) + [limit_dn]
    return df


@pytest.mark.parametrize("df", [None, pd.DataFrame()])
def test_limit_blocked_fail_closed_on_missing_data(df):
    assert mr.limit_blocked(df, "buy") is True, "行情缺失应按封板拦截买入"
    assert mr.limit_blocked(df, "sell") is True, "行情缺失应按封板拦截卖出"


def test_limit_blocked_fail_closed_on_parse_error(monkeypatch):
    def boom(df, symbol=None):
        raise RuntimeError("indicator 崩了")
    monkeypatch.setattr(mr, "bar_limit_flags", boom)
    df = _df_with_flags()
    assert mr.limit_blocked(df, "buy") is True, "解析异常应按封板拦截"


def test_limit_blocked_latest_bar_decision():
    # 最新 bar 涨停封板 → 买不进; 未封板 → 放行
    assert mr.limit_blocked(_df_with_flags(limit_up=True), "buy") is True
    assert mr.limit_blocked(_df_with_flags(limit_up=False), "buy") is False
    # 最新 bar 跌停封板 → 卖不出; 未封板 → 放行
    assert mr.limit_blocked(_df_with_flags(limit_dn=True), "sell") is True
    assert mr.limit_blocked(_df_with_flags(limit_dn=False), "sell") is False


def test_limit_blocked_bare_ohlc_not_blocked():
    # 合成/裸 K 线 (无指标列): 保持不误拦 (调用方负责在真实撮合前 attach 指标)
    bare = pd.DataFrame({"open": [1], "high": [1.1], "low": [0.9], "close": [1.0]})
    assert mr.limit_blocked(bare, "buy") is False
    assert mr.limit_blocked(bare, "sell") is False


def test_bar_limit_flags_empty():
    up, dn = mr.bar_limit_flags(pd.DataFrame(), "600000")
    assert len(up) == 0 and len(dn) == 0


def test_paper_facade_fail_closed_when_fetch_breaks(monkeypatch):
    """paper._limit_blocked 在行情回贴失败时按封板处理 (不再假成交)。"""
    import wyckoff.paper as paper

    def boom(code, datalen=420):
        raise RuntimeError("行情接口挂了")
    monkeypatch.setattr(paper, "_next_open", boom)
    paper._CUR["limit_fill"] = True
    try:
        assert paper._limit_blocked("600000", "buy") is True
        assert paper._limit_blocked("600000", "sell") is True
    finally:
        paper._CUR["limit_fill"] = False


def test_paper_facade_disabled_when_limit_fill_off(monkeypatch):
    import wyckoff.paper as paper

    def boom(code, datalen=420):
        raise RuntimeError("行情接口挂了")
    monkeypatch.setattr(paper, "_next_open", boom)
    paper._CUR["limit_fill"] = False
    try:
        assert paper._limit_blocked("600000", "buy") is False
    finally:
        paper._CUR["limit_fill"] = False
