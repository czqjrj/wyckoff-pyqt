"""流动性/信用确认层单元测试 (不触网)。"""

import numpy as np
import pandas as pd

from wyckoff import ashare_liquidity as al


def _money(n=36, m1_last=10.0, m2_last=8.0):
    months = pd.period_range("2019-01", periods=n, freq="M").astype(str)
    m1 = np.linspace(0, m1_last, n)
    m2 = np.linspace(8, m2_last, n)
    return pd.DataFrame({"month": months, "m1_yoy": m1, "m2_yoy": m2})


def _credit(n=36, last=10.0):
    months = pd.period_range("2019-01", periods=n, freq="M").astype(str)
    return pd.DataFrame({"month": months, "credit_yoy": np.linspace(0, last, n)})


def _shibor(n=100, last=1.0):
    dates = pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "shibor3m": np.linspace(3.0, last, n)})


def _bond(n=100, last=1.5):
    dates = pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "bond10": np.linspace(4.0, last, n)})


def _patch(monkeypatch, money=None, credit=None, shibor=None, bond=None):
    al._CACHE.clear()
    monkeypatch.setattr(al, "_fetch_money_supply", lambda: money)
    monkeypatch.setattr(al, "_fetch_credit", lambda: credit)
    monkeypatch.setattr(al, "_fetch_shibor", lambda: shibor)
    monkeypatch.setattr(al, "_fetch_bond10", lambda: bond)


def test_month_key():
    assert al._month_key("2026年9月份") == "2026-09"
    assert al._month_key("2026-09") == "2026-09"
    assert al._month_key("bad") is None


def test_date_key_handles_ms_and_string():
    assert al._date_key(1793923200000) == "2026-11-06"
    assert al._date_key("2026-11-06") == "2026-11-06"
    assert al._date_key(None) is None


def test_fetch_shibor_fallback(monkeypatch):
    import akshare as ak

    def _boom(*_a, **_k):
        raise RuntimeError("primary down")

    monkeypatch.setattr(ak, "macro_china_shibor_all", _boom)
    monkeypatch.setattr(
        ak,
        "rate_interbank",
        lambda **_k: pd.DataFrame({"报告日": [1793836800000, 1793923200000], "利率": [1.398, 1.4]}),
    )
    out = al._fetch_shibor()
    assert out is not None
    assert len(out) == 2
    assert out["date"].iloc[-1] == "2026-11-06"
    assert out["shibor3m"].iloc[-1] == 1.4


def test_fetch_bond10_fallback(monkeypatch):
    import akshare as ak

    def _boom(*_a, **_k):
        raise RuntimeError("primary down")

    monkeypatch.setattr(ak, "bond_zh_us_rate", _boom)
    monkeypatch.setattr(
        ak,
        "bond_china_yield",
        lambda **_k: pd.DataFrame(
            {
                "曲线名称": ["中债商业银行普通债收益率曲线(AAA)", "中债国债收益率曲线"],
                "日期": [1793923200000, 1793923200000],
                "3月": [1.16, 1.16],
                "6月": [1.16, 1.16],
                "1年": [1.17, 1.17],
                "3年": [1.23, 1.23],
                "5年": [1.31, 1.31],
                "7年": [1.40, 1.40],
                "10年": [1.76, 1.59],
                "30年": [2.22, 2.03],
            }
        ),
    )
    out = al._fetch_bond10()
    assert out is not None
    assert len(out) == 1
    assert out["date"].iloc[-1] == "2026-11-06"
    assert out["bond10"].iloc[-1] == 1.59


def test_liquidity_easy(monkeypatch):
    _patch(monkeypatch, _money(), _credit(), _shibor(), _bond())
    out = al.build_liquidity_context()
    assert out["ok"] is True
    assert out["label"] == "宽松"
    assert out["score"] > 65
    assert out["m1_m2"] > 0
    assert out["as_of"]
    assert out["rate_as_of"]


def test_liquidity_tight(monkeypatch):
    # M1 弱、信用弱、利率高 → 偏紧
    money = _money(m1_last=0.0, m2_last=10.0)
    credit = _credit(last=0.0)
    shibor = _shibor(last=6.0)
    bond = _bond(last=5.0)
    _patch(monkeypatch, money, credit, shibor, bond)
    out = al.build_liquidity_context()
    assert out["ok"] is True
    assert out["label"] == "偏紧"
    assert out["score"] < 35


def test_liquidity_missing(monkeypatch):
    _patch(monkeypatch, None, None, None, None)
    out = al.build_liquidity_context()
    assert out["ok"] is False
    assert out["label"] == "数据不足"
    assert out["score"] is None


def test_liquidity_partial(monkeypatch):
    _patch(monkeypatch, _money(), None, None, None)
    out = al.build_liquidity_context()
    assert out["ok"] is True
    assert out["score"] is not None
    assert out["credit_yoy"] is None
