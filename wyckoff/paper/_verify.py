"""实盘验证点观测 (docs/execution_expectations.md §验证点与降配预警)。

只观测与告警, 不改变交易行为: run_cycle 每周期末调用 _verification_check(st),
把滚动窗口统计与告警写入 st['meta']['verify'], 并对"由好变差"的新触发项推送一次
微信告警 (幂等: 每项状态翻转只告警一次, 避免窗口未改善前每周期刷屏)。

告警口径 (与文档一致):
  - 前 20~30 笔后统计: 胜率 <45% 或 累计期望 <0 (已扣成本) → 建议降为 1 仓观察
  - 连续 10 笔胜率 <35% (正期望信号连续失败) → 触发复核
  - 纪律/左侧成笔盈亏比 <1.8 (止损频繁且无大赢补偿) → 复核事件口径
"""
import statistics
import time

import wyckoff.paper as paper

# ── 验证点阈值 (真源, 与 docs/execution_expectations.md 对齐) ──
VERIFY_WINDOW = 30        # 累计窗口上限 (前 20~30 笔取该滚动窗)
VERIFY_MIN_TRADES = 20    # 满 20 笔后才评估"前 20~30 笔"验证点
VERIFY_WIN_RATE_LOW = 0.45
VERIFY_STREAK_N = 10      # 连续 N 笔
VERIFY_STREAK_WIN_RATE_LOW = 0.35
VERIFY_PL_RATIO_LOW = 1.8 # 盈亏比下限 (纪律/左侧)
VERIFY_PL_MIN_TRADES = 10
# 盈亏比口径只统计主动策略成笔 (纪律 + 左侧买点; 价值吸筹弱策略不计入)
VERIFY_PL_STRATEGIES = ("paper_discipline_bull", "long_buy_left")


def _stats_of(closed):
    """窗口内成笔统计: 胜率/均值/累计/盈亏比 (ret 已扣除交易成本)。"""
    rets = [float(c.get("ret") or 0.0) for c in closed if c.get("ret") is not None]
    if not rets:
        return None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = abs(statistics.mean(losses)) if losses else 0.0
    return {
        "n": len(rets),
        "win_rate": len(wins) / len(rets),
        "mean_ret": statistics.mean(rets),
        "cum_ret": sum(rets),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pl_ratio": (avg_win / avg_loss) if avg_loss > 0 else None,
    }


def _check_window(st):
    """前 20~30 笔验证点: 胜率 <45% 或 累计期望 <0 → 降 1 仓观察。"""
    closed = st.get("closed", [])
    if len(closed) < VERIFY_MIN_TRADES:
        return None
    window = closed[-VERIFY_WINDOW:]
    s = _stats_of(window)
    if s is None or s["n"] < VERIFY_MIN_TRADES:
        return None
    if s["win_rate"] < VERIFY_WIN_RATE_LOW and s["cum_ret"] < 0:
        key, reason = "win_low_exp_neg", (
            f"胜率 {s['win_rate']*100:.1f}% < 45% 且 累计期望 "
            f"{s['cum_ret']*100:+.1f}% < 0")
    elif s["win_rate"] < VERIFY_WIN_RATE_LOW:
        key, reason = "win_rate_low", f"胜率 {s['win_rate']*100:.1f}% < 45%"
    elif s["cum_ret"] < 0:
        key, reason = "neg_expectancy", (
            f"累计期望 {s['cum_ret']*100:+.1f}% < 0 (已扣成本)")
    else:
        return None
    return {
        "key": key,
        "msg": (f"实盘验证点(前{len(window)}笔): {reason}, "
                f"建议降为 1 仓观察"),
        "stats": s,
    }


def _check_streak(st):
    """连续 VERIFY_STREAK_N 笔胜率 <VERIFY_STREAK_WIN_RATE_LOW → 复核。"""
    closed = st.get("closed", [])
    if len(closed) < VERIFY_STREAK_N:
        return None
    recent = closed[-VERIFY_STREAK_N:]
    s = _stats_of(recent)
    if s is None or s["n"] < VERIFY_STREAK_N:
        return None
    if s["win_rate"] < VERIFY_STREAK_WIN_RATE_LOW:
        return {
            "key": "streak_wr_low",
            "msg": (f"实盘验证点: 连续 {s['n']} 笔胜率 "
                    f"{s['win_rate']*100:.1f}% < "
                    f"{VERIFY_STREAK_WIN_RATE_LOW*100:.0f}%, 触发复核"),
            "stats": s,
        }
    return None


def _check_pl_ratio(st):
    """纪律/左侧成笔盈亏比 <1.8 (止损频繁且无大赢补偿) → 复核事件口径。"""
    closed = [c for c in st.get("closed", [])
              if (c.get("strategy") or "") in VERIFY_PL_STRATEGIES]
    if len(closed) < VERIFY_PL_MIN_TRADES:
        return None
    s = _stats_of(closed)
    if s is None or s.get("pl_ratio") is None:
        return None
    if s["pl_ratio"] < VERIFY_PL_RATIO_LOW:
        return {
            "key": "pl_ratio_low",
            "msg": (f"实盘验证点: 纪律/左侧成笔盈亏比 {s['pl_ratio']:.2f} < 1.8 "
                    f"(止损频繁且无大赢补偿), 复核事件口径"),
            "stats": s,
        }
    return None


def _checks(st):
    return [c for c in (_check_window(st), _check_streak(st), _check_pl_ratio(st))
            if c is not None]


def _round_stats(s):
    if not s:
        return {}
    out = {}
    for k, v in s.items():
        out[k] = (round(v, 4) if isinstance(v, float) else v)
    return out


def _verification_check(st):
    """执行验证点观测; 返回本轮新增告警列表 (未改变交易行为)。

    写 st['meta']['verify']:
      - stats:    最近窗口统计快照 (window/win_rate/cum_ret/pl_ratio...)
      - triggers: 已触发项 {key: 首次触发时间}
    新增触发项经 paper._notify_trade(kind="verify") 推送 (受 push_enabled 门控)。
    """
    verify = st.setdefault("meta", {}).setdefault("verify", {})
    window_s = _stats_of(st.get("closed", [])[-VERIFY_WINDOW:])
    if window_s:
        snap = _round_stats(window_s)
        snap["n_trades_total"] = len(st.get("closed", []))
        snap["window"] = VERIFY_WINDOW
        verify["stats"] = snap
    triggers = verify.setdefault("triggers", {})
    fired = []
    for a in _checks(st):
        if a["key"] in triggers:
            continue
        triggers[a["key"]] = time.strftime("%Y-%m-%d %H:%M:%S")
        verify["last_alert"] = a["msg"]
        _notify_verify(a)
        fired.append(a)
    return fired


def _notify_verify(alert):
    """验证点告警推送 (未启用推送/通道配置缺失时静默)。"""
    try:
        lines = []
        s = alert.get("stats") or {}
        if s:
            lines.append(f"窗口 {s.get('n', 0)} 笔, 胜率 {s.get('win_rate', 0)*100:.1f}%"
                         f", 均值 {s.get('mean_ret', 0)*100:+.1f}%,"
                         f" 盈亏比 {s.get('pl_ratio') or '-'}")
        paper._notify_trade("verify", msg=alert.get("msg", ""), lines=lines)
    except Exception:
        pass
