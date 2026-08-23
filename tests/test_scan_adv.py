"""高级扫描引擎测试 (wyckoff.scan_adv): 8 项基于K线的专项扫描 + 调度。

全部离线: monkeypatch scan_adv.fetch_kline 注入合成数据, 不访问网络。
P1 数据源 (龙虎榜/两融/…) 的扫描逻辑见 test_flow_extra.py。
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
    """区间10.5 → 放量跌破至9.3 → 强力收回11 → 小幅回踩10.8 (经典 Spring)。"""
    wob = np.sin(np.arange(120) / 7.0) * 0.2
    cl = np.r_[10.5 + wob,
               np.linspace(10.5, 9.3, 14),
               np.linspace(9.3, 11.0, 16),
               10.8 + np.sin(np.arange(20) / 6.0) * 0.15]
    vol = np.r_[np.full(120, 8e5), np.full(14, 4e6),
                np.linspace(4e6, 1.4e6, 16), np.linspace(1.2e6, 1.0e6, 20)]
    return add_indicators(_mk(cl, vol), symbol="sh600001")


@pytest.fixture
def pnf_df():
    """区间 10.3~11.2 三次触顶后放量上破至 12.6 (三重顶突破)。"""
    seg = []
    for _ in range(3):
        seg.append(np.linspace(10.2, 11.2, 22))
        seg.append(np.linspace(11.2, 10.3, 18))
    seg.append(np.linspace(10.3, 12.6, 26))
    cl = np.concatenate(seg)
    n = len(cl)
    vol = np.r_[np.full(n - 26, 9e5), np.linspace(1e6, 4e6, 26)]
    return add_indicators(_mk(cl, vol, wob=0.05), symbol="sh600003")


@pytest.fixture
def surge_df():
    """末根 +4% 且量比5.5 (放量突破)。"""
    cl = np.r_[np.linspace(10.0, 10.3, 110), 10.3, 10.72]
    vol = np.r_[np.full(110, 8e5), 1.2e6, 6e6]
    return add_indicators(_mk(cl, vol), symbol="sh600008")


@pytest.fixture
def trend_df():
    """持续上行 + 正弦波动 (触发波浪斐波那契关键位)。"""
    t = np.arange(220)
    rng = np.random.default_rng(7)
    close = np.linspace(10, 16, 220) + np.sin(t / 18) * 0.6 + rng.normal(0, 0.08, 220)
    vol = np.full(220, 1e6)
    return add_indicators(_mk(close, vol, wob=0.0), symbol="sh600009")


def _patch(datasets, codes):
    """把不同 code 映射到不同合成 df 的 fetch_kline。"""
    def _fk(symbol, datalen=500, scale=240):
        c6 = symbol[-6:]
        return datasets.get(c6, datasets.get(list(datasets)[0])).copy()
    sa.fetch_kline = _fk


# ── 1. 回踩买点 ──

def test_pullback_detects_spring(spring_df):
    _patch({"600001": spring_df}, ["sh600001"])
    rows = sa.scan_pullback(["sh600001"], workers=1)
    assert rows, "应识别到 Spring 后回调未破位"
    r = rows[0]
    assert r["sig"] == "Spring"
    assert r["break"] == "未破位"
    assert r["sig_price"] < r["peak"]          # 买点低于其后高点 (谷底型)
    assert 0 < r["pull"] < 16
    assert r["score"] > 0


def test_pullback_requires_trough_buy_point(trend_df):
    """单边上行无谷底买点 → 无结果。"""
    _patch({"600009": trend_df}, ["sh600009"])
    rows = sa.scan_pullback(["sh600009"], workers=1)
    assert rows == []


# ── 2. P&F 突破 ──

def test_pnf_breakout_triple_top(pnf_df):
    _patch({"600003": pnf_df}, ["sh600003"])
    rows = sa.scan_pnf_breakout(["sh600003"], workers=1)
    assert rows
    r = rows[0]
    assert r["break"] == "三重顶突破"
    assert r["target"] > r["res"]
    assert r["lead"] > 0
    assert r["tgt_dist"] is not None


# ── 3. 量能异动 ──

def test_volume_surge_breakout(surge_df):
    _patch({"600008": surge_df}, ["sh600008"])
    rows = sa.scan_volume_surge(["sh600008"], workers=1)
    assert rows
    assert rows[0]["kind"] == "放量突破"
    assert rows[0]["vr"] >= 2.0
    assert rows[0]["pct"] >= 3


# ── 4. 量价背离 ──

def test_volume_divergence_lag(spring_df):
    """放量滞涨: 平盘处放量收于开盘下方。"""
    cl = np.linspace(10.2, 10.2, 80) + np.sin(np.arange(80) / 9.0) * 0.02
    cl[-1] = 10.18
    vol = np.r_[np.full(79, 8e5), 5e6]
    df = add_indicators(_mk(cl, vol), symbol="sh600006")
    _patch({"600006": df}, ["sh600006"])
    rows = sa.scan_volume_divergence(["sh600006"], workers=1)
    assert any(r["kind"] == "放量滞涨" for r in rows)


def test_volume_divergence_shrink_peak():
    """缩量过峰: 平台回升后缩量强收创新高。"""
    cl = np.r_[np.full(30, 9.4), np.full(30, 9.7), np.full(28, 10.0),
               10.30, 10.34, 10.38]
    vol = np.r_[np.full(89, 1.4e6), 4e5, 4e5]
    df = _mk(cl, vol)
    df.loc[df.index[-3:], "high"] = df.loc[df.index[-3:], "close"]  # 强收
    df = add_indicators(df, symbol="sh600007")
    _patch({"600007": df}, ["sh600007"])
    rows = sa.scan_volume_divergence(["sh600007"], workers=1)
    assert any(r["kind"] == "缩量过峰" for r in rows)


# ── 5. 波浪亲密度 ──

def test_wave_proximity_returns_levels(trend_df):
    _patch({"600009": trend_df}, ["sh600009"])
    rows = sa.scan_wave_proximity(["sh600009"], workers=1)
    assert rows
    r = rows[0]
    assert "level" in r and "kp" in r and "dist" in r
    assert r["dist"] <= 4.0
    assert r["score"] > 0


# ── 6. 持仓风险 ──

def test_portfolio_risk_breaks_when_stop_violated(spring_df, monkeypatch):
    from wyckoff import storage
    monkeypatch.setattr(storage, "load_portfolio", lambda: [
        {"code": "600001", "name": "测试", "cost": 9.0, "stop": 12.0},
    ])
    _patch({"600001": spring_df}, ["600001"])
    rows = sa.scan_portfolio_risk(workers=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["broke"] == "是"             # 现价 10.77 < 止损 12 → 破位
    assert r["pnl"] is not None and r["pnl"] > 0
    assert r["advice"] in ("减仓/离场", "破位警戒", "逢高减仓")


def test_portfolio_risk_hold_when_intact(spring_df, monkeypatch):
    from wyckoff import storage
    monkeypatch.setattr(storage, "load_portfolio", lambda: [
        {"code": "600001", "name": "测试", "cost": 9.0, "stop": 8.0},
    ])
    _patch({"600001": spring_df}, ["600001"])
    rows = sa.scan_portfolio_risk(workers=1)
    assert rows[0]["broke"] == "否"


# ── 7. 候选池巡检 ──

def test_candidates_status_strong(spring_df, monkeypatch):
    from wyckoff import storage
    monkeypatch.setattr(storage, "load_candidates", lambda: [
        {"code": "600001", "name": "测试", "signals": "Spring/ST",
         "date": "2026-01-01 10:00"},
    ])
    _patch({"600001": spring_df}, ["600001"])
    rows = sa.scan_candidates_status(workers=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["origin"] == "Spring/ST"
    assert r["status"] == "已走强"          # 上升趋势 + 距20日高 ~0
    assert r["days"] is not None


# ── 8. 板块联动 ──

def test_sector_driven_merges(surge_df, monkeypatch):
    from wyckoff import backtest
    monkeypatch.setattr(backtest, "scan_sectors", lambda: [
        {"name": "半导体", "bk_code": "BK001", "live": True, "tone": "bullish",
         "flow20_yi": 25.0, "score": 92.0},
        {"name": "电力", "bk_code": "BK002", "live": True, "tone": "bullish",
         "flow20_yi": 6.0, "score": 60.0},
    ])
    monkeypatch.setattr(backtest, "scan_sector_stocks", lambda *a, **k: [
        {"code": "600008", "name": "样本", "last": 10.7, "phase": "上升趋势",
         "signals": ["SOS"], "score": 70},
    ])
    rows = sa.scan_sector_driven(workers=1)
    assert rows
    r = rows[0]
    assert r["sector"] == "半导体"
    assert r["sec_score"] == 92.0
    assert r["signals"] == "SOS"
    assert r["score"] == 70


# ── P1 调度扫描 (数据源 mock) ──

def test_run_scan_p1_uses_flow_extra(monkeypatch):
    """P1 扫描走 flow_extra 且不炸。"""
    monkeypatch.setattr(sa, "fetch_lhb_stats", lambda: [])
    monkeypatch.setattr(sa, "fetch_margin", lambda days_ago=0: [])
    monkeypatch.setattr(sa, "fetch_restricted", lambda days=60: [])
    monkeypatch.setattr(sa, "fetch_yjyg", lambda: [])
    monkeypatch.setattr(sa, "fetch_north", lambda: [])
    monkeypatch.setattr(sa, "fetch_dzjy", lambda lookback_days=10: [])
    monkeypatch.setattr(sa, "fetch_jgdy", lambda lookback_days=7: [])
    monkeypatch.setattr(sa, "fetch_ztpool", lambda lookback_days=5: [])
    monkeypatch.setattr(sa, "fetch_gpzy", lambda lookback_days=5: [])
    for key in ("lhb", "margin", "restricted", "yjyg", "north",
                "dzjy", "jgdy", "ztpool", "gpzy"):
        rows = sa.run_scan(key)
        assert isinstance(rows, list)


def test_lhb_filters_negative(monkeypatch):
    """龙虎榜扫描只保留净买入/机构买入为正的股票。"""
    monkeypatch.setattr(sa, "fetch_lhb_stats", lambda: [
        {"code": "600001", "name": "A", "times": 3, "net": 2e8,
         "inst_net": 5e7, "last": 10.0, "pct_1m": 18.0},
        {"code": "600002", "name": "B", "times": 1, "net": -1e8,
         "inst_net": 0.0, "last": 9.0, "pct_1m": -5.0},
    ])
    rows = sa.scan_lhb()
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["times"] == 3 and rows[0]["net"] == 2.0


def test_margin_surge_filters(monkeypatch):
    monkeypatch.setattr(sa, "fetch_margin", lambda days_ago=0: (
        [{"code": "600001", "name": "A", "mrg_bal": 11e8, "sec_bal": 0.1e8}]
        if days_ago == 0 else
        [{"code": "600001", "name": "A", "mrg_bal": 10e8, "sec_bal": 0.1e8}]))
    rows = sa.scan_margin(bench_days=5)
    assert len(rows) == 1
    assert rows[0]["mrg_chg"] == pytest.approx(10.0, rel=0.01)


def test_restricted_skips_low_ratio(monkeypatch):
    monkeypatch.setattr(sa, "fetch_restricted", lambda days=60: [
        {"code": "600001", "name": "A", "date": "2026-09-01", "value": 3e9,
         "ratio": 8.5, "type": "首发原股东", "last": 10.0, "pct20": 5.0},
        {"code": "600002", "name": "B", "date": "2026-09-02", "value": 5e7,
         "ratio": 0.3, "type": "股权激励", "last": 9.0, "pct20": -2.0},
    ])
    rows = sa.scan_restricted()
    assert [r["code"] for r in rows] == ["600001"]
    assert rows[0]["ratio"] == 8.5


def test_yjyg_classifies_up_down(monkeypatch):
    monkeypatch.setattr(sa, "fetch_yjyg", lambda: [
        {"code": "600001", "name": "A", "kind": "预增", "ampl": 55.0,
         "msg": "净利润大增", "date": "2026-08-20", "last": None},
        {"code": "600002", "name": "B", "kind": "续亏", "ampl": -12.0,
         "msg": "持续亏损", "date": "2026-08-20", "last": None},
        {"code": "600003", "name": "C", "kind": "不确定", "ampl": 0.0,
         "msg": "无法判断", "date": "2026-08-20", "last": None},
    ])
    rows = sa.scan_yjyg()
    codes = {r["code"]: r for r in rows}
    assert "600001" in codes and "600002" in codes
    assert "600003" not in codes


def test_north_returns_rows(monkeypatch):
    monkeypatch.setattr(sa, "fetch_north", lambda: [
        {"market": "沪股通", "net": 1.5e9, "code": "", "name": "",
         "hold_chg": None, "last": None,
         "msg": "沪股通 净流入15.00亿"},
    ])
    rows = sa.scan_north()
    assert rows and rows[0]["market"] == "沪股通"


def test_run_scan_all_keys_dispatch(spring_df, monkeypatch):
    """run_scan 对注册表全部 key 都能调度并返回列表 (含空)。"""
    _patch({"600001": spring_df}, ["600001"])
    monkeypatch.setattr(sa, "fetch_lhb_stats", lambda: [])
    monkeypatch.setattr(sa, "fetch_margin", lambda days_ago=0: [])
    monkeypatch.setattr(sa, "fetch_restricted", lambda days=60: [])
    monkeypatch.setattr(sa, "fetch_yjyg", lambda: [])
    monkeypatch.setattr(sa, "fetch_north", lambda: [])
    monkeypatch.setattr(sa, "fetch_dzjy", lambda lookback_days=10: [])
    monkeypatch.setattr(sa, "fetch_jgdy", lambda lookback_days=7: [])
    monkeypatch.setattr(sa, "fetch_ztpool", lambda lookback_days=5: [])
    monkeypatch.setattr(sa, "fetch_gpzy", lambda lookback_days=5: [])
    from wyckoff.scan_adv import SCAN_REGISTRY
    for s in SCAN_REGISTRY:
        key = s["key"]
        rows = sa.run_scan(key, codes=["sh600001"], workers=1)
        assert isinstance(rows, list), key
