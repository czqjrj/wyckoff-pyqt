"""高级扫描引擎第二轮测试 (wyckoff.scan_adv): 派发/平台突破/吸筹完成度 + 大宗/调研/涨停/质押。

K线类离线: monkeypatch scan_adv.fetch_kline 注入合成数据。
P1 类离线: monkeypatch scan_adv.fetch_dzjy/jgdy/ztpool/gpzy。
"""
import numpy as np
import pandas as pd
import pytest

import wyckoff.scan_adv as sa
from wyckoff.indicators import add_indicators


def _mk(closes, vols, wob=0.05):
    n = len(closes)
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    c = np.array(closes, dtype=float) + np.sin(np.arange(n) / 9.0) * wob
    return pd.DataFrame({"day": dates, "open": c, "close": c,
                         "high": c + 0.06, "low": c - 0.06,
                         "volume": np.array(vols, dtype=float)})


@pytest.fixture
def spring_df():
    """区间10.5 → 放量跌破至9.3 → 强力收回11 → 小幅回踩 (完整吸筹链)。"""
    wob = np.sin(np.arange(120) / 7.0) * 0.2
    cl = np.r_[10.5 + wob,
               np.linspace(10.5, 9.3, 14),
               np.linspace(9.3, 11.0, 16),
               10.8 + np.sin(np.arange(20) / 6.0) * 0.15]
    vol = np.r_[np.full(120, 8e5), np.full(14, 4e6),
                np.linspace(4e6, 1.4e6, 16), np.linspace(1.2e6, 1.0e6, 20)]
    return add_indicators(_mk(cl, vol), symbol="sh600001")


@pytest.fixture
def surge_df():
    """末根 +4% 且量比5.5 (平台突破基准: 平台窄幅 + 放量上破)。"""
    cl = np.r_[np.linspace(10.0, 10.3, 110), 10.3, 10.72]
    vol = np.r_[np.full(110, 8e5), 1.2e6, 6e6]
    df = add_indicators(_mk(cl, vol), symbol="sh600008")
    df.loc[df.index[-1], "high"] = df.loc[df.index[-1], "close"] + 0.02
    return df


@pytest.fixture
def distribution_df():
    """上升 → 高位横盘 → 冲高UTAD → 回落 (派发序列)。"""
    seg = [np.linspace(10, 12, 120), np.full(40, 12.2),
           np.linspace(12.2, 13.4, 6), np.linspace(13.4, 12.4, 8),
           np.linspace(12.4, 11.0, 30)]
    cl = np.concatenate(seg)
    vol = np.r_[np.full(120, 9e5), np.full(40, 1e6),
                np.linspace(1.2e6, 3e6, 6), np.full(8, 2.5e6),
                np.full(30, 1.3e6)]
    return add_indicators(_mk(cl, vol, wob=0.06), symbol="sh600011")


def _patch(**datasets):
    def _fk(symbol, datalen=500, scale=240):
        c6 = symbol[-6:]
        return datasets.get(c6, list(datasets.values())[0]).copy()
    sa.fetch_kline = _fk


# ── K线类 ──

def test_absorption_chain_complete(spring_df):
    _patch(**{"600001": spring_df})
    rows = sa.scan_absorption(["sh600001"], workers=1)
    assert rows
    r = rows[0]
    assert "SC" in r["chain"] and ("Spring" in r["chain"] or "AR" in r["chain"])
    assert r["score"] > 0


def test_absorption_requires_bull_confirmation(distribution_df):
    """派发序列无 Spring/AR/SOS 末尾确认 → 不标吸筹完成。"""
    _patch(**{"600011": distribution_df})
    rows = sa.scan_absorption(["sh600011"], workers=1)
    assert rows == []


def test_platform_breakout_detected(surge_df):
    _patch(**{"600008": surge_df})
    rows = sa.scan_platform(["sh600008"], workers=1)
    assert rows
    r = rows[0]
    assert r["vr"] >= 1.8
    assert r["last"] > r["high20"]
    assert r["band"] <= 20


def test_platform_rejects_weak_volume(spring_df):
    """无放量上破 → 不标平台突破。"""
    _patch(**{"600001": spring_df})
    rows = sa.scan_platform(["sh600001"], workers=1)
    assert rows == []


def test_distribution_detects_bear(distribution_df):
    _patch(**{"600011": distribution_df})
    rows = sa.scan_distribution(["sh600011"], workers=1)
    assert rows
    assert rows[0]["kind"] == "派发信号"
    assert "UTAD" in rows[0]["signals"] or "BC" in rows[0]["signals"]


def test_distribution_empty_on_uptrend(spring_df):
    """上升趋势 + 无派发事件 → 不标风险。"""
    _patch(**{"600001": spring_df})
    rows = sa.scan_distribution(["sh600001"], workers=1)
    assert rows == []


# ── P1 类 ──

def test_dzjy_filters_small_and_keeps_discount(monkeypatch):
    monkeypatch.setattr(sa, "fetch_dzjy", lambda lookback_days=10: [
        {"code": "600001", "name": "A", "date": "2026-08-10", "premium": -19.4,
         "amount_yi": 0.74, "close": 33.34, "price": 26.8},
        {"code": "600002", "name": "B", "date": "2026-08-10", "premium": 0.0,
         "amount_yi": 0.01, "close": 9.0, "price": 9.0},
    ])
    rows = sa.scan_dzjy(min_amount=0.5)
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["premium"] == pytest.approx(-19.4)
    assert rows[0]["score"] > 0


def test_jgdy_filters_below_min_inst(monkeypatch):
    monkeypatch.setattr(sa, "fetch_jgdy", lambda lookback_days=7: [
        {"code": "600001", "name": "A", "last": 11.5, "inst_num": 12,
         "date": "2026-08-17", "way": "特定对象调研"},
        {"code": "600002", "name": "B", "last": 9.0, "inst_num": 2,
         "date": "2026-08-17", "way": "网络互动"},
    ])
    rows = sa.scan_jgdy(min_inst=5)
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["inst_num"] == 12


def test_ztpool_filters_below_lianban(monkeypatch):
    monkeypatch.setattr(sa, "fetch_ztpool", lambda lookback_days=5: [
        {"code": "600001", "name": "A", "last": 11.0, "pct": 10.0,
         "amount_yi": 3.0, "limit_times": 3, "open_cnt": 1,
         "sector": "半导体", "date": "2026-08-17"},
        {"code": "600002", "name": "B", "last": 6.0, "pct": 10.0,
         "amount_yi": 1.0, "limit_times": 1, "open_cnt": 2,
         "sector": "医药", "date": "2026-08-17"},
    ])
    rows = sa.scan_ztpool(min_lt=2)
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["limit_times"] == 3


def test_gpzy_filters_below_ratio(monkeypatch):
    monkeypatch.setattr(sa, "fetch_gpzy", lambda lookback_days=5: [
        {"code": "600001", "name": "A", "ratio": 62.5, "market_value": 25.0,
         "industry": "化学纤维", "pct_y1": -19.8, "date": "2026-08-14"},
        {"code": "600002", "name": "B", "ratio": 12.3, "market_value": 3.0,
         "industry": "电力", "pct_y1": 5.0, "date": "2026-08-14"},
    ])
    rows = sa.scan_gpzy(min_ratio=50)
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["ratio"] == pytest.approx(62.5)
    assert rows[0]["score"] >= 50
