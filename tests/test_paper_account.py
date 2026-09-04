"""模拟盘引擎测试 (wyckoff.paper): 保存/选股/撮合/卖出/统计闭环。

全部离线: monkeypatch datasource/indicators/events 注入合成数据与确定性事件,
不访问网络。数据目录由 conftest.py 重定向到临时目录。
"""
import os

import numpy as np
import pandas as pd
import pytest

import wyckoff.paper as paper


def _mk(closes, wob=0.05):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    c = closes + np.sin(np.arange(n) / 9.0) * wob
    return pd.DataFrame({"day": dates, "open": c, "close": c,
                         "high": c + 0.06, "low": c - 0.06,
                         "volume": np.full(n, 8e5)})


def _candidate(code="sh600001", type_="Spring", conf=100, last=10.0, idx=299):
    return {"code": code, "name": "测试股", "type": type_, "conf": conf,
            "last": last, "idx": idx}


@pytest.fixture(autouse=True)
def clean_paper_file():
    """避免测试间共享持久化账户状态 (conftest 只隔离目录, 不隔离单文件)。"""
    if os.path.exists(paper.PAPER_FILE):
        os.remove(paper.PAPER_FILE)
    yield
    if os.path.exists(paper.PAPER_FILE):
        os.remove(paper.PAPER_FILE)


# ───────────────────────── 保存/读取 ─────────────────────────
def test_save_load_roundtrip():
    st = paper._new_state()
    st["cash"] = 42.5
    st["meta"]["v"] = 1
    assert paper.save_state(st)
    got = paper.load_state()
    assert got["cash"] == 42.5
    assert got["meta"]["v"] == 1


def test_load_state_bad_file():
    import os
    if os.path.exists(paper.PAPER_FILE):
        os.remove(paper.PAPER_FILE)
    got = paper.load_state()
    assert got["cash"] == paper.INIT_CASH
    assert got["positions"] == []


# ───────────────────────── 选股筛选 ─────────────────────────
def test_pick_candidates_filter_and_sort(monkeypatch):
    df = _mk(np.linspace(10.0, 10.5, 400))
    # 两只股票: A 有 Spring + LPS(应丢弃，非强多头); B 有 Spring88(整只入池)
    # 注: UTAD(空头)和SC(中性)已从LONG_EVENT_TYPES中移除，仅保留Spring/Shakeout/ST/LPS
    events_a = [
        _candidate(code="sh600001", type_="Spring", conf=95, idx=395),
        _candidate(code="sh600001", type_="LPS", conf=92, idx=200),
    ]
    events_b = [
        _candidate(code="sh600002", type_="Spring", conf=88, idx=390),
    ]

    def fake_detect(dfit, piv):
        sym = dfit["sym"].iloc[-1]
        return events_a if sym == "sh600001" else events_b

    def fake_annotate(df, symbol=None, **k):
        df["sym"] = symbol
        return df

    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators", fake_annotate)
    monkeypatch.setattr("wyckoff.indicators.find_pivots", lambda *a, **k: [])
    monkeypatch.setattr("wyckoff.events.detect_all", fake_detect)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    out = paper.pick_candidates(universe=["sh600001", "sh600002"],
                                max_codes=10, min_conf=85, skip_gates=True)
    # 仅保留多头事件: Spring/LPS/Shakeout/ST
    # A: Spring(95) 最高; LPS(92) 在强梯队内但被保留
    # B: Spring(88) 整只入池
    assert len(out) == 2
    confs = sorted(e["conf"] for e in out)
    assert confs == [88, 95]
    by_code = {e["code"]: e["type"] for e in out}
    assert by_code["sh600001"] == "Spring"
    assert by_code["sh600002"] == "Spring"


def test_pick_candidates_no_net_grace(monkeypatch):
    """universe 拉取失败时走兜底并无人为抛错。"""
    df = _mk(np.linspace(8.0, 9.0, 300))
    monkeypatch.setattr("wyckoff.fundamental.fetch_market_universe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down")))
    monkeypatch.setattr("wyckoff.datasource.fetch_kline", lambda *a, **k: df)
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr("wyckoff.indicators.find_pivots", lambda *a, **k: [])
    monkeypatch.setattr("wyckoff.events.detect_all",
                        lambda *a, **k: [_candidate(idx=295)])
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    out = paper.pick_candidates(max_codes=5, skip_gates=True)
    assert isinstance(out, list)


def test_pick_candidates_discipline_gate_failclose(monkeypatch):
    """大盘20日线向上门禁 fail-close: 大盘未站上 MA20 时整池拦截 (返回空)。"""
    df = _mk(np.linspace(10.0, 10.5, 400))
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.indicators.find_pivots", lambda *a, **k: [])
    monkeypatch.setattr("wyckoff.events.detect_all",
                        lambda *a, **k: [_candidate(idx=395)])
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (False, "大盘未站上MA20"))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert out == []


def test_pick_candidates_discipline_gate_sector_flow(monkeypatch):
    """板块强度>60分位 + 资金流截面中位门禁: 未达标候选被拦截。"""
    df = _mk(np.linspace(10.0, 10.5, 400))
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.indicators.find_pivots", lambda *a, **k: [])
    monkeypatch.setattr("wyckoff.events.detect_all",
                        lambda *a, **k: [_candidate(code="sh600001", idx=395)])
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (True, "大盘站上MA20"))
    # 板块强度不足 → fail-close
    monkeypatch.setattr(paper, "_sector_strength_ok", lambda s: (False, "板块强度40分位"))
    assert paper.pick_candidates(universe=["sh600001"], max_codes=5) == []
    # 板块达标; 全部候选无资金流数据 → 数据缺失降级跳过门禁③ (不误杀)
    monkeypatch.setattr(paper, "_sector_strength_ok",
                        lambda s: (True, "板块强度80分位"))
    monkeypatch.setattr(paper, "_flow_net5", lambda c: None)
    monkeypatch.setattr(paper, "add_condition", lambda *a, **k: (None, ""))
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) == 1
    assert out[0]["code"] == "sh600001"
    # 资金流达标 → 候选入池 (与上同路径, 含数据)
    monkeypatch.setattr(paper, "_flow_net5", lambda c: 1_000_000.0)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5)
    assert len(out) == 1


def test_pick_candidates_flow_gate_partial_data(monkeypatch):
    """资金流门禁③: 部分候选有数据时, 中位过滤仍生效, 无数据者按不达标拦截。"""
    df = _mk(np.linspace(10.0, 10.5, 400))
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: df.copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators",
                        lambda df, **k: df)
    monkeypatch.setattr("wyckoff.indicators.find_pivots", lambda *a, **k: [])
    monkeypatch.setattr("wyckoff.events.detect_all",
                        lambda *a, **k: [_candidate(code="sh600001", idx=395),
                                         _candidate(code="sh600002", idx=395)])
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok",
                        lambda: (True, "大盘站上MA20"))
    monkeypatch.setattr(paper, "_sector_strength_ok",
                        lambda s: (True, "板块强度80分位"))
    monkeypatch.setattr(paper, "add_condition", lambda *a, **k: (None, ""))
    # sh600001 有数据且高于中位, sh600002 无数据
    monkeypatch.setattr(paper, "_flow_net5",
                        lambda c: 1_000_000.0 if c == "sh600001" else None)
    out = paper.pick_candidates(universe=["sh600001", "sh600002"], max_codes=5)
    codes = [e["code"] for e in out]
    assert codes == ["sh600001"]


# ───────────────────────── 订单/撮合 ─────────────────────────
def test_make_order_lot_sizing():
    order = paper._make_order("sh600001", "", "Spring", 90, 10.0, 0,
                              cash=10_000_000)
    assert order["qty"] % 100 == 0
    assert order["price"] == round(10.0 * (1 + paper.SLIP_BUY), 3)
    assert order["side"] == "buy"
    # 金额不足一手 → None
    assert paper._make_order("sh600001", "", "Spring", 90, 10.0, 0,
                             cash=1_000) is None


def test_place_buy_and_equity(monkeypatch):
    monkeypatch.setattr("wyckoff.paper.execute_date", lambda c: "2024-05-01")
    st = paper._new_state()
    order2 = paper._make_order("sh600001", "", "Spring", 95, 10.0, 5, st["cash"])
    paper.fill_buy(st, order2)
    assert len(st["positions"]) == 1
    pos = st["positions"][0]
    assert pos["qty"] > 0 and pos["buy_px"] == order2["price"]
    assert st["cash"] < paper.INIT_CASH
    df = _mk([10.0, 10.2, 10.4])
    last = float(df["close"].iloc[-1])
    eq = paper.equity(st, {"sh600001": df})
    assert eq == pytest.approx(st["cash"] + pos["qty"] * last, rel=1e-9)


def test_step_fills_pending():
    st = paper._new_state()
    o = paper._make_order("sh600002", "", "LPS", 90, 20.0, 0, st["cash"])
    st["pending"].append(o)
    df = _mk([20.0, 20.5, 21.0])
    assert st["positions"] == []
    paper.step(st, {"sh600002": df})
    # 保单按最近收盘+滑点成交
    assert len(st["positions"]) == 1
    assert st["pending"] == []
    last = float(df["close"].iloc[-1])
    assert st["positions"][0]["buy_px"] == round(last * (1 + paper.SLIP_BUY), 3)


# ───────────────────────── 卖出条件 ─────────────────────────
def _state_with_position(cash=paper.INIT_CASH, entry=10.0, bars=5):
    st = paper._new_state()
    st["cash"] = cash
    st["positions"].append({
        "symbol": "sh600001", "name": "测试", "type": "Spring", "conf": 95,
        "qty": 10000, "buy_px": entry, "cost": 40.0,
        "entry_ts": "2024-03-01 10:00:00", "entry_bars": bars, "staged": False,
    })
    return st


def test_step_take_profit_closes(monkeypatch):
    monkeypatch.setattr("wyckoff.paper.execute_date", lambda c: "2024-05-02")
    st = _state_with_position(entry=10.0)
    df = _mk([11.0, 12.0])  # +20% ≥ +15% 止盈
    paper.step(st, {"sh600001": df})
    assert st["positions"] == []
    assert st["closed"][0]["reason"] == "止盈"
    assert st["closed"][0]["ret"] > 0.15
    assert st["cash"] > paper.INIT_CASH


def test_step_stop_loss_closes():
    st = _state_with_position(entry=10.0)
    df = _mk([9.4, 9.2])  # 跌破买入价 -6%, 触发默认止损 (-3%)
    paper.step(st, {"sh600001": df})
    assert st["closed"][0]["reason"] == "止损"
    # 止损限价 = 买入价*(1-默认止损%), 卖出价再扣滑点 (低于止损价成交)
    stop_px = 10.0 * (1 - paper._CUR["stop_loss"])
    assert st["closed"][0]["sell_px"] < stop_px
    assert st["closed"][0]["sell_px"] < 10.0
    assert st["closed"][0]["ret"] < 0


def test_step_expiry_closes():
    st = _state_with_position(entry=10.0, bars=paper.HOLD_BARS)
    df = _mk([10.3, 10.2])  # 未触发止盈/止损/破位
    paper.step(st, {"sh600001": df})
    assert st["closed"][0]["reason"] == "到期"


def test_step_no_close_when_flat():
    st = _state_with_position(entry=10.0)
    df = _mk([10.2, 10.3, 10.1])  # 均在止损/止盈间
    paper.step(st, {"sh600001": df})
    assert st["positions"]  # 未平仓
    assert st["closed"] == []


# ───────────────────────── 收益统计 ─────────────────────────
def _closed(type_, ret, reason):
    return {"symbol": "s", "name": "", "type": type_, "conf": 90,
            "qty": 1000, "buy_px": 10.0, "sell_px": round(10.0 * (1 + ret), 3),
            "ret": round(ret, 4), "reason": reason,
            "entry_ts": "t", "close_ts": "t", "bars": 5}


def test_stats_aggregation():
    st = paper._new_state()
    st["closed"] = [
        _closed("Spring", 0.20, "止盈"),
        _closed("Spring", 0.10, "止盈"),
        _closed("LPS", -0.05, "止损"),
    ]
    s = paper.stats(st)
    assert s["n_closed"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["pl_ratio"] == pytest.approx(0.15 / 0.05, rel=0.1)
    assert s["by_type"]["Spring"]["n"] == 2
    assert s["by_type"]["Spring"]["win"] == pytest.approx(1.0, abs=1e-3)
    assert s["by_reason"]["止盈"]["n"] == 2
    assert s["avg_ret"] == pytest.approx((0.20 + 0.10 - 0.05) / 3, abs=1e-3)


def test_stats_empty():
    s = paper.stats(paper._new_state())
    assert s["win_rate"] is None
    assert s["n_closed"] == 0


# ───────────────────────── 全周期闭环 ─────────────────────────
def test_expiry_after_hold_bars_cycles(monkeypatch):
    """平价横盘下, 经过 HOLD_BARS 个周期后因到期平仓。"""
    import wyckoff.datasource as ds
    monkeypatch.setattr(ds, "fetch_kline",
                        lambda *a, **k: _mk([10.0] * 60))
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr(paper, "pick_candidates",
                        lambda **k: [_candidate(type_="Spring", conf=95)],
                        raising=True)

    while True:
        s = paper.run_cycle()
        if s["n_closed"] == 1:
            break
        assert s["n_positions"] == 1
    st = paper.load_state()
    assert st["closed"][0]["reason"] == "到期"
    assert st["closed"][0]["bars"] == paper.HOLD_BARS


def test_run_cycle_end_to_end(monkeypatch, tmp_path):
    """筛选→下单→持仓→步进→止盈卖出→统计 全链路。

    fetch_kline 按调用次数返回递进行情: 第1次建仓, 第2次(+40%)触发止盈。
    """
    import wyckoff.datasource as ds
    calls = {"n": 0}

    def fake_fetch(code, datalen=None, scale=None):
        calls["n"] += 1
        if calls["n"] <= 1:
            return _mk([10.0, 10.1, 10.2])
        return _mk([12.0, 13.0, 14.0])

    monkeypatch.setattr(ds, "fetch_kline", fake_fetch)
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr(paper, "pick_candidates",
                        lambda **k: [_candidate(type_="Spring", conf=96)])
    # 第一周期: 建仓
    s1 = paper.run_cycle()
    assert s1["n_positions"] == 1
    # 第二周期: 持仓股已 +40%, 触发止盈平仓
    monkeypatch.setattr(paper, "pick_candidates", lambda **k: [])
    s2 = paper.run_cycle()
    assert s2["n_closed"] == 1
    assert s2["n_positions"] == 0
    st = paper.load_state()
    # 建仓后自动生成持仓保护条件单; 条件单优先于默认止盈触发平仓
    assert any(c["kind"] == "take_profit" and c["status"] == "done"
               for c in st["conditions"])
    assert st["closed"][0]["reason"] == "条件单:take_profit"
    assert st["closed"][0]["type"] == "Spring"
    assert st["closed"][0]["conf"] == 96


# ───────────────────────── 条件单 ─────────────────────────
def _state_with_cond(kind, price=None, pct=None, trigger="above",
                     entry=10.0, cash=paper.INIT_CASH):
    st = paper._new_state()
    st["cash"] = cash
    c, msg = paper.add_condition(st, kind, "sh600001", price=price, pct=pct,
                                 trigger=trigger, name="测试", save=False)
    assert c is not None, msg
    # 建仓
    o = paper._make_order("sh600001", "测试", "Spring", 90, entry, 0, st["cash"])
    paper.fill_buy(st, o)
    return st


def test_condition_buy_price_triggers_below():
    """价格买入条件单: 现价回落到触发价下方时买入。"""
    st = paper._new_state()
    st["cash"] = paper.INIT_CASH
    paper.add_condition(st, "buy_price", "sh600001", price=10.0,
                        trigger="below", name="测试", save=False)
    df = _mk([12.0, 9.5])  # 现价 9.5 ≤ 10 触发
    paper.step(st, {"sh600001": df})
    assert len(st["positions"]) == 1
    assert st["conditions"][0]["status"] == "done"
    assert st["conditions"][0]["matched_price"] is not None


def test_condition_sell_price_triggers_above():
    """价格卖出条件单: 现价升破触发价时卖出持仓。"""
    st = _state_with_cond("sell_price", price=13.0, trigger="above", entry=10.0)
    df = _mk([10.0, 13.2])
    paper.step(st, {"sh600001": df})
    assert st["positions"] == []
    assert st["conditions"][0]["status"] == "done"
    assert st["closed"][0]["reason"] == "条件单:sell_price"


def test_condition_take_profit():
    st = _state_with_cond("take_profit", pct=0.12, entry=10.0)
    df = _mk([10.0, 11.5])  # +15% ≥ 12% 止盈
    paper.step(st, {"sh600001": df})
    assert st["positions"] == []
    assert st["conditions"][0]["status"] == "done"
    assert st["closed"][0]["reason"] == "条件单:take_profit"


def test_condition_stop_loss():
    st = _state_with_cond("stop_loss", pct=0.05, entry=10.0)
    df = _mk([10.0, 9.3])  # -7% ≥ 5% 止损
    paper.step(st, {"sh600001": df})
    assert st["positions"] == []
    assert st["conditions"][0]["status"] == "done"


def test_buy_generates_tp_sl_conditions():
    """建仓后自动生成 止盈/止损 条件单, 并消费同标的入场条件单。"""
    st = paper._new_state()
    st["cash"] = paper.INIT_CASH
    paper.add_condition(st, "buy_price", "sh600001", price=10.0,
                        trigger="above", name="测试", save=False)
    o = paper._make_order("sh600001", "测试", "Spring", 90, 10.0, 0, st["cash"])
    paper.fill_buy(st, o)
    kinds = {(c["kind"], c["status"]) for c in st["conditions"]}
    assert ("take_profit", "active") in kinds
    assert ("stop_loss", "active") in kinds
    # buy_price 入场条件已被消费, 不会重复买入
    assert not any(c["kind"] == "buy_price" and c["status"] == "active"
                   for c in st["conditions"])
    # 二次买入同标的: 不重复生成保护条件单
    n_tp = sum(1 for c in st["conditions"] if c["kind"] == "take_profit")
    paper.fill_buy(st, dict(o, symbol="sh600001"))
    assert sum(1 for c in st["conditions"]
               if c["kind"] == "take_profit") == n_tp


def test_condition_trailing_stop():
    st = _state_with_cond("trailing", pct=0.05, entry=10.0)
    paper.step(st, {"sh600001": _mk([10.0, 11.0])})  # 建峰
    assert st["positions"], "首根不应触发"
    peak = st["conditions"][0]["peak"]
    assert peak is not None and peak > 10.0
    # 大幅回撤 (>5%) 触发追踪止损
    paper.step(st, {"sh600001": _mk([11.0, 8.0])})
    assert st["positions"] == []
    assert st["conditions"][0]["status"] == "done"


def test_condition_not_triggered_when_flat():
    st = paper._new_state()
    st["cash"] = paper.INIT_CASH
    paper.add_condition(st, "buy_price", "sh600001", price=8.0,
                        trigger="below", name="测试", save=False)
    df = _mk([10.2, 10.3])  # 现价高于触发价, 未触发
    paper.step(st, {"sh600001": df})
    assert st["conditions"][0]["status"] == "active"
    assert st["positions"] == []


def test_condition_cancel():
    st = paper._new_state()
    c, _ = paper.add_condition(st, "buy_price", "sh600001", price=9.0,
                               save=False)
    assert paper.cancel_condition(st, c["cid"])
    assert st["conditions"][0]["status"] == "cancelled"


def test_force_close_position_manual():
    """手动平仓: 按代码平仓, 回收现金并发平仓记录; 不存在返回 None。"""
    st = paper._new_state()
    st["cash"] = 100_000
    pos = {"symbol": "sh600519", "name": "贵州茅台", "type": "Spring",
           "strategy": "paper_discipline_bull", "conf": 95, "qty": 100,
           "buy_px": 1200.0, "last": 1300.0, "entry_bars": 5,
           "entry_ts": "2026-01-01 10:00:00"}
    st["positions"].append(pos)
    cash_before = st["cash"]
    got = paper.force_close_position(st, "sh600519", "手动平仓")
    assert got is pos
    assert len(st["positions"]) == 0
    assert len(st["closed"]) == 1
    assert st["closed"][0]["symbol"] == "sh600519"
    assert st["closed"][0]["reason"] == "手动平仓"
    assert st["cash"] > cash_before
    # 不存在的代码返回 None
    assert paper.force_close_position(st, "sh999999") is None


# ───────────────────────── 优化: 条件单去重 ─────────────────────────
def test_apply_auto_conditions_upsert_dedup():
    """覆盖式去重: 同代码已有 active buy_price 时更新触发价而非堆积重复。"""
    st = paper._new_state()
    c1, _ = paper.add_condition(st, "buy_price", "sh600001", price=10.0,
                                save=False)
    cand = [{"code": "sh600001", "name": "A", "strategy": "paper_discipline_bull",
             "type": "Spring", "conf": 95, "auto_cond_price": 11.0}]
    n = paper._apply_auto_conditions(st, cand)
    assert n == 1  # 更新计为一次
    active = [c for c in st["conditions"] if c.get("status") == "active"]
    assert len(active) == 1  # 不再堆积重复
    assert active[0]["price"] == 11.0
    assert active[0]["cid"] == c1["cid"]  # 复用原条件


def test_apply_auto_conditions_global_dedup():
    """全局预清理: 历史遗留下多条同代码 active 条件被合流为一条。"""
    st = paper._new_state()
    for px in (10.0, 10.1, 10.2):
        paper.add_condition(st, "buy_price", "sh600001", price=px, save=False)
    paper._apply_auto_conditions(st, [])  # 空候选也触发全局清理
    active = [c for c in st["conditions"] if c.get("status") == "active"]
    assert len(active) == 1


# ───────────────────────── 优化: 低质池过滤 ─────────────────────────
def test_is_low_quality_filter():
    assert paper._is_low_quality("bj920056")          # 北交所
    assert paper._is_low_quality("sh600001", name="ST怪股")
    assert paper._is_low_quality("sh600001", name="*ST退")
    assert paper._is_low_quality("sh600001", price=2.0)  # 低价
    assert not paper._is_low_quality("sh600001", name="贵州茅台", price=100.0)


def test_value_accum_excludes_bj_and_st(monkeypatch):
    """价值吸筹: 北交所 / ST 被过滤, 正常主板保留。"""
    class FakeMgr:
        def __init__(self, t):
            self.t = t
        def evaluate_strategy_value_accumulation(self, *a, **k):
            return {"event": {"type": "Spring", "idx": 100, "conf": 90}}

    evs = []
    piv = []
    df = _mk(np.linspace(10.0, 11.0, 250))
    monkeypatch.setattr(paper, "_strategy_manager",
                        lambda: FakeMgr("Spring"))
    # 北交所被拒
    assert paper._value_accum_candidate("bj920056", df, piv, evs) is None
    # 正常主板保留 (需 ST 名检查来自 _stock_name 空 → 非 ST)
    monkeypatch.setattr(paper, "_stock_name", lambda c: "正常股")
    got = paper._value_accum_candidate("sh600001", df, piv, evs)
    assert got is not None and got["strategy"] == "screener_value_accumulation"
    # ST 被拒
    monkeypatch.setattr(paper, "_stock_name", lambda c: "ST怪异")
    assert paper._value_accum_candidate("sh600001", df, piv, evs) is None


# ───────────────────────── 优化: 等权仓位 ─────────────────────────
def test_make_order_equal_weight_equity():
    """传入 st 时按账户总权益等权, 修正首批买入吞现金导致权重失衡。"""
    cash = 400_000.0
    st = paper._new_state()
    st["cash"] = cash
    st["positions"].append({
        "symbol": "sh600001", "name": "A", "type": "Spring", "conf": 95,
        "qty": 20000, "buy_px": 10.0, "cost": 1.0, "entry_ts": "t",
        "entry_bars": 1, "staged": False, "last": 10.0,
    })
    order = paper._make_order("sh600002", "", "Spring", 90, 10.0, 0,
                              st["cash"], st=st)
    assert order["qty"] % 100 == 0
    # 总权益 = 400000 + 20000*10 = 600000; 等权 /max_pos(3) = 200000 → 约 20000 股
    assert order["qty"] >= 19000
    assert order["qty"] <= 21000


# ───────────────────────── 优化: 追踪止损 ─────────────────────────
def test_step_trailing_stop_protects_gains(monkeypatch):
    """追踪止损: 持仓从高点回落 stop_loss 幅度时平仓, 而非回到买入价才止。"""
    monkeypatch.setattr(paper, "_CUR", {
        **paper._CUR,
        "trailing_stop": True, "trail_atr_mult": 0.0,
        "stop_loss": 0.05, "take_profit": 0.15, "hold_bars": paper.HOLD_BARS,
    })
    st = paper._new_state()
    st["positions"].append({
        "symbol": "sh600001", "name": "测试", "type": "Spring", "conf": 95,
        "qty": 10000, "buy_px": 10.0, "cost": 40.0,
        "entry_ts": "2024-03-01 10:00:00", "entry_bars": 1, "staged": False,
    })
    # 先冲高到 ~11 (+10%, 峰值≈11, 未及 +15% 止盈), 再回落到 ~10.2
    # (距峰值约 -7% > 追踪 5%) → 在明显高于买入价处触发追踪止损。
    paper.step(st, {"sh600001": _mk([10.6, 11.0])})
    assert st["positions"], "冲高阶段不应平仓"
    paper.step(st, {"sh600001": _mk([10.5, 10.2])})
    assert st["positions"] == []
    assert st["closed"][0]["reason"] == "止损"
    # 追踪止损触发时按市价(现价 last)成交: 收益仍为正值 (10.19 > 买入 10),
    # 但远优于回到买入价才止 (否则会拿到 0% 或负收益)。
    assert st["closed"][0]["sell_px"] >= 10.0
    assert st["closed"][0]["ret"] > 0


def test_condition_invalid_kind():
    st = paper._new_state()
    c, msg = paper.add_condition(st, "not_a_kind", "sh600001", price=9.0,
                                 save=False)
    assert c is None
    assert "不支持" in msg


# ───────────────────────── 净收益口径 (含费用) ─────────────────────────
def test_float_ret_is_net_of_fees():
    # 平价浮亏: 即便现价=成本, 扣双边费用后净收益为负
    ret = paper.float_ret(10.0, 10.0)
    cost_rate = 1.0 + paper._CUR["cost"]
    deriv = (10.0 * (1 - paper.SLIP_SELL) * (1 - paper._CUR["cost"])
             / (10.0 * cost_rate) - 1)
    assert ret == pytest.approx(deriv, abs=1e-12)
    assert ret < 0
    # 现价涨过成本价时, 净收益应为正且小于毛收益率
    gross = 10.5 / 10.0 - 1
    assert paper.float_ret(10.0, 10.5) < gross


def test_float_ret_zero_guard():
    assert paper.float_ret(0.0, 10.0) == 0.0


def test_close_position_ret_is_net(monkeypatch):
    monkeypatch.setattr("wyckoff.paper.execute_date", lambda c: "2024-05-01")
    st = paper._new_state()
    order = paper._make_order("sh600001", "", "Spring", 95, 10.0, 5,
                              st["cash"])
    paper.fill_buy(st, order)
    pos = st["positions"][0]
    outlay = pos["buy_px"] * pos["qty"] * paper.net_cost_rate()
    paper.close_position(st, pos, 10.0, "止盈")
    proceeds = 10.0 * pos["qty"] * (1 - paper._CUR["cost"])
    expect = round((proceeds - outlay) / outlay, 4)
    assert st["closed"][0]["ret"] == pytest.approx(expect, abs=1e-9)


def test_stats_equity_includes_positions_mark_to_market():
    st = paper._new_state()
    order = paper._make_order("sh600001", "", "Spring", 95, 10.0, 5,
                              st["cash"])
    paper.fill_buy(st, order)
    pos = st["positions"][0]
    pos["last"] = 11.0  # 模拟现价刷新
    s = paper.stats(st)
    assert s["equity"] == pytest.approx(round(st["cash"] + pos["qty"] * 11.0, 2),
                                        abs=1e-9)
    init = paper._CUR["init_cash"]
    assert s["total_return"] == pytest.approx(round(s["equity"] / init - 1, 4),
                                              abs=1e-9)


# ───────────────────────── 优化: 交易安全/一致性 ─────────────────────────
def test_condition_buy_skips_when_already_held():
    """已持有标的的 buy_price 条件单触发时不重复建仓, 直接取消。"""
    st = paper._new_state()
    st["cash"] = 1_000_000
    # 已持仓
    st["positions"].append({
        "symbol": "sh600001", "name": "测试", "type": "Spring", "conf": 90,
        "qty": 1000, "buy_px": 10.0, "cost": 40.0,
        "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
    })
    c, _ = paper.add_condition(st, "buy_price", "sh600001", price=9.0,
                                trigger="above", save=False)
    n_before = len(st["positions"])
    n = paper._check_conditions(st, {"sh600001": _mk([9.0, 9.0])})
    # 触发但已持有: 条件单取消, 不新增仓位
    assert st["conditions"][0]["status"] == "cancelled"
    assert len(st["positions"]) == n_before
    assert n == 0


def test_condition_buy_no_duplicate_when_fire_held():
    """_fire_condition 直连时对已持有标的取消而非再买入 (实盘 sz000550 场景)。"""
    st = paper._new_state()
    st["cash"] = 1_000_000
    st["positions"].append({
        "symbol": "sz000550", "name": "江铃汽车", "type": "Spring", "conf": 90,
        "qty": 1000, "buy_px": 16.6, "cost": 60.0,
        "entry_ts": "2024-01-02 10:00:00", "entry_bars": 2, "staged": False,
    })
    c, _ = paper.add_condition(st, "buy_price", "sz000550", price=16.924,
                                trigger="above", save=False)
    n_before = len(st["positions"])
    paper._fire_condition(st, c, 17.0, _mk([16.8, 17.0]), side="buy")
    assert st["conditions"][0]["status"] == "cancelled"
    assert len(st["positions"]) == n_before  # 未重复建仓


def test_fill_buy_rejects_when_insufficient_cash(monkeypatch):
    """现金不足时 fill_buy 不成交、不清为负余额。"""
    st = paper._new_state()
    st["cash"] = 1000.0  # 远小于 1 手市值
    order = {
        "symbol": "sh600001", "name": "测试", "type": "Spring", "conf": 90,
        "qty": 100, "price": 50.0, "ts": "2024-01-02 10:00:00", "bars": 0,
    }
    res, msg = paper.fill_buy(st, order)
    assert res is None
    assert "现金" in msg
    assert len(st["positions"]) == 0
    assert st["cash"] == 1000.0  # 未扣款


def test_run_cycle_backfills_position_protection():
    """_backfill_position_protection 对缺失止盈/止损条件单的持仓自动补齐。"""
    st = paper._new_state()
    st["positions"].append({
        "symbol": "sh600001", "name": "测试", "type": "Spring", "conf": 90,
        "qty": 1000, "buy_px": 10.0, "cost": 40.0,
        "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
        "strategy": "paper_discipline_bull",
    })
    assert paper._backfill_position_protection(st) == 1
    protect = [c for c in st["conditions"]
               if c.get("status") == "active"
               and c.get("kind") in ("take_profit", "stop_loss", "trailing")
               and c.get("symbol") == "sh600001"]
    assert len(protect) >= 2  # 至少补上止盈+止损
    # 幂等: 再次调用不再重复堆积
    assert paper._backfill_position_protection(st) == 0
    assert len([c for c in st["conditions"]
                if c.get("status") == "active"]) == len(protect)


# ───────────────────── 策略核心优化: 等权口径/再平衡/候选分层 ─────────────────────
def test_condition_buy_uses_equity_weight():
    """条件单买入统一权益/3 等权口径: 不再按剩余现金/3 导致顺序衰减。"""
    st = paper._new_state()
    # 已有重仓 (市值 40万), 现金 30万 → 权益 70万
    st["cash"] = 300_000.0
    st["positions"].append({
        "symbol": "sh600001", "name": "A", "type": "Spring", "conf": 95,
        "qty": 40000, "buy_px": 10.0, "last": 10.0, "cost": 1600.0,
        "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
        "strategy": "paper_discipline_bull",
    })
    c, _ = paper.add_condition(st, "buy_price", "sh600002", price=10.0,
                               trigger="above", save=False)
    paper._check_conditions(st, {"sh600002": _mk([10.0, 10.2])})
    assert st["conditions"][0]["status"] == "done"
    assert len(st["positions"]) == 2
    # 权益/3 口径: 权益=70万, 目标 23.3万/仓; 现金/3 口径则仅 10万
    new_pos = st["positions"][-1]
    new_mv = new_pos["qty"] * new_pos["buy_px"]
    assert new_mv > 200_000  # 权益/3 (23.3万) 明显大于现金/3 (10万)


def test_rebalance_topup_low_weight_position(monkeypatch):
    """满仓且现金富余时, 把权重过低的持仓补足到等权目标。"""
    monkeypatch.setattr(paper, "_CUR", {
        **paper._CUR, "max_pos": 3, "rebalance": True,
    })
    st = paper._new_state()
    st["cash"] = 400_000.0
    # 满3仓, 但其中一只占比过低 (模拟顺序衰减后的失衡)
    st["positions"] = [
        {"symbol": "sh600001", "name": "A", "type": "Spring", "conf": 95,
         "qty": 2500, "buy_px": 10.0, "last": 12.0, "cost": 100.0,
         "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
         "strategy": "paper_discipline_bull"},
        {"symbol": "sh600002", "name": "B", "type": "Spring", "conf": 95,
         "qty": 3000, "buy_px": 10.0, "last": 11.0, "cost": 120.0,
         "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
         "strategy": "paper_discipline_bull"},
        {"symbol": "sh600003", "name": "C", "type": "Spring", "conf": 95,
         "qty": 500, "buy_px": 10.0, "last": 10.0, "cost": 20.0,  # 权重明显过低
         "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
         "strategy": "paper_discipline_bull"},
    ]
    df_by_code = {p["symbol"]: _mk([10.0, 11.0]) for p in st["positions"]}
    cash_before = st["cash"]
    n = paper._rebalance_portfolio(st, df_by_code)
    assert n >= 1  # 低权重仓被补
    # 权重过低的 C 被加仓
    c_pos = next(p for p in st["positions"] if p["symbol"] == "sh600003")
    assert c_pos["qty"] > 500
    assert st["cash"] < cash_before  # 用了现金补仓


def test_rebalance_skips_when_not_full(monkeypatch):
    """未满仓时不触发再平衡 (交给建仓路径即可)。"""
    monkeypatch.setattr(paper, "_CUR", {
        **paper._CUR, "max_pos": 3, "rebalance": True,
    })
    st = paper._new_state()
    st["cash"] = 500_000.0
    st["positions"] = [{
        "symbol": "sh600001", "name": "A", "type": "Spring", "conf": 95,
        "qty": 1000, "buy_px": 10.0, "last": 10.0, "cost": 40.0,
        "entry_ts": "2024-01-02 10:00:00", "entry_bars": 3, "staged": False,
        "strategy": "paper_discipline_bull",
    }]
    assert paper._rebalance_portfolio(st, {}) == 0


def test_value_accum_conf_floor():
    """价值吸筹候选需满足 VA_MIN_CONF 下限, 低 conf 不入选。"""
    # 通过 monkeypatch 供给一个低 conf 的价值吸筹信号
    class FakeMgr:
        def evaluate_strategy_value_accumulation(self, *a, **k):
            return {"event": {"type": "Spring", "conf": 60, "code": "sh600001"}}
    paper._strategy_manager = lambda: FakeMgr()
    try:
        assert paper._value_accum_candidate("sh600001", None, None, None) is None
    finally:
        pass
