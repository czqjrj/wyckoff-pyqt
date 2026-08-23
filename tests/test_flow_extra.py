"""A股补充数据源测试 (wyckoff.flow_extra): 龙虎榜/两融/解禁/业绩预告/北向解析。

全部离线: monkeypatch flow_extra._ak 注入假 akshare 模块。
"""
import pandas as pd
import pytest

import wyckoff.flow_extra as fe


class FakeAk:
    """最小可用的 akshare 假模块 (只实现被测接口)。"""

    def stock_lhb_detail_em(self, **kw):
        return pd.DataFrame([
            {"代码": "600001", "名称": "甲", "上榜日": "2026-08-15",
             "龙虎榜净买额": 210000000.0, "收盘价": 11.5, "涨跌幅": 8.3,
             "上榜原因": "日涨幅偏离值达7%", "解读": ""},
        ])

    def stock_lhb_stock_statistic_em(self, symbol="近一月"):
        return pd.DataFrame([
            {"代码": "600001", "名称": "甲", "上榜次数": 3,
             "龙虎榜净买额": 202000000.0, "机构买入净额": 50000000.0,
             "收盘价": 11.5, "近1个月涨跌幅": 18.0},
        ])

    def stock_margin_detail_sse(self, date=None):
        return pd.DataFrame([
            {"标的证券代码": "600001", "标的证券简称": "甲",
             "融资余额": 1000000.0, "融券余额": None},
        ])

    def stock_margin_detail_szse(self, date=None):
        return pd.DataFrame([
            {"证券代码": "000001", "证券简称": "乙",
             "融资余额": 2000000.0, "融券余额": None},
        ])

    def stock_restricted_release_detail_em(self, start_date="", end_date=""):
        return pd.DataFrame([
            {"股票代码": "600001", "股票简称": "甲", "解禁时间": "2026-09-01",
             "实际解禁市值": 3000000000.0, "占解禁前流通市值比例": 0.085,
             "限售股类型": "首发原股东限售股份",
             "解禁前一交易日收盘价": 10.0, "解禁前20日涨跌幅": 5.0},
            {"股票代码": "600002", "股票简称": "乙", "解禁时间": "2026-09-02",
             "实际解禁市值": 50000000.0, "占解禁前流通市值比例": 0.003,
             "限售股类型": "股权激励限售股份",
             "解禁前一交易日收盘价": 9.0, "解禁前20日涨跌幅": -2.0},
        ])

    def stock_yjyg_em(self, date=""):
        return pd.DataFrame([
            {"股票代码": "600001", "股票简称": "甲", "预告类型": "预增",
             "业绩变动幅度": "55.0%", "业绩变动": "净利润大增",
             "公告日期": "2026-08-20"},
        ])

    def stock_hsgt_hold_stock_em(self, market="", indicator=""):
        raise Exception("接口失效")

    def stock_hsgt_fund_flow_summary_em(self):
        return pd.DataFrame([
            {"板块": "沪股通", "资金净流入": 1500000000.0},
            {"板块": "深股通", "资金净流入": 800000000.0},
        ])


@pytest.fixture
def fake_ak(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: FakeAk())
    return FakeAk


def test_ak_missing_returns_empty(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: None)
    assert fe.fetch_lhb_detail() == []
    assert fe.fetch_lhb_stats() == []
    assert fe.fetch_margin() == []
    assert fe.fetch_restricted() == []
    assert fe.fetch_yjyg() == []
    assert fe.fetch_north() == []


def test_fetch_lhb_detail(fake_ak):
    import datetime as dt
    rows = fe.fetch_lhb_detail(start_date=dt.date(2026, 8, 1),
                               end_date=dt.date(2026, 8, 18))
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600001" and r["net"] == 2.1e8
    assert r["last"] == 11.5 and r["pct"] == 8.3


def test_fetch_lhb_stats(fake_ak):
    rows = fe.fetch_lhb_stats()
    assert rows[0]["name"] == "甲"
    assert rows[0]["times"] == 3
    assert rows[0]["net"] == 2.02e8
    assert rows[0]["pct_1m"] == 18.0


def test_fetch_margin_merges_sse_szse(fake_ak):
    rows = fe.fetch_margin()
    codes = {r["code"] for r in rows}
    assert codes == {"600001", "000001"}
    by_code = {r["code"]: r for r in rows}
    assert by_code["600001"]["mrg_bal"] == 1e6
    assert by_code["000001"]["mrg_bal"] == 2e6


def test_fetch_restricted_filters_ratio(fake_ak):
    rows = fe.fetch_restricted(days=60, min_ratio=1.0)
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["ratio"] == 8.5          # ratio 已 *100
    assert rows[0]["value"] == 3e9


def test_fetch_yjyg_ampl_stripped(fake_ak):
    rows = fe.fetch_yjyg()
    assert rows[0]["kind"] == "预增"
    assert rows[0]["ampl"] == 55.0
    assert rows[0]["msg"] == "净利润大增"


def test_fetch_north_falls_back_to_summary(fake_ak):
    """个股接口抛异常 → 兜底大盘净流入。"""
    rows = fe.fetch_north()
    assert len(rows) >= 2
    assert rows[0]["market"] == "沪股通"
    assert rows[0]["net"] == 1.5e9
    assert "15.00亿" in rows[0]["msg"]


def test_fetch_north_per_stock_when_available(monkeypatch):
    class AkWithHold(FakeAk):
        def stock_hsgt_hold_stock_em(self, market="", indicator=""):
            return pd.DataFrame([
                {"代码": "600001", "名称": "甲", "较昨日变化": 1200000.0,
                 "最新价": 11.5},
            ])
    monkeypatch.setattr(fe, "_ak", lambda: AkWithHold())
    rows = fe.fetch_north()
    assert rows[0]["code"] == "600001"
    assert rows[0]["hold_chg"] == 1.2e6
    assert rows[0]["market"] == "沪股通"


def test_cell_num_code6_helpers():
    assert fe._num("2.1亿") is None or fe._num("2.1亿") == 2.1   # 亿后缀不解析
    assert fe._num("2,100,000.5") == 2100000.5
    assert fe._num(None) is None
    assert fe._num(float("nan")) is None
    assert fe._code6("sh600001") == "600001"
    assert fe._code6(600001) == "600001"
