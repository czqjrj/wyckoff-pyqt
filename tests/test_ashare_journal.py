"""A股预测日志/回填/统计单元测试 (不触网)。"""

import numpy as np
import pandas as pd

from wyckoff import ashare_journal as aj


def _fake_result(as_of="2026-01-12", close=100.0, p20=0.6):
    return {
        "as_of": as_of,
        "generated_at": "2026-01-12 16:00:00",
        "primary": "sh000001",
        "indices": {
            "sh000001": {
                "name": "上证指数",
                "close": close,
                "day_pct": -1.0,
                "score": 23.6,
                "state": "谨慎",
                "phase": "区间整理",
                "trend_score": 0,
                "momentum_score": 30,
                "vol_score": 23,
                "drawdown_score": 71,
                "atr_pct": 1.6,
            }
        },
        "primary_levels": {
            "tr_bottom": 95.0,
            "tr_top": 110.0,
            "near_up": 105.0,
            "near_down": 95.0,
            "ma20": 99.0,
            "ma50": 102.0,
            "ma200": 110.0,
            "support_levels": [95.0, 92.0],
            "resistance_levels": [105.0, 110.0],
            "up_targets": [120.0, 125.0, 130.0],
            "down_targets": [90.0, 85.0, 80.0],
        },
        "analogs": {
            "ok": True,
            "horizons": {
                "5": {"up_prob": 0.5, "median": 0.0, "p10": -2, "p90": 2, "n": 10},
                "20": {"up_prob": p20, "median": 1.0, "p10": -3, "p90": 4, "n": 10},
                "60": {"up_prob": 0.6, "median": 3.0, "p10": -5, "p90": 8, "n": 10},
            },
        },
        "scenarios": [
            {"name": "基准: 区间震荡", "trigger": "x", "path": "95-110"},
            {"name": "风险: 破位", "trigger": "y", "path": "90-85"},
            {"name": "反转: 右侧", "trigger": "z", "path": "120-125"},
        ],
        "market": {
            "env": "熊市环境",
            "breadth": {"up_ratio": 0.14, "limit_up": 39, "limit_down": 22},
            "amount": {"total_yi": 16578},
            "fund": {"total_yi": -686},
            "emotion": {"phase": "退潮"},
        },
        "etf": {
            "ok": True,
            "label": "双向平衡",
            "vs_market": "弱市、大资金未明显托底（防守/观望）",
            "net_y20": -621.3,
            "score": -0.034,
        },
        "liquidity": {
            "ok": True,
            "label": "中性",
            "score": 49.9,
            "m1_m2": -4.0,
            "credit_yoy": -20.9,
            "shibor3m": 1.4,
            "bond10": 1.59,
            "as_of": "2026-09",
        },
        "risk_budget": {"low": 0, "high": 10},
    }


def _fake_kline(n=80):
    day = pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    close = 100 + np.arange(n) * 0.5
    high = close + 2
    low = close - 1
    return pd.DataFrame(
        {
            "day": day,
            "open": close - 0.5,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


def test_record_outlook_and_dedupe(tmp_path):
    path = str(tmp_path / "journal.json")
    result = _fake_result()
    info = aj.record_outlook(result, path=path)
    assert info["ok"] is True
    assert info["total"] == 1
    data = aj.load_journal(path)
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["id"] == "2026-01-12:sh000001"
    assert rec["score"] == 23.6
    assert rec["analogs"]["20"]["up_prob"] == 0.6

    # 同一天重复记录: 覆盖更新, 不新增
    result["indices"]["sh000001"]["score"] = 25.0
    info2 = aj.record_outlook(result, path=path)
    assert info2["replaced"] is True
    assert info2["total"] == 1
    assert aj.load_journal(path)["records"][0]["score"] == 25.0


def test_record_stock(tmp_path):
    path = str(tmp_path / "stock.json")
    data = {
        "symbol": "sh600519",
        "name": "贵州茅台",
        "as_of": "2026-01-12",
        "close": 1500.0,
        "confirm": False,
        "summary": [{"label": "综合", "value": "偏多 +10", "tone": "bullish"}],
        "precomputed": {
            "phase": "吸筹",
            "fusion": {"score": 10, "confidence": "中"},
            "trade_plan": [
                "  方向: 多头/突破",
                "  止损: 1450",
                "  目标1: 1600",
                "  目标2: 1700",
            ],
        },
    }
    market = {
        "primary": "sh000001",
        "indices": {"sh000001": {"state": "谨慎", "score": 23.6}},
        "risk_budget": {"low": 0, "high": 15},
    }
    info = aj.record_stock(data, market, path=path)
    assert info["ok"] is True
    assert info["total"] == 1
    rec = aj.load_journal(path)["records"][0]
    assert rec["symbol"] == "sh600519"
    assert rec["name"] == "贵州茅台"
    assert rec["stop"] == 1450.0
    assert rec["target1"] == 1600.0
    assert rec["fusion_score"] == 10.0
    assert rec["market"]["state"] == "谨慎"

    info2 = aj.record_stock(data, market, path=path)
    assert info2["replaced"] is True
    assert info2["total"] == 1


def _stock_kline(n=80):
    day = pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    close = 100 + np.arange(n) * 0.5
    return pd.DataFrame(
        {
            "day": day,
            "open": close - 0.5,
            "high": close + 10,
            "low": close - 2,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


def test_backfill_stocks_and_stats(tmp_path, monkeypatch):
    path = str(tmp_path / "stock.json")
    data = {
        "symbol": "sh600519",
        "name": "贵州茅台",
        "as_of": "2026-01-12",
        "close": 103.5,
        "confirm": False,
        "summary": [],
        "precomputed": {
            "phase": "吸筹",
            "fusion": {"score": 10, "confidence": "中"},
            "trade_plan": [
                "  方向: 多头/突破",
                "  止损: 90",
                "  目标1: 120",
            ],
        },
    }
    aj.record_stock(data, path=path)
    monkeypatch.setattr(aj, "fetch_kline", lambda *a, **k: _stock_kline())
    summary = aj.backfill_stocks(path=path, datalen=100)
    assert summary["ok"] is True
    assert summary["matured"]["5"] == 1
    assert summary["matured"]["20"] == 1
    assert summary["matured"]["60"] == 1

    rec = aj.load_journal(path)["records"][0]
    o20 = rec["outcomes"]["20"]
    assert o20["ret"] > 0
    assert o20["direction"] == "偏多"
    assert o20["direction_hit"] is True
    assert o20["target_hit"] is True
    assert o20["stop_hit"] is False
    assert o20["first_event"] == "target"

    stats = aj.compute_stock_stats(path=path, horizon=20)
    assert stats["ok"] is True
    assert stats["matured"]["20"] == 1
    h20 = stats["horizons"]["20"]
    assert h20["n"] == 1
    assert h20["target_hit"] == 1.0
    assert h20["events"]["target"] == 1
    assert stats["by_phase"]
    assert "个股预测回填统计" in aj.render_stock_stats(stats)


def test_compare_with_previous(tmp_path):
    path = str(tmp_path / "journal.json")
    aj.record_outlook(_fake_result(as_of="2026-01-05", close=100.0), path=path)
    r2 = _fake_result(as_of="2026-01-12", close=101.0)
    r2["indices"]["sh000001"]["score"] = 30.0
    r2["indices"]["sh000001"]["state"] = "防御"
    aj.record_outlook(r2, path=path)
    ch = aj.compare_with_previous(r2, path=path)
    assert ch is not None
    assert ch["previous_as_of"] == "2026-01-05"
    assert ch["score_delta"] == 6.4
    assert ch["state_from"] == "谨慎"
    assert ch["state_to"] == "防御"
    assert ch["state_changed"] is True
    assert ch["close_delta_pct"] == 1.0


def test_load_journal_corrupt(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    data = aj.load_journal(str(path))
    assert data["records"] == []


def test_backfill_and_stats(tmp_path, monkeypatch):
    path = str(tmp_path / "journal.json")
    aj.record_outlook(_fake_result(), path=path)
    monkeypatch.setattr(aj, "fetch_kline", lambda *a, **k: _fake_kline())
    summary = aj.backfill(path=path, datalen=100)
    assert summary["ok"] is True
    assert summary["matured"]["5"] == 1
    assert summary["matured"]["20"] == 1
    assert summary["matured"]["60"] == 1

    rec = aj.load_journal(path)["records"][0]
    o20 = rec["outcomes"]["20"]
    assert o20["ret"] > 0
    assert o20["up"] is True
    assert o20["direction"] == "偏多"
    assert o20["direction_hit"] is True
    assert o20["brier"] == 0.16
    assert o20["scenario"] == "bull"

    stats = aj.compute_stats(path=path, horizon=20)
    assert stats["ok"] is True
    assert stats["matured"]["20"] == 1
    h20 = stats["horizons"]["20"]
    assert h20["n"] == 1
    assert h20["up_rate"] == 1.0
    assert h20["direction_hit"] == 1.0
    assert h20["calibration"]
    assert stats["by_primary"]
    assert stats["by_state"]
    assert stats["by_etf"]
    assert stats["by_liquidity"]
    assert "A股预测回填统计" in aj.render_stats(stats)


def test_backfill_pending(tmp_path, monkeypatch):
    path = str(tmp_path / "journal.json")
    # 最新一根: 未来 20/60 日还不够
    result = _fake_result(as_of="2026-03-27")
    aj.record_outlook(result, path=path)
    monkeypatch.setattr(aj, "fetch_kline", lambda *a, **k: _fake_kline())
    summary = aj.backfill(path=path, datalen=100)
    assert summary["ok"] is True
    assert summary["pending"]["20"] == 1
    assert summary["matured"]["20"] == 0


def test_direction_thresholds():
    assert aj._direction(0.6) == "偏多"
    assert aj._direction(0.4) == "偏空"
    assert aj._direction(0.5) == "中性"
    assert aj._direction(None) == "中性"
