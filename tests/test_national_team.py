"""国家队资金流向统一判定模块测试: 三通道聚合评分、通道剔除、摘要格式。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.national_team import (
    INFLOW_T,
    OUTFLOW_T,
    aggregate_nt,
    fmt_nt_flow,
)


def _etf(verdict, y1, y20, source="flow"):
    return {"verdict": verdict, "y1": y1,
            "y5": y1 * 5.0 if y1 is not None else None,
            "y20": y20,
            "source": source, "code": "000000", "name": "ETF"}


def _etfs_bull(n=11):
    return [_etf("疑似买入", 3.0, 80.0) for _ in range(n)]


def _etfs_bear(n=11):
    return [_etf("疑似减仓", -3.0, -80.0) for _ in range(n)]


def _factor(buy, sell, direction):
    return {"buy_prob": buy, "sell_prob": sell, "direction": direction}


def _factors_bull():
    return [_factor(0.9, 0.1, "buy") for _ in range(8)] + \
           [_factor(0.4, 0.45, "sell") for _ in range(3)]


def _factors_bear():
    return [_factor(0.1, 0.9, "sell") for _ in range(8)] + \
           [_factor(0.45, 0.4, "buy") for _ in range(3)]


def _hold(statuses):
    return {"holders": [{"status": s} for s in statuses], "exited": []}


def _holds_bull():
    return [_hold(["加仓", "新进", "维持"]), _hold(["加仓"])]


def _holds_bear():
    return [_hold(["减仓", "退出"]), _hold(["减仓"]),
            {"holders": [], "exited": [{"name": "x"}]}]


def test_inflow_verdict():
    res = aggregate_nt(_etfs_bull(), _factors_bull(), _holds_bull())
    assert res["verdict"] == "净流入"
    assert res["bias"] == "流入"
    assert res["score"] >= INFLOW_T
    assert all(v == "ok" for v in res["channel_states"].values())
    assert res["etf"]["net_y1"] > 0


def test_outflow_verdict():
    res = aggregate_nt(_etfs_bear(), _factors_bear(), _holds_bear())
    assert res["verdict"] == "净流出"
    assert res["score"] <= OUTFLOW_T


def test_balanced_verdict():
    mid = _etf("正常", 0.1, 0.5)
    res = aggregate_nt([mid] * 11, [], [])
    assert res["verdict"] == "双向平衡"
    assert INFLOW_T > res["score"] > OUTFLOW_T


def test_channel_failsoft_renormalizes():
    """季报通道断连时自动剔除并按剩余权重归一, 日频强流入仍判净流入。"""
    res = aggregate_nt(_etfs_bull(), _factors_bull(), [])
    assert res["verdict"] == "净流入"
    assert res["channel_states"]["holdings"] == "down"
    assert res["reasoning"]  # 仍有日频/三因子依据
    assert res["holdings"]["sample"] == 0


def test_only_single_channel():
    """只剩日频通道也可出结论。"""
    res = aggregate_nt(_etfs_bull(), [], [])
    assert res["verdict"] == "净流入"
    assert res["channel_states"]["factor"] == "down"
    assert res["channel_states"]["holdings"] == "down"


def test_all_channels_down():
    res = aggregate_nt([], [], [])
    assert res["verdict"] == "数据不足"
    assert res["score"] == 0.0
    assert len(res["reasoning"]) == 3  # 每通道给出剔除原因
    assert all("剔除" in t or "不足" in t or "无有效" in t
               for t in res["reasoning"])


def test_daily_channel_half_down():
    """超过一半 ETF 无 y1 时日频通道剔除。"""
    rows = [_etf("数据断连", None, None, "none") for _ in range(6)] + \
           [_etf("净流入", 2.0, 10.0) for _ in range(5)]
    res = aggregate_nt(rows, [], [])
    assert res["channel_states"]["daily"] == "down"


def test_factor_neutral_has_no_contribution():
    """三因子全部信号中性 (无方向) 时通道不计入, 且措辞非'数据不足'。"""
    neutral = [_factor(0.5, 0.5, "none") for _ in range(11)]
    res = aggregate_nt(_etfs_bull(), neutral, [])
    assert res["channel_states"]["factor"] == "down"
    assert res["verdict"] == "净流入"  # 日频单通道仍支持
    assert res["factor"]["valid"] == 11
    factor_reason = res["levels"]["factor"]["reason"]
    assert "中性" in factor_reason and "数据不足" not in factor_reason


def test_fmt_nt_flow_roundtrip():
    res = aggregate_nt(_etfs_bull(), [], [])
    txt = fmt_nt_flow(res)
    assert "国家队资金[" in txt
    assert "净流入" in txt
    assert "ETF日频" in txt
