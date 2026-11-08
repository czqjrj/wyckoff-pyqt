"""A股状态系统单元测试 (不触网)。"""

import os

import numpy as np
import pandas as pd

from wyckoff import ashare_outlook as ao
from wyckoff.indicators import add_indicators


def _ohlcv(n=360, drift=0.0, seed=7):
    rng = np.random.default_rng(seed)
    ret = drift + rng.normal(0, 0.01, n)
    close = 100 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    vol = rng.integers(1_000_000, 2_000_000, n).astype(float)
    day = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "day": day,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        }
    )


def _features(drift=0.0):
    df = add_indicators(_ohlcv(drift=drift), symbol="sh000001")
    f = ao.compute_features(df)
    f["regime_score"] = ao.regime_score_series(f)
    return f


def test_compute_features_columns():
    f = _features(0.001)
    for col in (
        "ma20",
        "ma50",
        "ma200",
        "ret20",
        "vol20",
        "atr_pct",
        "dd250",
        "vol_pct120",
        "vola_pct250",
        "regime_score",
    ):
        assert col in f.columns
    assert f["regime_score"].between(0, 100).all()
    assert pd.notna(f["atr_pct"].iloc[-1])


def test_regime_score_uptrend_beats_downtrend():
    up = _features(0.003)["regime_score"].iloc[-1]
    down = _features(-0.003)["regime_score"].iloc[-1]
    assert up > 60
    assert down < 40
    assert up > down + 20


def test_ttl_cache_stale():
    cache = ao._TTLCache(disk_path=None)
    value, q = cache.get("x", 100, lambda: {"ok": True, "v": 1})
    assert value["v"] == 1
    assert q["ok"] is True
    assert q["stale"] is False

    def _boom():
        raise RuntimeError("boom")

    value, q = cache.get("x", 0, _boom)
    assert value["v"] == 1
    assert q["stale"] is True
    assert "boom" in q["error"]


def test_ttl_cache_disk_persist(tmp_path):
    path = str(tmp_path / "cache.json")

    def _boom():
        raise RuntimeError("boom")

    c1 = ao._TTLCache(disk_path=path)
    c1.get("x", 100, lambda: {"ok": True, "v": 2})
    assert os.path.exists(path)

    c2 = ao._TTLCache(disk_path=path)
    v, q = c2.get("x", 100, _boom)
    assert v["v"] == 2
    assert q["stale"] is False

    c3 = ao._TTLCache(disk_path=path)
    v, q = c3.get("x", 0, _boom)
    assert v["v"] == 2
    assert q["stale"] is True


def test_ttl_cache_stats(tmp_path):
    path = str(tmp_path / "cache.json")

    def _boom():
        raise RuntimeError("boom")

    c1 = ao._TTLCache(disk_path=path)
    c1.get("x", 100, lambda: {"ok": True, "v": 2})
    c1.get("x", 0, _boom)
    st = c1.stats()["x"]
    assert st["ok"] == 1
    assert st["fail"] == 1
    assert st["last_ms"] >= 0
    assert "boom" in st["last_error"]
    assert st["last_success"]

    c2 = ao._TTLCache(disk_path=path)
    st2 = c2.stats()["x"]
    assert st2["ok"] == 1
    assert st2["fail"] == 1
    assert st2["last_success"] == st["last_success"]


def test_quality_issues():
    issues = ao._quality_issues(
        {
            "indices": {"ok": True, "stale": False},
            "sectors": {"ok": False},
            "etf": {"ok": True, "stale": True},
            "liquidity": {"ok": True, "stale": False},
            "market": {"breadth": True, "amount": False},
        }
    )
    assert "行业缺失" in issues
    assert "ETF过期" in issues
    assert "成交额缺失" in issues


def test_state_labels():
    assert ao.state_label(80) == "进攻"
    assert ao.state_label(60) == "中性偏多"
    assert ao.state_label(40) == "防御"
    assert ao.state_label(25) == "谨慎"
    assert ao.state_label(10) == "风险规避"


def test_analog_candidates_do_not_use_future():
    cand = ao._analog_candidates(400, 60)
    assert len(cand) > 0
    assert cand.min() >= ao.MIN_HISTORY
    assert cand.max() + 60 < 400


def _analog_frame(n=420):
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "close": close,
            "regime_score": np.linspace(5, 95, n),
            "ma200_dev": rng.normal(0, 5, n),
            "ret20": rng.normal(0, 3, n),
            "ret60": rng.normal(0, 5, n),
            "vola_pct250": rng.uniform(0, 100, n),
            "dd250": rng.uniform(-30, 0, n),
            "vol_pct120": rng.uniform(0, 100, n),
            "ma200": pd.Series(close).rolling(200, min_periods=120).mean(),
        }
    )


def test_historical_analogs_returns_distribution():
    f = _analog_frame()
    out = ao.historical_analogs(f, horizons=(5, 20), k=25)
    assert out["ok"] is True
    assert out["n_analogs"] == 25
    for h in ("5", "20"):
        st = out["horizons"][h]
        assert st["n"] == 25
        assert 0 <= st["up_prob"] <= 1
        assert st["p10"] <= st["median"] <= st["p90"]


def test_regime_backtest_buckets_and_baseline():
    f = _analog_frame(500)
    out = ao.regime_backtest(f, horizon=20)
    assert out["ok"] is True
    assert out["buckets"]
    assert out["baseline"]
    assert sum(row["n"] for row in out["buckets"]) > 0


def test_build_scenarios_has_three_paths():
    levels = {
        "close": 3650,
        "tr_bottom": 3613,
        "tr_top": 3941,
        "near_up": 3795,
        "near_down": 3503,
        "support_levels": [3613, 3585, 3574],
        "resistance_levels": [3693, 3795, 3941],
        "up_targets": [4215, 4270, 4543],
        "down_targets": [3339, 3230, 2956],
    }
    out = ao.build_scenarios(levels)
    names = [s["name"] for s in out]
    assert len(out) == 3
    assert any("基准" in n for n in names)
    bear = next(s for s in out if "风险" in s["name"])
    assert "3503" in bear["path"]
    assert "3339" in bear["path"]
    bull = next(s for s in out if "反转" in s["name"])
    assert "4215" in bull["path"]


def test_build_etf_confirmation(monkeypatch):
    fake = {
        "verdict": "双向平衡",
        "score": -0.03,
        "bias": "平衡",
        "etf": {"total": 11, "net_y1": -40.6, "net_y20": -621.3},
        "factor": {"valid": 4, "buy": 3, "sell": 1, "avg_diff": 0.25},
        "holdings": {"sample": 3, "add": 10, "cut": 0},
        "reasoning": ["测试"],
        "factor_rows": [{"share_5d": 25.0}],
    }
    monkeypatch.setattr(ao, "_nt_flow", lambda: fake)
    out = ao.build_etf_confirmation()
    assert out["ok"] is True
    assert out["label"] == "双向平衡"
    assert out["net_y20"] == -621.3
    assert out["factor_buy"] == 3
    assert out["warnings"]
    assert "ETF" in out["warnings"][0] or "份额" in out["warnings"][0]


def test_build_etf_confirmation_missing_flow(monkeypatch):
    fake = {
        "verdict": "净流入",
        "score": 0.3,
        "etf": {"total": 0, "net_y1": 0, "net_y20": 0},
        "factor": {"valid": 4, "buy": 3, "sell": 1, "avg_diff": 0.25},
        "holdings": {"sample": 3, "add": 10, "cut": 0},
        "factor_rows": [],
    }
    monkeypatch.setattr(ao, "_nt_flow", lambda: fake)
    out = ao.build_etf_confirmation()
    assert out["label"] == "ETF资金缺失"
    assert out["flow_ok"] is False
    assert out["warnings"]


def test_build_etf_confirmation_proxy(monkeypatch):
    fake = {
        "verdict": "净流入",
        "score": 0.3,
        "etf": {"total": 11, "net_y1": None, "net_y20": None},
        "factor": {"valid": 4, "buy": 3, "sell": 1, "avg_diff": 0.25},
        "holdings": {"sample": 3, "add": 10, "cut": 0},
        "etf_rows": [
            {"source": "proxy", "proxy_y1": 1.0, "proxy_y20": 20.0},
            {"source": "proxy", "proxy_y1": 0.5, "proxy_y20": 10.0},
        ],
        "factor_rows": [],
    }
    monkeypatch.setattr(ao, "_nt_flow", lambda: fake)
    out = ao.build_etf_confirmation()
    assert out["ok"] is True
    assert out["label"] == "量价代理偏多"
    assert out["proxy_ok"] is True
    assert out["flow_source"] == "量价代理"
    assert out["net_y20"] == 30.0
    assert out["warnings"]


def test_etf_vs_market():
    assert "托底" in ao._etf_vs_market(20, "托底/流入")
    assert "防守" in ao._etf_vs_market(20, "撤退/流出")
    assert "同向" in ao._etf_vs_market(70, "托底/流入")
    assert "谨慎" in ao._etf_vs_market(70, "撤退/流出")
    assert "无法确认" in ao._etf_vs_market(20, "数据不足")
    assert "无法确认" in ao._etf_vs_market(20, "ETF资金缺失")
    assert "量价代理" in ao._etf_vs_market(20, "量价代理偏多")
    assert "量价代理" in ao._etf_vs_market(70, "量价代理偏空")


def test_risk_budget_bounds():
    for score in (0, 10, 25, 40, 60, 90):
        rb = ao.risk_budget(score, 2.0, 0.5, 0.0)
        assert 0 <= rb["low"] <= rb["high"] <= 80
    weak = ao.risk_budget(10, 5.0, 0.1, -800)
    assert weak["high"] >= 10
    assert weak["low"] <= weak["high"]


def test_fetch_sector_constituents(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(
        ak,
        "stock_sector_detail",
        lambda **_k: pd.DataFrame(
            {
                "symbol": ["sh600176", "sz000001"],
                "code": ["600176", "000001"],
                "name": ["中国巨石", "平安银行"],
                "trade": [27.45, 11.2],
                "changepercent": [-5.44, 1.23],
                "amount": [4060205103, 1000000000],
                "turnoverratio": [3.75, 1.1],
                "per": [33.4, 5.2],
                "pb": [3.2, 0.6],
            }
        ),
    )
    rows = ao.fetch_sector_constituents("玻璃行业", label="new_blhy")
    assert len(rows) == 2
    assert rows[0]["name"] == "平安银行"
    assert rows[0]["amount_yi"] == 10.0
    assert rows[1]["pct"] == -5.44
    assert rows[0]["source"] == "sina"


def test_sina_label_for_name(monkeypatch):
    import akshare as ak

    ao._SINA_LABEL_CACHE.clear()
    ao._SINA_SECTOR_MAPS.clear()
    monkeypatch.setattr(
        ak,
        "stock_sector_spot",
        lambda **_k: pd.DataFrame({"label": ["new_blhy"], "板块": ["玻璃行业"]}),
    )
    assert ao._sina_label_for_name("玻璃行业") == "new_blhy"
    assert ao._sina_label_for_name("不存在") is None


def test_sina_label_alias(monkeypatch):
    import akshare as ak

    ao._SINA_LABEL_CACHE.clear()
    ao._SINA_SECTOR_MAPS.clear()

    def _spot(indicator="新浪行业"):
        if indicator == "行业":
            return pd.DataFrame(
                {
                    "label": ["hangye_ZJ66", "hangye_ZJ67"],
                    "板块": ["货币金融服务", "资本市场服务"],
                }
            )
        return pd.DataFrame({"label": ["new_blhy"], "板块": ["玻璃行业"]})

    monkeypatch.setattr(ak, "stock_sector_spot", _spot)
    assert ao._sina_label_for_name("银行") == "hangye_ZJ66"
    assert ao._sina_label_for_name("证券") == "hangye_ZJ67"


def test_code6():
    assert ao._code6("600519") == "600519"
    assert ao._code6("sh600519") == "600519"
    assert ao._code6("000001.SZ") == "000001"
    assert ao._code6("bad") == "000bad"


def test_choose_industry():
    assert ao._choose_industry(["饮料", "白酒"], ["银行", "白酒"]) == "白酒"
    assert ao._choose_industry(["货币金融服务"], ["银行"]) == "银行"
    assert ao._choose_industry(["未知行业"], ["银行"]) == "未知行业"
    assert ao._choose_industry([], ["银行"]) is None


def test_fetch_cninfo_industry(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(
        ak,
        "stock_industry_change_cninfo",
        lambda **_k: pd.DataFrame(
            {
                "新证券简称": ["贵州茅台"] * 3,
                "行业中类": ["白酒", "白酒Ⅲ", None],
                "行业大类": ["饮料", "白酒Ⅲ", "酒、饮料和精制茶制造业"],
                "行业次类": ["食品饮料与烟草", "白酒Ⅱ", None],
                "行业门类": ["主要消费", "食品饮料", "制造业"],
                "分类标准": [
                    "巨潮行业分类标准",
                    "申银万国行业分类标准",
                    "中国上市公司协会上市公司行业分类标准",
                ],
                "变更日期": [1700000000000, 1600000000000, 1800000000000],
            }
        ),
    )
    cands = ao._fetch_cninfo_industry("600519")
    assert cands
    assert cands[0] == "白酒"
    assert "饮料" in cands
    assert "制造业" in cands


def test_detect_stock_industry_local(monkeypatch):
    ao._CONS_BY_CODE.clear()
    ao._INDUSTRY_RAW_CACHE.clear()
    ao._record_constituents_by_code("白酒", [{"code": "600519"}])

    def _boom(code):
        raise AssertionError("不应触网")

    monkeypatch.setattr(ao, "_fetch_cninfo_industry", _boom)
    out = ao.detect_stock_industry("sh600519", sector_names=["白酒", "银行"])
    assert out["code"] == "600519"
    assert out["industry"] == "白酒"
    assert out["source"] == "local"


def test_detect_stock_industry_cninfo(monkeypatch):
    ao._CONS_BY_CODE.clear()
    ao._INDUSTRY_RAW_CACHE.clear()
    monkeypatch.setattr(ao, "_fetch_cninfo_industry", lambda _c: ["白酒", "饮料"])

    def _boom(code):
        raise AssertionError("不应走东财")

    monkeypatch.setattr(ao, "_fetch_em_industry", _boom)
    out = ao.detect_stock_industry("600519", sector_names=["银行", "白酒"])
    assert out["industry"] == "白酒"
    assert out["source"] == "cninfo"
    assert len(ao._INDUSTRY_RAW_CACHE) == 1


def test_build_alerts_breakdown():
    result = {
        "primary": "sh000001",
        "indices": {"sh000001": {"name": "上证指数", "close": 3600}},
        "primary_levels": {"tr_bottom": 3613, "ma200": 3984},
        "changes": {},
        "etf": {},
        "market": {},
    }
    alerts = ao.build_alerts(result)
    titles = [a["title"] for a in alerts]
    assert "P&F 破位" in titles
    assert alerts[0]["level"] == "warn"


def test_build_alerts_state_etf_breadth():
    result = {
        "primary": "sh000001",
        "indices": {"sh000001": {"name": "上证指数", "close": 3700}},
        "primary_levels": {"tr_bottom": 3600, "ma200": 3500},
        "changes": {
            "state_changed": True,
            "score_delta": -5.0,
            "state_from": "谨慎",
            "state_to": "风险规避",
            "etf_from": "撤退/流出",
        },
        "etf": {
            "label": "托底/流入",
            "net_y1": 10.0,
            "net_y20": -20.0,
        },
        "market": {"breadth": {"up_ratio": 0.05, "limit_up": 10, "limit_down": 100}},
    }
    alerts = ao.build_alerts(result)
    titles = [a["title"] for a in alerts]
    assert "状态切换" in titles
    assert "ETF 由流出转流入" in titles
    assert "ETF 短线转流入" in titles
    assert "广度极端弱" in titles
    assert "跌停家数偏多" in titles
    assert alerts[0]["level"] == "warn"


def test_render_report_alerts():
    result = {
        "as_of": "2026-11-06",
        "primary": "sh000001",
        "indices": {"sh000001": {"name": "上证指数", "close": 3600}},
        "primary_levels": {"tr_bottom": 3613, "tr_top": 3941},
        "alerts": [
            {
                "level": "warn",
                "title": "P&F 破位",
                "detail": "收盘低于 P&F 下沿。",
            }
        ],
    }
    text = ao.render_report(result)
    assert "## 预警" in text
    assert "P&F 破位" in text
    assert "有没有预警？" in text


def test_fetch_em_sector_constituents(monkeypatch):
    import akshare as ak

    ao._EM_CONS_COOLDOWN_UNTIL = 0.0
    monkeypatch.setattr(
        ak,
        "stock_board_industry_cons_em",
        lambda **_k: pd.DataFrame(
            [
                [
                    1,
                    "600000",
                    "浦发银行",
                    10.0,
                    1.2,
                    0.1,
                    100,
                    2e8,
                    0.1,
                    10.1,
                    9.9,
                    9.9,
                    9.8,
                    0.5,
                    5.2,
                    0.6,
                ],
                [
                    2,
                    "000001",
                    "平安银行",
                    11.0,
                    -0.8,
                    -0.1,
                    100,
                    1e8,
                    0.1,
                    11.1,
                    10.9,
                    10.9,
                    10.8,
                    0.6,
                    5.0,
                    0.7,
                ],
            ]
        ),
    )
    rows = ao._fetch_em_sector_constituents("银行", limit=2)
    assert len(rows) == 2
    assert rows[0]["name"] == "浦发银行"
    assert rows[0]["source"] == "eastmoney"
    assert rows[0]["amount_yi"] == 2.0
    assert rows[1]["pct"] == -0.8


def test_sector_rotation_sina_fallback(monkeypatch):
    monkeypatch.setattr(ao, "fetch_sector_ranking", lambda: [])
    monkeypatch.setattr(
        ao,
        "_fetch_sina_sector_ranking",
        lambda: [
            {
                "name": "银行",
                "pct": 1.2,
                "flow20_yi": 0.0,
                "amount_yi": 100.0,
                "tone": "bullish",
                "source": "sina",
            },
            {
                "name": "半导体",
                "pct": -3.0,
                "flow20_yi": 0.0,
                "amount_yi": 200.0,
                "tone": "bearish",
                "source": "sina",
            },
        ],
    )
    out = ao.build_sector_rotation()
    assert out is not None
    assert out["source"] == "sina"
    assert out["has_flow"] is False
    assert out["n"] == 2
    assert out["style_gap"] == 0.0


def test_sector_rotation(monkeypatch):
    rows = [
        {"name": "银行", "pct": 1.0, "flow20_yi": 20.0, "amount_yi": 10, "tone": "bullish"},
        {"name": "电力", "pct": 0.5, "flow20_yi": 10.0, "amount_yi": 8, "tone": "bullish"},
        {"name": "半导体", "pct": -3.0, "flow20_yi": -20.0, "amount_yi": 9, "tone": "bearish"},
        {"name": "医疗服务", "pct": -3.5, "flow20_yi": -30.0, "amount_yi": 7, "tone": "bearish"},
    ]
    monkeypatch.setattr(ao, "fetch_sector_ranking", lambda: rows)
    out = ao.build_sector_rotation()
    assert out["n"] == 4
    assert out["top"][0]["name"] == "银行"
    assert out["bottom"][-1]["name"] == "医疗服务"
    assert out["style_gap"] > 0


def test_render_report_contains_sections():
    result = {
        "as_of": "2026-11-06",
        "generated_at": "2026-11-06 21:00:00",
        "primary": "sh000001",
        "indices": {
            "sh000001": {
                "name": "上证指数",
                "close": 3650,
                "day_pct": -1.1,
                "score": 23.6,
                "state": "谨慎",
                "trend_score": 0,
                "momentum_score": 30,
                "vol_score": 23,
                "drawdown_score": 71,
                "phase": "区间整理",
                "ret20": -4.3,
                "dd250": -14.3,
                "atr_pct": 1.6,
            },
        },
        "market": {
            "env": "熊市环境",
            "breadth": {"up": 700, "down": 4400, "flat": 70, "limit_up": 39, "limit_down": 22},
            "amount": {"total_yi": 16578, "vol_pct": 18},
            "fund": {"total_yi": -686},
            "emotion": {"phase": "退潮"},
        },
        "analogs": {"ok": False, "reason": "样本不足"},
        "backtest": {},
        "primary_levels": {
            "close": 3650,
            "ma20": 3693,
            "ma50": 3821,
            "ma200": 3984,
            "tr_bottom": 3613,
            "tr_top": 3941,
            "near_up": 3795,
            "near_down": 3503,
            "support_levels": [3613, 3574],
            "resistance_levels": [3693, 3795, 3941],
            "up_targets": [4215, 4270, 4543],
            "up_prob": [0.61, 0.6, 0.5],
            "down_targets": [3339, 3230, 2956],
            "down_prob": [0.84, 0.77, 0.61],
            "atr_pct": 1.6,
        },
        "scenarios": [{"name": "基准", "trigger": "x", "path": "y"}],
        "changes": {
            "previous_as_of": "2026-11-05",
            "score_delta": -0.8,
            "state_from": "谨慎",
            "state_to": "谨慎",
            "state_changed": False,
            "phase_from": "区间整理",
            "phase_to": "区间整理",
            "phase_changed": False,
            "close_delta_pct": -1.1,
            "etf_from": "双向平衡",
            "etf_to": "双向平衡",
            "up_ratio_from": 0.2,
            "up_ratio_to": 0.14,
        },
        "backtest_strategy": {
            "ok": True,
            "start": "2021-11-24",
            "end": "2026-11-06",
            "cost_bps": 5.0,
            "strategy": {
                "total_return": 12.0,
                "cagr": 2.5,
                "max_drawdown": -20.0,
                "sharpe": 0.3,
                "avg_exposure": 30.0,
            },
            "overlay": {
                "total_return": 16.0,
                "cagr": 3.2,
                "max_drawdown": -18.0,
                "sharpe": 0.4,
                "avg_exposure": 55.0,
            },
            "rolling": {
                "window": 252,
                "step": 63,
                "n": 2,
                "win_rate": 50.0,
                "avg_excess": 0.1,
            },
            "ma200": {
                "total_return": 15.0,
                "cagr": 3.0,
                "max_drawdown": -18.0,
                "sharpe": 0.35,
                "avg_exposure": 55.0,
            },
            "buy_hold": {
                "total_return": 10.0,
                "cagr": 2.0,
                "max_drawdown": -30.0,
                "sharpe": 0.2,
                "avg_exposure": 100.0,
            },
        },
        "sectors": None,
        "liquidity": {
            "ok": True,
            "label": "中性",
            "score": 49.9,
            "m1_m2": -4.0,
            "credit_yoy": -20.9,
            "shibor3m": 1.4,
            "bond10": 1.59,
            "as_of": "2026-09",
            "rate_as_of": "2026-11-06",
        },
        "etf": {
            "ok": True,
            "label": "双向平衡",
            "verdict": "双向平衡",
            "score": -0.03,
            "net_y1": -40.6,
            "net_y20": -621.3,
            "factor_buy": 3,
            "factor_sell": 1,
            "avg_diff": 0.25,
            "holdings_add": 10,
            "holdings_cut": 0,
            "holdings_sample": 3,
            "vs_market": "弱市且大资金撤退（防守优先）",
            "warnings": [],
        },
        "risk_budget": {"low": 0, "high": 10, "note": "机械降档"},
        "data_quality": {
            "indices": {
                "ok": True,
                "stale": False,
                "as_of": "2026-11-06",
                "missing": [],
                "error": "",
            },
            "sectors": {"ok": True, "stale": False, "as_of": "2026-11-06", "error": ""},
            "etf": {"ok": True, "stale": False, "as_of": "2026-11-06", "error": ""},
            "liquidity": {"ok": True, "stale": False, "as_of": "2026-09", "error": ""},
            "market": {
                "env": True,
                "breadth": True,
                "amount": True,
                "fund": True,
                "emotion": True,
                "resonance": True,
            },
        },
        "source_stats": {
            "sectors": {
                "ok": 3,
                "fail": 1,
                "last_ms": 1200,
                "last_success": "2026-11-06 21:00:00",
                "last_error": "TimeoutError: timeout",
            },
            "etf": {"ok": 2, "fail": 0, "last_ms": 800, "last_success": "2026-11-06 21:00:00"},
        },
        "disclaimer": ao.DISCLAIMER,
    }
    text = ao.render_report(result)
    assert "A股市场状态报告" in text
    assert "大白话结论" in text
    assert "现在市场怎么样" in text
    assert "比上次怎么样" in text
    assert "关键看什么" in text
    assert "资金面松不松" in text
    assert "和上次相比" in text
    assert "状态分机械规则回测" in text
    assert "MA200+状态分" in text
    assert "滚动 252 日窗口" in text
    assert "流动性/政策确认" in text
    assert "数据质量" in text
    assert "数据源状态" in text
    assert "1/4 (25%)" in text
    assert "数据靠谱吗" in text
    assert "历史类比" in text
    assert "P&F 上方三档" in text
    assert "国家队/宽基ETF确认" in text
    assert "风险预算" in text
    assert "不构成投资建议" in text
