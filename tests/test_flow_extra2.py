"""A股补充数据源第二轮测试 (wyckoff.flow_extra): 大宗/机构调研/涨停池/质押。

全部离线: monkeypatch flow_extra._ak 注入假 akshare 模块。
"""
import datetime as dt

import pandas as pd
import pytest

import wyckoff.flow_extra as fe


def _recent_friday():
    d = dt.date.today()
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


def _today():
    return dt.date.today().strftime("%Y%m%d")


class DzjyAk:
    def stock_dzjy_mrtj(self, start_date="", end_date=""):
        return pd.DataFrame([
            {"交易日期": pd.Timestamp("2026-08-10"), "证券代码": "600001",
             "证券简称": "甲", "涨跌幅": 2.0, "收盘价": 10.0, "成交价": 9.8,
             "折溢率": -0.025, "成交笔数": 1, "成交总量": 500.0,
             "成交总额": 5000.0, "成交总额/流通市值": 0.5},
        ])


class JgdyAk:
    def __init__(self):
        self.calls = []

    def stock_jgdy_tj_em(self, date=""):
        self.calls.append(date)
        if date == _today():
            raise Exception("今日暂无披露")
        return pd.DataFrame([
            {"代码": "600001", "名称": "甲", "最新价": 11.5, "涨跌幅": 3.0,
             "接待机构数量": 12, "接待方式": "特定对象调研",
             "接待日期": "2026-08-17"},
        ])


class ZtpoolAk:
    def stock_zt_pool_em(self, date=""):
        if date == _today():
            raise Exception("非交易日")
        return pd.DataFrame([
            {"代码": "600001", "名称": "甲", "涨跌幅": 10.0, "最新价": 11.0,
             "成交额": 3e8, "炸板次数": 1, "连板数": 3, "所属行业": "半导体"},
        ])


class GpzyAk:
    def __init__(self):
        self.calls = []

    def stock_gpzy_pledge_ratio_em(self, date=""):
        self.calls.append(date)
        if date != _recent_friday():
            raise Exception("该日无质押披露")
        return pd.DataFrame([
            {"股票代码": "600001", "股票简称": "甲", "质押比例": 62.5,
             "质押市值": 2500000000.0, "所属行业": "化学纤维",
             "交易日期": date},
        ])


@pytest.fixture
def fake_dzjy(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: DzjyAk())


@pytest.fixture
def fake_jgdy(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: JgdyAk())


@pytest.fixture
def fake_ztpool(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: ZtpoolAk())


@pytest.fixture
def fake_gpzy(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: GpzyAk())


def test_ak_missing_returns_empty(monkeypatch):
    monkeypatch.setattr(fe, "_ak", lambda: None)
    assert fe.fetch_dzjy() == []
    assert fe.fetch_jgdy() == []
    assert fe.fetch_ztpool() == []
    assert fe.fetch_gpzy() == []


def test_fetch_dzjy_parses_units(fake_dzjy):
    rows = fe.fetch_dzjy(lookback_days=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600001"
    assert r["premium"] == pytest.approx(-2.5)      # 小数 → %
    assert r["amount_yi"] == pytest.approx(0.5)     # 万 → 亿
    assert r["price"] == 9.8 and r["close"] == 10.0


def test_fetch_jgdy_skips_empty_day(fake_jgdy):
    rows = fe.fetch_jgdy(lookback_days=3)
    assert len(rows) == 1
    assert rows[0]["inst_num"] == 12
    assert rows[0]["code"] == "600001"


def test_fetch_ztpool_uses_lianban(fake_ztpool):
    rows = fe.fetch_ztpool()
    assert len(rows) == 1
    assert rows[0]["limit_times"] == 3
    assert rows[0]["open_cnt"] == 1
    assert rows[0]["pct"] == 10.0


def test_fetch_gpzy_hits_friday(fake_gpzy):
    rows = fe.fetch_gpzy(lookback_days=5)
    assert len(rows) == 1
    assert rows[0]["ratio"] == 62.5
    assert rows[0]["market_value"] == pytest.approx(25.0)   # 亿
    assert rows[0]["date"] == _recent_friday()


def test_fetch_margin_carries_date(monkeypatch):
    from test_flow_extra import FakeAk
    monkeypatch.setattr(fe, "_ak", lambda: FakeAk())
    rows = fe.fetch_margin(days_ago=1)
    assert rows and "date" in rows[0]
    assert len(rows[0]["date"]) == 8
