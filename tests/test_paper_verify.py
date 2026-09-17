"""验证点自动监测 (任务②) 测试: 窗口/连败/盈亏比触发与幂等。"""
import wyckoff.paper as paper


def _mk_st(closed):
    return {
        "closed": closed, "meta": {}, "conditions": [],
        "positions": [], "init_cash": 1_000_000.0,
    }


def _trade(ret, strategy="paper_discipline_bull", type_="Spring", conf=100):
    return {"symbol": "sh600001", "type": type_, "conf": conf, "ret": ret,
            "reason": "止损", "strategy": strategy, "bars": 5}


def test_verify_below_min_trades_no_alert():
    """未满 20 笔: 不触发窗口验证点。"""
    st = _mk_st([_trade(-0.02) for _ in range(15)])
    fired = paper._verification_check(st)
    assert all(a["key"] != "win_low_exp_neg" for a in fired)
    assert "stats" in st["meta"]["verify"]


def test_verify_streak_and_pl_fire_then_idempotent():
    """连续 10 笔胜率<35% + 纪律盈亏比<1.8 → 触发; 重复调用不再重复告警。"""
    closed = [_trade(-0.03) for _ in range(10)]   # 10 连亏
    st = _mk_st(closed)
    fired = paper._verification_check(st)
    keys = {a["key"] for a in fired}
    assert "streak_wr_low" in keys
    assert "pl_ratio_low" in keys
    m = st["meta"]["verify"]
    assert m["triggers"]["streak_wr_low"]
    # 幂等: 相同状态再查 → 无新增
    fired2 = paper._verification_check(st)
    assert fired2 == []
    assert set(m["triggers"]) == {"streak_wr_low", "pl_ratio_low"}


def test_verify_window_low_win_and_negative_cum():
    """22 笔全亏 (>=20 满窗) → 胜率 0<45% 且累计<0 → 降仓验证点。"""
    st = _mk_st([_trade(-0.03 if i % 2 == 0 else -0.02) for i in range(22)])
    fired = paper._verification_check(st)
    keys = {a["key"] for a in fired}
    assert "win_low_exp_neg" in keys
    win_alert = next(a for a in fired if a["key"] == "win_low_exp_neg")
    assert "降为 1 仓" in win_alert["msg"]


def test_verify_healthy_state_no_false_alarm():
    """胜率 55% 且盈亏比高 (0.06 赢 / 0.02 亏): 不触发任何验证点。"""
    closed = [_trade(0.06 if i % 2 == 0 else -0.02)
              for i in range(22)]   # 交替盈亏, 最近 10 笔胜率 50%
    st = _mk_st(closed)
    fired = paper._verification_check(st)
    assert fired == []
    assert not st["meta"]["verify"].get("triggers")


def test_verify_pl_ratio_only_counts_active_strategies():
    """盈亏比 <1.8 只统计纪律/左侧, 价值吸筹弱策略不稀释口径。"""
    # 30 笔 VA 全亏不会因策略归属把 pl_ratio_low 触发掉; 纪律 10 笔健康
    va_closed = [_trade(-0.03, strategy="screener_value_accumulation") for _ in range(30)]
    disc_closed = [_trade(0.08) for _ in range(6)] + [_trade(-0.02) for _ in range(4)]
    st = _mk_st(va_closed + disc_closed)
    fired = paper._verification_check(st)
    keys = {a["key"] for a in fired}
    assert "pl_ratio_low" not in keys
