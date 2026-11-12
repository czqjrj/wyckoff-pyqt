"""九大检验 (wyckoff/ninetests.py) 回归测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.ninetests import nine_tests, nt_lines


def _mk_df(closes, volumes=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    opens = closes * 0.998
    highs = np.maximum(closes, opens) * 1.004
    lows = np.minimum(closes, opens) * 0.996
    vols = np.asarray(volumes, dtype=float) if volumes is not None else np.full(n, 1e6)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": opens, "close": closes, "high": highs, "low": lows,
        "volume": vols,
        "vol_ma20": pd.Series(vols).rolling(20, min_periods=1).mean().values,
    })


def _ev(etype, idx, price):
    return {"type": etype, "idx": idx, "date": None, "price": price, "desc": ""}


def test_nine_tests_buy_side():
    n = 160
    closes = 30 + np.sin(np.arange(n) / 6) * 0.5
    df = _mk_df(closes)
    events = [_ev("SC", 20, 28.5), _ev("AR", 35, 30.0), _ev("ST", 50, 28.8),
              _ev("Spring", 90, 28.2), _ev("SOS", 120, 30.5)]
    nt = nine_tests(df, events, pivots=[], phase="底部整固 (Accumulation)",
                    pnf_t={"下方目标": 28.0, "上方目标": 32.0})
    assert nt["side"] == "buy"
    assert len(nt["buy"]) == 9
    names = [t["name"] for t in nt["buy"]]
    assert names[0].startswith("T1")
    assert all(t.get("req") for t in nt["buy"])
    assert nt["buy_passed"] >= 2
    assert 0 <= nt["buy_passed"] <= 9


def test_nine_tests_sell_side():
    n = 160
    closes = 30 + np.sin(np.arange(n) / 6) * 0.5
    df = _mk_df(closes)
    events = [_ev("BC", 20, 31.0), _ev("AR", 35, 30.5), _ev("UTAD", 80, 31.5)]
    nt = nine_tests(df, events, pivots=[], phase="顶部构筑 (Distribution)",
                    tr={"top": 31.0, "bottom": 29.0})
    assert nt["side"] == "sell"
    assert len(nt["sell"]) == 9
    assert all(t.get("req") for t in nt["sell"])
    assert nt["sell_passed"] >= 1


def test_nine_tests_no_side():
    nt = nine_tests(_mk_df(np.linspace(10, 11, 100)), [], pivots=[], phase="区间整理")
    assert nt["side"] == ""
    assert len(nt["buy"]) == 9 and len(nt["sell"]) == 9


def test_nt_lines_render():
    nt = nine_tests(_mk_df(np.linspace(10, 11, 100)), [], pivots=[], phase="区间整理")
    lines = nt_lines(nt, phase="区间整理")
    assert any("九大" in ln for ln in lines)
    assert any("✓" in ln or "✗" in ln for ln in lines)
    reqs = [ln for ln in lines if "检验要求" in ln]
    assert len(reqs) == 9
    assert any("检验要求" in ln and "现价" in ln for ln in reqs)


def test_spring_expiry_after_confirm_window():
    """Spring 确认后第 9 根起跌破低点 → T7 必须失败 (旧实现永久为真)。"""
    n = 160
    closes = np.full(n, 30.0)
    closes[100:] = 27.0          # 第 100 根起收盘跌破 Spring 低点 28.2
    df = _mk_df(closes)
    events = [_ev("SC", 20, 28.0), _ev("AR", 35, 30.0),
              _ev("ST", 50, 29.0), _ev("Spring", 90, 28.2)]
    nt = nine_tests(df, events, pivots=[], phase="底部整固 (Accumulation)")
    t7 = next(t for t in nt["buy"] if t["name"].startswith("T7"))
    assert t7["passed"] is False, t7
    assert "失败" in t7["detail"] or "未" in t7["detail"]


def test_t9_requires_trading_range():
    """T9 不再因"行情够长"恒真: 无 TR 时必须失败。"""
    df = _mk_df(np.linspace(10, 11, 100))
    nt = nine_tests(df, [], pivots=[], phase="底部整固 (Accumulation)", tr=None)
    t9 = next(t for t in nt["buy"] if t["name"].startswith("T9"))
    assert t9["passed"] is False, t9


def test_t9_passes_with_enough_tests():
    df = _mk_df(np.linspace(10, 11, 100))
    tr = {"top": 11.0, "bottom": 10.0, "top_tests": 2, "bottom_tests": 1}
    nt = nine_tests(df, [], pivots=[], phase="底部整固 (Accumulation)", tr=tr)
    t9 = next(t for t in nt["buy"] if t["name"].startswith("T9"))
    assert t9["passed"] is True, t9


def test_invalid_utad_not_confirmed():
    """孤立 UTAD (无 BC 前置) 不应打 ✓ T7。"""
    n = 160
    closes = np.linspace(28, 30, n)
    df = _mk_df(closes)
    events = [_ev("UTAD", 80, 31.0)]
    nt = nine_tests(df, events, pivots=[], phase="顶部构筑 (Distribution)")
    t7 = next(t for t in nt["sell"] if t["name"].startswith("T7"))
    assert t7["passed"] is False, t7
    assert "证据不足" in t7["detail"]
