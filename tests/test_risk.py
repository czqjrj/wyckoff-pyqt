"""仓位管理 (wyckoff/risk.py) 回归测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.risk import calc_position_size, position_lines


def test_calc_position_size_basic():
    pos = calc_position_size(10.0, 9.5, target=12.0, portfolio_value=100000.0)
    assert pos["valid"]
    assert pos["risk_per_share"] == 0.5
    assert pos["rr_ratio"] == 4.0
    assert pos["meets_rr"]
    # 单笔风险预算 = 2% * 100000 = 2000元; 2000/0.5 = 4000股 → 整手 4000
    assert pos["shares"] == 4000
    assert pos["position_value"] == 40000.0
    assert pos["risk_amount"] == 2000.0
    assert abs(pos["risk_pct"] - 2.0) < 1e-6


def test_calc_position_rounds_to_lots():
    # 3000.4/... 非整手应向下取整到 100 股倍数
    pos = calc_position_size(10.0, 9.0, portfolio_value=50000.0)
    assert pos["shares"] % 100 == 0
    assert pos["shares"] > 0


def test_calc_position_invalid():
    pos = calc_position_size(10.0, 10.0, portfolio_value=100000.0)
    assert not pos["valid"]
    pos2 = calc_position_size(-1.0, 2.0)
    assert not pos2["valid"]


def test_calc_position_without_portfolio():
    pos = calc_position_size(10.0, 9.0)
    assert pos["valid"]
    assert "shares" not in pos


def test_position_lines_no_plan():
    lines = position_lines(None, 10.0)
    assert lines and "(无交易计划" in lines[0]


def test_position_lines_with_plan():
    plan = {"direction": "多头/低吸", "entry": 10.0, "stop": 9.5, "t1": 12.0}
    lines = position_lines(plan, 10.0, portfolio_value=100000.0)
    joined = "\n".join(lines)
    assert "盈亏比 4.0" in joined
    assert "建议仓位" in joined
