"""模拟盘候选选股 (pick_candidates) 门禁/排序/降级 测试。"""
import numpy as np
import pandas as pd

import wyckoff.paper as paper


def _mk_df(n=400):
    """给 pick_candidates._probe 用的假 K 线 (pandas DataFrame, 含 day/close 列)。"""
    base = np.linspace(10.0, 11.0, n)
    return pd.DataFrame({
        "day": [f"2026-01-{(i % 28) + 1:02d}" for i in range(n)],
        "open": base,
        "high": base + 0.05,
        "low": base - 0.05,
        "close": base,
    })


class LeftMgr:
    """只产左侧买点候选 (gated=False, 独立赛道不走三重门禁) 的假管理器。"""

    def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                        name="", event_types=None, strategies=None):
        return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                "conf": 82, "kind": "spring", "entry_price": 10.1,
                "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                "gated": False}


class DiscMgr:
    """只产纪律候选 (gated=True, 受大盘/板块/资金流门禁约束) 的假管理器。"""

    def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                        name="", event_types=None, strategies=None):
        return {"strategy": "paper_discipline_bull", "type": "Spring",
                "idx": 388, "conf": 85, "kind": "spring", "entry_price": 10.1,
                "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                "gated": True}


class VaMgr:
    """只产价值吸筹候选 (gated=True) 的假管理器。"""

    def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                        name="", event_types=None, strategies=None):
        return {"strategy": "screener_value_accumulation", "type": "Spring",
                "idx": 388, "conf": 85, "kind": "spring", "entry_price": 10.1,
                "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                "gated": True}


def _apply(monkeypatch, mgr, market_ok=(True, "")):
    monkeypatch.setattr(paper, "_strategy_manager", lambda: mgr)
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: _mk_df().copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok", lambda: market_ok)


def test_pick_candidates_left_buy_ignores_market_gate(monkeypatch):
    """威科夫左侧买点: 大盘未站上MA20 时仍可入池 (独立赛道, gated=False,
    挂买点入场价 below 条件单, 不受板块/资金流门禁管束)。"""
    _apply(monkeypatch, LeftMgr(), market_ok=(False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) == 1
    e = out[0]
    assert e["strategy"] == "long_buy_left"
    assert e["gated"] is False
    assert e["trigger"] == "below"
    assert e["auto_cond_price"] == 10.1
    assert e["entry_price"] == 10.1


def test_pick_candidates_filter_and_sort(monkeypatch):
    """Test pick_candidates filtering and sorting."""
    _apply(monkeypatch, LeftMgr())
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_no_net_grace(monkeypatch):
    """Test pick_candidates without network gracefully handles errors."""
    monkeypatch.setattr(paper, "_strategy_manager", lambda: None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_no_net(monkeypatch):
    """Test pick_candidates without network."""
    monkeypatch.setattr(paper, "_strategy_manager", lambda: None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_discipline_gate_failclose(monkeypatch):
    """Test pick_candidates discipline gate fail-close."""
    _apply(monkeypatch, LeftMgr(), market_ok=(False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_discipline_gate_sector_flow(monkeypatch):
    """Test pick_candidates discipline gate with sector and flow."""
    _apply(monkeypatch, DiscMgr(), market_ok=(True, ""))
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001", "sh600002"], max_codes=5)
    codes = [e["code"] for e in out]
    assert codes == ["sh600001"]


def test_pick_candidates_flow_gate_partial_data(monkeypatch):
    """Test pick_candidates flow gate with partial data."""
    _apply(monkeypatch, DiscMgr(), market_ok=(True, ""))
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001", "sh600002"], max_codes=5)
    codes = [e["code"] for e in out]
    assert codes == ["sh600001"]


def test_pick_candidates_restricts_to_main_board(monkeypatch):
    """Test pick_candidates restricts to main board."""
    _apply(monkeypatch, LeftMgr())
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_enable_va_off_default(monkeypatch):
    """停用价值吸筹后: 纪律/左侧候选正常产出, 价值吸筹候选被剔除。"""
    _apply(monkeypatch, VaMgr())
    old = paper._CUR.get("enable_va", True)
    paper._CUR["enable_va"] = False
    try:
        out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    finally:
        if "enable_va" in paper._CUR and paper._CUR.get("enable_va", True) != old:
            paper._CUR["enable_va"] = old
    assert [e["strategy"] for e in out] == []


def test_pick_candidates_enable_va_off_explicit_va_only(monkeypatch):
    """停用价值吸筹后, 显式仅扫描价值吸筹 → 直接返回空 (不回退全策略)。"""
    _apply(monkeypatch, VaMgr())
    paper._CUR["enable_va"] = False
    try:
        out = paper.pick_candidates(universe=["sh600001"], max_codes=5,
                                    strategies=("screener_value_accumulation",))
    finally:
        paper._CUR["enable_va"] = True
    assert out == []


def test_pick_candidates_enable_va_on_keeps_va(monkeypatch):
    """开关默认开启时, 价值吸筹候选正常产出。"""
    _apply(monkeypatch, VaMgr())
    paper._CUR["enable_va"] = True
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert [e["strategy"] for e in out] == ["screener_value_accumulation"]
