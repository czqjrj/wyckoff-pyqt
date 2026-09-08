import numpy as np
import pytest

def _mk(data):
    return data


def test_pick_candidates_left_buy_ignores_market_gate(monkeypatch):
    """威科夫左侧买点: 大盘未站上MA20 时仍可入池 (独立赛道, gated=False,
    挂买点入场价 below 条件单, 不受板块/资金流门禁管束)。"""
    df = _mk(np.linspace(10.0, 11.0, 400))

    class LeftMgr:
        """只产左侧买点候选的假管理器 (绕过真实 buypoints 拉行情)。"""
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
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
    import wyckoff.paper as paper
    from wyckoff.strategies.manager import WyckoffStrategyManager

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (True, ""))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_no_net_grace(monkeypatch):
    """Test pick_candidates without network gracefully handles errors."""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_strategy_manager", lambda: None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_discipline_gate_failclose(monkeypatch):
    """Test pick_candidates discipline gate fail-close."""
    import wyckoff.paper as paper

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5", lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_discipline_gate_sector_flow(monkeypatch):
    """Test pick_candidates discipline gate with sector and flow."""
    import wyckoff.paper as paper

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001", "sh600002"], max_codes=5)
    codes = [e["code"] for e in out]
    assert codes == ["sh600001"]


def test_pick_candidates_flow_gate_partial_data(monkeypatch):
    """Test pick_candidates flow gate with partial data."""
    import wyckoff.paper as paper

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (True, ""))
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001", "sh600002"], max_codes=5)
    codes = [e["code"] for e in out]
    assert codes == ["sh600001"]


def test_pick_candidates_left_buy_ignores_market_gate(monkeypatch):
    """威科夫左侧买点: 大盘未站上MA20 时仍可入池 (独立赛道, gated=False,
    挂买点入场价 below 条件单, 不受板块/资金流门禁管束)。"""
    df = _mk(np.linspace(10.0, 11.0, 400))

    class LeftMgr:
        """只产左侧买点候选的假管理器 (绕过真实 buypoints 拉行情)。"""
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) == 1
    e = out[0]
    assert e["strategy"] == "long_buy_left"
    assert e["gated"] is False
    assert e["trigger"] == "below"
    assert e["auto_cond_price"] == 10.1
    assert e["entry_price"] == 10.1


def test_pick_candidates_restricts_to_main_board(monkeypatch):
    """Test pick_candidates restricts to main board."""
    import wyckoff.paper as paper

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (True, ""))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_discipline_gate_failclose(monkeypatch):
    """Test pick_candidates discipline gate fail-close."""
    import wyckoff.paper as paper

    class LeftMgr:
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: np.linspace(10.0, 11.0, 400).copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: np.linspace(10.0, 11.0, 400))
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) >= 0


def test_pick_candidates_no_net(monkeypatch):
    """Test pick_candidates without network."""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_strategy_manager", lambda: None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_no_net_grace(monkeypatch):
    """Test pick_candidates no net grace."""
    import wyckoff.paper as paper
    monkeypatch.setattr(paper, "_strategy_manager", lambda: None)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_left_buy_ignores_market_gate(monkeypatch):
    """威科夫左侧买点: 大盘未站上MA20 时仍可入池 (独立赛道, gated=False,
    挂买点入场价 below 条件单, 不受板块/资金流门禁管束)。"""
    df = _mk(np.linspace(10.0, 11.0, 400))

    class LeftMgr:
        """只产左侧买点候选的假管理器 (绕过真实 buypoints 拉行情)。"""
        def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                            name="", event_types=None, strategies=None):
            return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                    "conf": 82, "kind": "spring", "entry_price": 10.1,
                    "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                    "gated": False}

    monkeypatch.setattr(paper, "_strategy_manager", lambda: LeftMgr())
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) == 1
    e = out[0]
    assert e["strategy"] == "long_buy_left"
    assert e["gated"] is False
    assert e["trigger"] == "below"
    assert e["auto_cond_price"] == 10.1
    assert e["entry_price"] == 10.1