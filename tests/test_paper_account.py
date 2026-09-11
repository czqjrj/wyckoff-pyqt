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


# ── 交易时段撮合门禁 ──────────────────────────────────────

def _mk_st():
    """最小化持仓台账, 供 step 门禁测试。"""
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


def test_step_trading_true_sells_below_stop(monkeypatch):
    """交易时段内 step: 跌破止损 → 正常平仓 (旧行为保留)。"""
    st = _mk_st()
    df = _mk_df()
    df.loc[df.index[-1], "close"] = 8.0  # -20% << -4% 止损
    df.loc[df.index[-1], "day"] = "2026-09-02"
    paper.step(st, {"sh600001": df}, trading=True)
    assert len(st["closed"]) == 1
    assert st["closed"][0]["reason"] == "止损"


def test_step_snapshot_mode_freezes_sells(monkeypatch):
    """非交易时段 (trading=False): 只更新最新价, 不触发止损平仓。"""
    st = _mk_st()
    df = _mk_df()
    df.loc[df.index[-1], "close"] = 8.0
    df.loc[df.index[-1], "day"] = "2026-09-02"
    paper.step(st, {"sh600001": df}, trading=False)
    assert len(st["closed"]) == 0          # 不卖
    assert len(st["positions"]) == 1
    assert st["positions"][0]["last"] == 8.0  # 但 mark-to-market


def test_run_scan_gated_off_hours(monkeypatch):
    """非交易时段 run_scan 直接返回门禁提示, 不动状态。"""
    monkeypatch.setattr(paper, "gate_reason", lambda anytime=False:
                        "非交易时段 17:00 (A股 09:30-11:30 / 13:00-15:00)")
    st = {"scan_count": 3, "candidates": []}
    res = paper.run_scan(st, scan_type="")
    assert res.startswith("跳过扫描")
    assert st["scan_count"] == 3           # 未自增
    assert st["candidates"] == []


def test_run_scan_anytime_bypasses_gate(monkeypatch):
    """anytime=True 强制放行扫描 (供手动补跑)。"""
    monkeypatch.setattr(paper, "gate_reason", lambda anytime=False:
                        None if anytime else "非交易时段")
    monkeypatch.setattr(paper, "_mainboard_universe", lambda n: ["sh600001"])
    monkeypatch.setattr(paper, "pick_candidates",
                        lambda *a, **k: [{"code": "sh600001", "conf": 90}])
    monkeypatch.setattr(paper, "_apply_auto_conditions", lambda *a, **k: 0)
    monkeypatch.setattr(paper, "save_state", lambda s: s)
    st = {"scan_count": 0, "candidates": [], "conditions": [], "positions": []}
    res = paper.run_scan(st, scan_type="", anytime=True)
    assert "命中 1 个候选" in res
    assert st["scan_count"] == 1


def test_run_scan_passes_weak_when_weak_market(monkeypatch):
    """弱市时 run_scan 必须把 weak=True 传给 _apply_auto_conditions,
    否则价值吸筹候选在弱市仍会生成入场条件单 (曾漏传被默认为 False)。"""
    monkeypatch.setattr(paper, "gate_reason", lambda anytime=False: None)
    monkeypatch.setattr(paper, "_mainboard_universe", lambda n: ["sh600001"])
    monkeypatch.setattr(
        paper, "pick_candidates",
        lambda *a, **k: [{"code": "sh600001", "conf": 90,
                          "strategy": "screener_value_accumulation"}])
    monkeypatch.setattr(paper, "_weak_market_flag", lambda: True)
    seen = {}
    def fake_apply(st, cand, weak=False):
        seen["weak"] = weak
        return 0
    monkeypatch.setattr(paper, "_apply_auto_conditions", fake_apply)
    monkeypatch.setattr(paper, "save_state", lambda s: s)
    st = {"scan_count": 0, "candidates": [], "conditions": [], "positions": []}
    paper.run_scan(st, scan_type="", anytime=True)
    assert seen.get("weak") is True


def test_run_scan_passes_weak_when_not_weak(monkeypatch):
    """非弱市时 run_scan 传 weak=False (与 run_cycle 默认同口径)。"""
    monkeypatch.setattr(paper, "gate_reason", lambda anytime=False: None)
    monkeypatch.setattr(paper, "_mainboard_universe", lambda n: ["sh600001"])
    monkeypatch.setattr(
        paper, "pick_candidates",
        lambda *a, **k: [{"code": "sh600001", "conf": 90,
                          "strategy": "long_buy_left"}])
    monkeypatch.setattr(paper, "_weak_market_flag", lambda: False)
    seen = {}
    def fake_apply(st, cand, weak=False):
        seen["weak"] = weak
        return 0
    monkeypatch.setattr(paper, "_apply_auto_conditions", fake_apply)
    monkeypatch.setattr(paper, "save_state", lambda s: s)
    st = {"scan_count": 0, "candidates": [], "conditions": [], "positions": []}
    paper.run_scan(st, scan_type="", anytime=True)
    assert seen.get("weak") is False


# ── 交易时段判定 (trading_time) ─────────────────────────────

def test_gate_reason_hours_and_day(monkeypatch):
    import datetime
    from wyckoff import trading_time as tt
    monkeypatch.setattr(tt, "is_trading_day", lambda now=None: True)
    # 盘中放行
    for hh, mm in ((9, 30), (10, 0), (11, 30), (13, 0), (14, 59), (15, 0)):
        now = datetime.datetime(2026, 9, 10, hh, mm)
        assert tt.gate_reason(now=now) is None, (hh, mm)
    # 收盘后/午休拦截
    for hh, mm in ((9, 29), (11, 31), (12, 0), (15, 1), (20, 0)):
        now = datetime.datetime(2026, 9, 10, hh, mm)
        assert tt.gate_reason(now=now) is not None, (hh, mm)


def test_gate_reason_non_trading_day(monkeypatch):
    import datetime
    from wyckoff import trading_time as tt
    # 周末: 即使盘中时间也拦截
    now = datetime.datetime(2026, 9, 12, 10, 0)  # 周六
    assert tt.in_trading_hours(now=now) is True
    assert tt.is_trading_day(now=now) is False
    assert tt.gate_reason(now=now) is not None
    # anytime 强制放行
    assert tt.gate_reason(now=now, anytime=True) is None


def test_gate_reason_fallback_weekday(monkeypatch):
    import datetime
    from wyckoff import trading_time as tt
    monkeypatch.setattr(tt, "_load_trade_dates", lambda: None)
    holiday = datetime.datetime(2026, 9, 10, 10, 0)  # 周四盘中
    assert tt.gate_reason(now=holiday) is None       # 日历不可用 → 放行


# ── 微信推送 (交易提醒) ──────────────────────────────────────

def test_notify_trade_disabled_skips(monkeypatch):
    """推送未启用 → 不触发任何推送。"""
    paper._CUR["push_enabled"] = False
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    paper._notify_trade("buy", symbol="sh600001", name="A", qty=100,
                        price=10.0, strategy="paper_discipline_bull")
    assert calls == []


def test_notify_trade_server_chan(monkeypatch):
    """启用 Server酱: 组装标题/正文并调用推送。"""
    paper._CUR.update({"push_enabled": True, "push_method": "server_chan",
                       "server_chan_key": "SCKEY123"})
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    paper._notify_trade("buy", symbol="sh600001", name="新泉股份", qty=4800,
                        price=41.121, strategy=paper.STRATEGY_LONG_LEFT,
                        reason="回踩买入", amount=197400, ts="2026-09-10 10:00")
    assert len(calls) == 1
    method, cfg, title, content = calls[0][0]
    assert method == "server_chan"
    assert cfg == {"sckey": "SCKEY123"}
    assert "买入 新泉股份 sh600001" in title
    assert "回踩买入" in content and "41.121" in content


def test_notify_trade_wechat_work_missing_secret_skips(monkeypatch):
    """企业微信渠道但缺 secret → 跳过, 不产生推送线程。"""
    paper._CUR.update({"push_enabled": True, "push_method": "wechat_work",
                       "wechat_corp_id": "CID", "wechat_corp_secret": ""})
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    paper._notify_trade("sell", symbol="sh600001", name="A", qty=100,
                        buy_price=10.0, sell_price=8.0, ret=-0.2, reason="止损")
    assert calls == []


def test_fill_buy_triggers_push(monkeypatch):
    """fill_buy 成交后触发推送 (buy 事件, 含成交金额)。"""
    paper._CUR.update({"push_enabled": True, "push_method": "server_chan",
                       "server_chan_key": "SK", "cash": 1_000_000.0})
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    st = {"init_cash": 1_000_000.0, "cash": 1_000_000.0, "positions": [],
          "orders": [], "closed": [], "candidates": [], "pending": [],
          "conditions": [], "equity_hist": []}
    order = {"symbol": "sh600001", "name": "新泉股份", "type": "Spring",
             "conf": 100, "qty": 100, "price": 10.0, "ts": "2026-09-10 10:00",
             "strategy": "paper_discipline_bull", "day": "2026-09-10"}
    filled, msg = paper.fill_buy(st, order)
    assert filled is not None
    assert len(calls) == 1
    method, cfg, title, content = calls[0][0]
    assert "买入 新泉股份 sh600001" in title
    assert "纪律" in content and "买入" in content


def test_notify_trade_wxpusher(monkeypatch):
    """WxPusher 渠道: 按逗号分隔拆出主题ID/UID, 组装配置并调用。"""
    paper._CUR.update({
        "push_enabled": True, "push_method": "wxpusher",
        "wxpusher_app_token": "AT_test",
        "wxpusher_topic_ids": "123, 456、789",
        "wxpusher_uids": "",
    })
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    paper._notify_trade("sell", symbol="sz002937", name="兴瑞科技", qty=200,
                        buy_price=20.0, sell_price=19.2, ret=-0.04,
                        reason="止损", bars=8, strategy=paper.STRATEGY_DISCIPLINE)
    assert len(calls) == 1
    method, cfg, title, content = calls[0][0]
    assert method == "wxpusher"
    assert cfg["app_token"] == "AT_test"
    assert cfg["topic_ids"] == ["123", "456", "789"]
    assert "卖出 兴瑞科技 sz002937" in title
    assert "-4.00%" in content
    assert "止损" in content and "8 根K线" in content


def test_notify_trade_wxpusher_missing_receiver_skips(monkeypatch):
    """WxPusher 有 token 但主题/UID 都为空 → 跳过 (避免无人接收的无效发送)。"""
    paper._CUR.update({
        "push_enabled": True, "push_method": "wxpusher",
        "wxpusher_app_token": "AT_test",
        "wxpusher_topic_ids": "", "wxpusher_uids": "",
    })
    calls = []
    monkeypatch.setattr(paper, "_push_dispatch",
                        lambda *a, **k: calls.append((a, k)))
    paper._notify_trade("buy", symbol="sh600001", name="A", qty=100,
                        price=10.0, strategy="")
    assert calls == []
