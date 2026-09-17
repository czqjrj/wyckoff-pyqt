"""QLib 卖出否决 (试点) 测试: 主动卖出 (止盈/到期/追踪) 幂等否决, 止损永不否决。"""
import numpy as np
import pandas as pd

import wyckoff.paper as paper


def _mk_st():
    """最小化持仓台账 (同 test_paper_account._mk_st)。"""
    return {
        "init_cash": 1_000_000.0, "cash": 900_000.0,
        "positions": [
            {"symbol": "sh600001", "name": "A", "type": "Spring", "conf": 90,
             "qty": 1000, "buy_px": 10.0, "cost": 40.0,
             "entry_ts": "2026-09-01 10:00:00", "entry_bars": 1,
             "sector": "", "strategy": "paper_discipline_bull",
             "stop_pct": None, "take_pct": None, "staged": False,
             "event_type": None, "entry_day": "2026-09-01"},
        ],
        "orders": [], "closed": [], "candidates": [], "pending": [],
        "conditions": [], "equity_hist": [], "scan_count": 0,
    }


def _mk_df(close=12.0):
    """样本 K 线 (末点 close 为给定价); 买价 10 → close=13.5 为 +35%。"""
    n = 400
    base = np.linspace(10.0, close, n)
    df = pd.DataFrame({
        "day": [f"2026-09-{(i % 28) + 1:02d}" for i in range(n)],
        "open": base, "high": base + 0.05, "low": base - 0.05, "close": base,
    })
    df.loc[df.index[-1], "day"] = "2026-09-02"
    return df


def _enable_veto(monkeypatch, prob=(True, 0.85)):
    """开启试点开关并把模型探测钉死为给定 (veto, prob)。"""
    paper._CUR["qlib_veto"] = True
    paper._CUR["qlib_veto_hi"] = 0.60
    # 关闭移动止盈模式: 否则止盈判定交给 trailing 条件单, 结构卖出分支不走
    paper._CUR["trailing_stop"] = False
    monkeypatch.setattr("wyckoff.qlib_adapter.qlib_probe_live",
                        lambda symbol, veto_hi=0.60, **k: prob)


def test_qlib_veto_exit_off_by_default():
    """开关默认关闭: 不查模型, 直接返回不否决。"""
    st = _mk_st()
    pos = st["positions"][0]
    assert paper._qlib_veto_exit(st, pos) is False
    assert pos.get("qlib_veto_used") is None
    assert not (st.get("meta") or {}).get("qlib_veto_hits")


def test_qlib_veto_exit_vetoes_once_then_marks(monkeypatch):
    """强看多 (≥0.85) → 否决一次并打标记; 第二次调用不再否决 (防踏空防黑天鹅)。"""
    _enable_veto(monkeypatch, prob=(True, 0.85))
    st = _mk_st()
    pos = st["positions"][0]
    assert paper._qlib_veto_exit(st, pos) is True
    assert pos["qlib_veto_used"] is True
    assert st["meta"]["qlib_veto_hits"][0]["prob_buy"] == 0.85
    assert paper._qlib_veto_exit(st, pos) is False


def test_qlib_veto_exit_weak_no_veto(monkeypatch):
    """模型探测到但概率不足 → 不否决, 不打标记。"""
    _enable_veto(monkeypatch, prob=(False, 0.50))
    st = _mk_st()
    pos = st["positions"][0]
    assert paper._qlib_veto_exit(st, pos) is False
    assert pos.get("qlib_veto_used") is None


def test_qlib_veto_exit_fail_open_on_error(monkeypatch):
    """模型异常/无模型 → fail-open 不否决 (不因试点功能阻塞卖出)。"""
    paper._CUR["qlib_veto"] = True
    monkeypatch.setattr("wyckoff.qlib_adapter.qlib_probe_live",
                        lambda symbol, veto_hi=0.60, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    st = _mk_st()
    assert paper._qlib_veto_exit(st, st["positions"][0]) is False
    assert not (st.get("meta") or {}).get("qlib_veto_hits")


def test_step_vetoes_take_profit_but_not_stop(monkeypatch):
    """step: 止盈被否决 (不平仓, meta 记一否); 止损属风险保护永不被否决。"""
    _enable_veto(monkeypatch, prob=(True, 0.85))
    st = _mk_st()
    paper.step(st, {"sh600001": _mk_df(close=13.5)}, trading=True)
    assert len(st["closed"]) == 0
    assert len(st["positions"]) == 1
    assert st["positions"][0].get("qlib_veto_used") is True
    # 止损路径: 现价 8.0 (-20% << -4%) → 直接平仓, 不查否决
    st2 = _mk_st()
    df = _mk_df(close=8.0)
    paper.step(st2, {"sh600001": df}, trading=True)
    assert len(st2["closed"]) == 1
    assert st2["closed"][0]["reason"] == "止损"
    assert not (st2.get("meta") or {}).get("qlib_veto_hits")


def test_conditions_take_profit_vetoed_trailing_vetoed_stop_not(monkeypatch):
    """条件单路径: 止盈/移动止盈否决一次; 止损条件单不否决。"""
    paper._CUR["qlib_veto"] = True
    paper._CUR["qlib_veto_hi"] = 0.60
    monkeypatch.setattr("wyckoff.qlib_adapter.qlib_probe_live",
                        lambda symbol, veto_hi=0.60, **k: (True, 0.85))
    # 止盈条件单: 现价 +25% ≥ pct 20% → 被否决
    st = _mk_st()
    c = {"cid": "c1", "kind": "take_profit", "symbol": "sh600001", "pct": 0.20,
         "trigger": "above", "status": "active"}
    st["conditions"] = [dict(c)]
    paper._check_conditions(st, {"sh600001": _mk_df(close=12.5)})
    assert len(st["closed"]) == 0
    assert st["positions"][0].get("qlib_veto_used") is True
    # 同一持仓下次触发通过 (标记已打)
    paper._check_conditions(st, {"sh600001": _mk_df(close=12.5)})
    assert len(st["closed"]) == 1
    assert st["closed"][0]["reason"] == "条件单:take_profit"
    # 止损条件单: -20% ≤ -10% → 永不否决
    st2 = _mk_st()
    c2 = {"cid": "c2", "kind": "stop_loss", "symbol": "sh600001", "pct": 0.10,
          "trigger": "above", "status": "active"}
    st2["conditions"] = [dict(c2)]
    paper._check_conditions(st2, {"sh600001": _mk_df(close=8.0)})
    assert len(st2["closed"]) == 1
    assert st2["closed"][0]["reason"] == "条件单:stop_loss"
    assert not (st2.get("meta") or {}).get("qlib_veto_hits")
