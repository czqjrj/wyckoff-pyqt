"""方向化期望融合 (任务①) 测试: apply_edge_adjust / sort_candidates。"""
import wyckoff.events as events
import wyckoff.paper as paper


def _monkey(monkeypatch, recs, baseline=0.05):
    """钉死胜率表记录与基线, 使期望/排序完全确定 (不依赖真实缓存)。"""
    monkeypatch.setattr(events, "_winrate_rec",
                        lambda type_, kind="event": recs.get(str(type_)))
    monkeypatch.setattr(events, "_edge_baseline", lambda: baseline)


def _cand(type_, conf=90, kind="event"):
    return {"type": type_, "conf": conf, "kind": kind}


def test_sort_high_edge_first(monkeypatch):
    """方向化均值期望高于基线 → edge_conf 上升; 排序取其降序。"""
    recs = {"Spring": {"dir_mean": 0.10, "n": 300},
            "SOS": {"dir_mean": 0.03, "n": 300}}
    _monkey(monkeypatch, recs, baseline=0.05)
    out = events.sort_candidates([_cand("SOS"), _cand("Spring")])
    assert out[0]["type"] == "Spring"
    assert out[0]["edge_conf"] == 98   # 90 + clamp((0.10-0.05)*300=15 → 8)
    assert out[1]["type"] == "SOS"
    assert out[1]["edge"] == 0.03


def test_negative_edge_pushed_back(monkeypatch):
    """期望为负 (低于基线) → edge_conf 被压低, 排序靠后。"""
    recs = {"JOC": {"dir_mean": -0.02, "n": 300},
            "SC": {"dir_mean": 0.09, "n": 300}}
    _monkey(monkeypatch, recs, baseline=0.05)
    out = events.sort_candidates([_cand("JOC"), _cand("SC")])
    assert out[0]["type"] == "SC"
    assert out[0]["edge_conf"] == 98   # 90 + clamp((0.09-0.05)*300=12 → 8)
    assert out[1]["type"] == "JOC"
    assert out[1]["edge_conf"] == 82   # 90 - clamp((-0.02-0.05)*300=-21 → 8)


def test_no_sample_falls_back_to_conf(monkeypatch):
    """无期望样本 -> edge=0, edge_conf=conf, 排序退化为原 conf (旧行为)。"""
    _monkey(monkeypatch, {}, baseline=0.05)
    out = events.sort_candidates([_cand("Spring", conf=95),
                                  _cand("SOS", conf=88)])
    assert set(o["type"] for o in out) == {"Spring", "SOS"}
    for e in out:
        assert e["edge"] == 0.0
        assert e["edge_conf"] == e["conf"]


def test_edge_conservative_never_leaps_above_100(monkeypatch):
    """edge_conf 钳制在 [1,100], 高期望不被推到 100 以上。"""
    recs = {"SP": {"dir_mean": 0.30, "n": 300}}
    _monkey(monkeypatch, recs, baseline=0.05)
    out = events.sort_candidates([_cand("SP", conf=99)])
    assert out[0]["edge_conf"] == 100


def test_pick_candidates_uses_edge_order(monkeypatch):
    """模拟盘选股入口走 sort_candidates: 输出带 edge/edge_conf 且按它排序。"""
    recs = {"Spring": {"dir_mean": 0.10, "n": 300},
            "SOS": {"dir_mean": 0.03, "n": 300}}
    _monkey(monkeypatch, recs, baseline=0.05)

    class Mgr:
        def scan_individual(self, *a, **k):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 1,
                    "conf": 90, "kind": "spring", "entry_price": 10.0,
                    "stop_price": 9.4, "target_price": 13.0, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: Mgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: _df())
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok", lambda: (True, ""))
    paper._CUR["enable_long_left"] = True
    paper._CUR["enable_va"] = False
    try:
        out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    finally:
        paper._CUR["enable_long_left"] = False
    assert out
    assert all("edge_conf" in e for e in out)
    confs = [int(e["edge_conf"]) for e in out]
    assert confs == sorted(confs, reverse=True)


import numpy as np
import pandas as pd


def _df(n=400):
    base = np.linspace(10.0, 11.0, n)
    return pd.DataFrame({
        "day": [f"2026-01-{(i % 28) + 1:02d}" for i in range(n)],
        "open": base, "high": base + 0.05, "low": base - 0.05, "close": base})
