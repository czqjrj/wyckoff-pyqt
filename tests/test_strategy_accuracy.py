"""策略级准确度 + 盈利能力追踪测试 (wyckoff.paper_strategy_accuracy)。

覆盖: 信号记录/合并/评估、预测准确度聚合、条件单触点正确率、平仓盈利聚合、
计报告。全部离线, 不访问网络; 数据目录由 conftest.py 重定向到临时目录。
"""
import os

import pytest

import wyckoff.paper_strategy_accuracy as psa
from wyckoff.paper import load_state


@pytest.fixture(autouse=True)
def clean_sig_file(monkeypatch):
    os.environ.pop("PAPER_STRATEGY_ACCURACY_FILE", None)
    monkeypatch.setattr(psa, "_STATS_CACHE", None)
    monkeypatch.setattr(psa, "_STATS_CACHE_KEY", None)
    pth = psa.PAPER_STRATEGY_ACCURACY_FILE
    if os.path.exists(pth):
        os.remove(pth)
    yield
    if os.path.exists(pth):
        os.remove(pth)


def test_record_mark_fired_and_stats():
    psa.record_signal("paper_discipline_bull", "600001", "600001", "测试一",
                      "Spring", 90, "2024-01-02", 10.0)
    psa.record_signal("long_buy_left", "600002", "600002",
                      "测试二", "左侧买点", 80, "2024-01-02", 20.0)
    psa.mark_fired("paper_discipline_bull", "600001")
    recs = psa.load_signals()
    assert len(recs) == 2
    fired = next(r for r in recs if r["symbol"] == "600001")
    assert fired["fired"] is True
    s = psa.signal_stats()
    assert s["paper_discipline_bull"]["n"] == 1
    assert s["long_buy_left"]["n"] == 1
    assert s["_summary"]["total"] == 2


def test_record_cooldown_merge():
    import numpy as np
    import pandas as pd
    n = 60
    closes = np.linspace(10.0, 11.0, n) + 0.1 * np.sin(np.arange(n))
    df = pd.DataFrame({"day": pd.date_range("2024-01-02", periods=n, freq="B"),
                       "open": closes, "close": closes,
                       "high": closes + 0.05, "low": closes - 0.05,
                       "volume": np.full(n, 8e5)})
    psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                      "Spring", 90, "2024-01-02", 10.0, df=df)
    rc = psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                           "Spring", 95, "2024-01-03", 11.0, df=df)
    assert rc == -1
    recs = psa.load_signals()
    assert len(recs) == 1
    assert recs[0]["conf"] == 95
    assert recs[0]["ref_px"] == 11.0


def test_record_cooldown_merge_without_df():
    """实盘扫描路径 df=None 时, 冷却窗内跨扫描日同事件仍应合并 (防重复入库)。"""
    psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                      "Spring", 90, "2024-01-02", 10.0)
    rc = psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                           "Spring", 95, "2024-01-03", 11.0)
    assert rc == -1
    recs = psa.load_signals()
    assert len(recs) == 1
    assert recs[0]["conf"] == 95
    assert recs[0]["ref_px"] == 11.0
    assert recs[0]["date"] == "2024-01-03"


def test_record_without_df_cooldown_not_merged_outside_window():
    """df=None 时超出冷却窗(日)的同事件应作为新信号入库。"""
    psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                      "Spring", 90, "2024-01-02", 10.0)
    rc = psa.record_signal("paper_discipline_bull", "600001", "600001", "测试",
                           "Spring", 95, "2024-03-05", 15.0)
    assert rc == 1
    recs = psa.load_signals()
    assert len(recs) == 2


def test_cond_accuracy():
    st = load_state()
    st["conditions"] = [
        {"status": "done", "symbol": "600001", "correct": True,
         "reason": "自动:paper_discipline_bull:Spring(90)"},
        {"status": "done", "symbol": "600001", "correct": False,
         "reason": "自动:paper_discipline_bull:Spring(90)"},
        {"status": "done", "symbol": "600002", "correct": True,
         "reason": "自动:long_buy_left:左侧买点(80)"},
        {"status": "pending", "symbol": "600001", "correct": None},
    ]
    out = psa.cond_accuracy(st)
    assert out["paper_discipline_bull"]["done"] == 2
    assert out["paper_discipline_bull"]["correct"] == 1
    assert out["paper_discipline_bull"]["accuracy"] == 0.5
    assert out["long_buy_left"]["accuracy"] == 1.0


def test_profit_summary_geometric_cum():
    st = load_state()
    st["closed"] = [
        {"symbol": "600001", "strategy": "paper_discipline_bull",
         "ret": 0.10, "bars": 5},
        {"symbol": "600002", "strategy": "paper_discipline_bull",
         "ret": -0.05, "bars": 6},
        {"symbol": "600003", "strategy": "long_buy_left",
         "ret": 0.20, "bars": 10},
    ]
    out = psa.profit_summary(st)
    pb = out["paper_discipline_bull"]
    assert pb["n"] == 2
    assert pb["win_rate"] == 0.5
    assert pb["cum_ret"] == pytest.approx((1.10 * 0.95) - 1.0)
    assert pb["avg_ret"] == pytest.approx((0.10 - 0.05) / 2)
    assert out["long_buy_left"]["n"] == 1


def test_strategy_report_renders():
    st = load_state()
    st["closed"] = [{"symbol": "600001", "strategy": "paper_discipline_bull",
                     "ret": 0.05, "bars": 5}]
    rep = psa.strategy_report(st)
    md = psa._render_report(rep)
    assert "paper_discipline_bull" in rep
    assert "模拟盘策略追踪报告" in md
    assert "%" in md


def test_apply_auto_conditions_uses_event_anchor(monkeypatch):
    """实盘条件记录应以事件日/事件价锚点入库, 而非扫描日 (5.8 对齐回测口径)。"""
    import wyckoff.paper as paper
    import wyckoff.paper._conditions as cond
    captured = {}

    def fake_record(strategy, symbol, code, name, event_type, conf, date, price,
                    fired=False):
        captured.update(strategy=strategy, symbol=symbol, code=code,
                        event_type=event_type, conf=conf, date=date,
                        price=price, fired=fired)
        return 1

    monkeypatch.setattr(cond.paper_strategy_accuracy, "record_signal", fake_record)
    monkeypatch.setattr(paper, "has_position", lambda *_a, **_k: False)
    st = load_state()
    st["conditions"] = []
    cand = {
        "code": "600001", "name": "测试", "strategy": "paper_discipline_bull",
        "type": "Spring", "conf": 92, "auto_cond_price": 11.0,
        "event_date": "2024-03-05", "event_px": 9.8,
    }
    cond._apply_auto_conditions(st, [cand])
    assert captured["date"] == "2024-03-05"
    assert captured["price"] == pytest.approx(9.8)
    assert captured["event_type"] == "Spring"
    assert captured["conf"] == 92


def test_apply_auto_conditions_falls_back_to_scan_day(monkeypatch):
    """候选不带事件锚点 (旧格式/外部候选) 时回退扫描日, 不崩。"""
    import wyckoff.paper as paper
    import wyckoff.paper._conditions as cond
    captured = {}

    def fake_record(strategy, symbol, code, name, event_type, conf, date, price,
                    fired=False):
        captured.update(date=date, price=price)
        return 1

    monkeypatch.setattr(cond.paper_strategy_accuracy, "record_signal", fake_record)
    monkeypatch.setattr(paper, "has_position", lambda *_a, **_k: False)
    st = load_state()
    st["conditions"] = []
    cand = {
        "code": "600002", "name": "测试", "strategy": "paper_discipline_bull",
        "type": "Spring", "conf": 90, "auto_cond_price": 12.0,
        "last": 9.5,
    }
    cond._apply_auto_conditions(st, [cand])
    assert captured["date"].startswith("20")
    assert captured["price"] == pytest.approx(9.5)
