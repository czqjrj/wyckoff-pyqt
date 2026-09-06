"""威科夫策略管理器 · 信号评估器 (纯函数, 无状态/无实例依赖)。

拆分自 manager.py: 每只评估逻辑为独立模块级函数, 供 candidates.py 与对外
兼容方法直接复用。判据不读取管理器实例状态, 便于离线回测/网格扫描直接调用。

门禁判定 (大盘20日线/板块强度/资金流) 统一收敛于 wyckoff.discipline 单一源,
本模块不再反向依赖 wyckoff.paper (解除 paper↔manager 依赖环)。
"""

from wyckoff.discipline import flow_net5, market_trend_ok, sector_strength_ok
from wyckoff.phases import judge_phase
from wyckoff.strategies.constants import LONG_EVENT_TYPES, SPRING_CONFIRM_WINDOW


def trading_discipline(**overrides):
    """策略4 实证的交易纪律 (止损/止盈/持有/同持上限/结构破位),
    供各策略共用, 保证信号落地时执行相同的出场规则。"""
    d = {
        "max_pos": 3,          # 同持上限
        "hold_bars": 20,       # 最大持有K数
        "stop_loss": 0.05,     # 止损 -5%
        "take_profit": 0.15,   # 止盈 +15%
        "support_break": True, # 结构位破位离场
        "cost": 0.004,         # 单边成本
    }
    d.update(overrides)
    return d


def check_discipline_gates(stock_code):
    """纪律三道硬门禁 (与 wyckoff.discipline 单一源一致, fail-close)。

    任一数据不可用即视为不满足 (严格拦截)。返回 {all_pass, details}。"""
    pass_ = True
    details = []
    try:
        ok, reason = market_trend_ok()
        pass_ &= bool(ok)
        details.append(f"大盘20日线: {'通过' if ok else f'拦截({reason})'}")
    except Exception:
        pass_ &= False
        details.append("大盘20日线: 拦截(数据不可用)")
    try:
        from wyckoff.fundamental import fetch_sector
        sector = fetch_sector(stock_code) or ""
        ok, reason = sector_strength_ok(sector)
        pass_ &= bool(ok)
        details.append(f"板块强度: {'通过' if ok else f'拦截({reason})'}")
    except Exception:
        pass_ &= False
        details.append("板块强度: 拦截(数据不可用)")
    try:
        flow = flow_net5(stock_code)
        pass_ &= bool(flow is not None and flow > 0)
        details.append(f"资金流: {'通过' if (flow is not None and flow > 0) else '拦截(净流入不足)'}")
    except Exception:
        pass_ &= False
        details.append("资金流: 拦截(数据不可用)")
    return {"all_pass": bool(pass_), "details": details}


def evaluate_strategy_4(df, i, wevents, nt, vsa_labels, stock_code=None, min_conf=90):
    """策略4: 模拟盘纪律策略 (强多头事件 + high conf + 硬门禁)

    源自 wyckoff.paper 模拟盘实证 (真实K线历史回放胜率~53%、盈亏比~3):
      选股: 强多头事件 {Spring, Shakeout, ST, LPS, SC}, conf≥90, 事件在近10根内;
      纪律硬门禁 (栅栏, 缺条件即拦截):
        ① 大盘20日线向上
        ② 板块强度 > 60 分位
        ③ 资金流近5日主力净流入 > 0
      该策略的盈利主要通过 同持上限3 + 出场纪律(-5%止损/+15%止盈/结构破位) 实现,
      此处仅负责"选股信号"部分; 持仓与出场纪律见 wyckoff.paper。
    """
    bull_events = [
        e for e in wevents
        if e["type"] in LONG_EVENT_TYPES
        and int(e.get("conf", 0) or 0) >= min_conf
        and e["idx"] >= i - 10  # 事件在近10根内（模拟盘可买入窗口）
    ]
    if not bull_events:
        return None
    event = bull_events[0]

    signal = {
        "strategy": "paper_discipline_bull",
        "name": "模拟盘纪律策略",
        "signal": "strong_bull_conf90",
        "confidence": int(event.get("conf", 0) or 0),
        "details": f"模拟盘纪律: 强多头事件 {event['type']} (conf={int(event.get('conf', 0) or 0)})",
        "event": {"type": event["type"], "idx": event["idx"], "conf": int(event.get("conf", 0) or 0)},
        "requirements_met": "强多头+conf≥90",
        "trading": trading_discipline(),
    }

    # 可选硬门禁（需实时数据，离线分析默认关闭；开启时任一不满足即不产生信号）
    if stock_code:
        gates = check_discipline_gates(stock_code)
        signal["gates"] = gates
        if not gates["all_pass"]:
            return None
    return signal


def evaluate_strategy_value_accumulation(df, i, wevents, wpivots):
    """综合选股·价值吸筹 (推荐预设): 底部整固 + 20根内吸筹事件

    源自 research/screener_presets_verify.py 2026-09-02 综合选股预设回测,
    5个预设中唯一实测正期望 (48只样本):
      胜率 49.2%、盈亏比 1.53、累计收益 +224%, 样本内/外 47.4%/51.9%,
      时间半段 48/51, 属"正期望型"(大盈小亏) 而非 >60% 高胜率。
    事件窗口回测口径为 20 根 (比策略4的 conf≥90 + 10根 事件更密, 更稳健)。
    """
    window = df.iloc[:i + 1]
    phase, _ = judge_phase(window, wpivots, wevents)
    if (phase or "").split(" ")[0] != "底部整固":
        return None
    acc_events = [
        e for e in wevents
        if e["type"] in LONG_EVENT_TYPES and e["idx"] >= i - 20
    ]
    if not acc_events:
        return None
    event = acc_events[0]
    return {
        "strategy": "screener_value_accumulation",
        "name": "价值吸筹",
        "signal": "accumulation_bottom_build",
        "confidence": int(event.get("conf", 0) or 0),
        "details": f"价值吸筹: 底部整固 + 吸筹事件 {event['type']} "
                   f"(近20根, conf={int(event.get('conf', 0) or 0)})",
        "event": {"type": event["type"], "idx": event["idx"],
                  "conf": int(event.get("conf", 0) or 0)},
        "requirements_met": "底部整固 + 多头吸筹事件≤20根",
        "trading": trading_discipline(),
        "verified": {
            "date": "2026-09-02", "recommended": True,
            "n": 130, "wr": 49.2, "pf": 1.53, "cum": 223.9,
            "note": "唯一实测正期望(PF>1.5)且样本内外+时间半段稳定; "
                    "正期望型(大盈小亏)而非>60%高胜率",
        },
    }


def evaluate_strategy_spring(df, i, wevents, nt, vsa_labels, stock_code=None):
    """Spring回踩确认策略

    逻辑:
    - 事件: Spring (刺破前低后收回，底部震荡中的假突破/诱多)
    - 入场: Spring确认后的动态窗口 (8根K线) 收盘价守住回升低点
    - 入场确认: 后续收盘价未跌破Spring产生日低点
    - 止损: 低于Spring产生日低点的更低低点，或 3% 止损
    - 止盈: 固定风险比 1:2 或 1:3
    - 特征: Spring是经典的威科夫筑底形态, 实证回测显示Spring后20根
             +12.7% 的上涨概率，是中短线多头的高概率入场时机

    相比策略4: 无需硬门禁, 专注于个股底部反转形态;
    相比SOS策略: 更侧重于震荡区间内的回踩确认而非突破。
    """
    spring_events = [e for e in wevents if e["type"] == "Spring"]
    if not spring_events:
        return None

    # 取最近的Spring事件
    sp = spring_events[-1]
    sp_idx = sp["idx"]

    n = len(df)
    if sp_idx >= n:
        return None

    close = df["close"].values
    low = df["low"].values

    # 使用动态确认窗口: 检查后SPRING_CONFIRM_WINDOW根K线
    # 确认条件: 收盘价守住Spring日低点，未跌破
    dyn_window = SPRING_CONFIRM_WINDOW
    confirm_idx = None

    # 检查确认窗口内是否守住低点
    for j in range(sp_idx + 1, min(sp_idx + 1 + dyn_window, n)):
        if close[j] > low[sp_idx]:  # 收盘守住低点
            confirm_idx = j
            break

    if confirm_idx is None:
        return None  # 确认窗口内跌破低点，放弃

    # 入场价: 确认bar的开盘价
    entry_price = df["open"].iloc[confirm_idx]
    if entry_price <= 0:
        return None

    # 止损: 低于Spring日低点 2% (收紧止损以提高盈亏比)
    stop_price = low[sp_idx] * 0.985

    # 止盈: 固定风险比 1:2.5 (保守比例，确保正期望)
    risk = entry_price - stop_price
    if risk <= 0:
        return None
    target_price = entry_price + risk * 3.0

    # 检查在持有horizon内是否触及止盈/止损
    horizon = 20
    exit_idx = min(confirm_idx + horizon, n - 1)
    exit_price = close[exit_idx]

    # 逐日扫描出场
    hit_tp = False
    hit_sl = False
    actual_exit_price = exit_price

    for j in range(confirm_idx + 1, exit_idx + 1):
        p = close[j]

        # 触及止盈
        if p >= target_price:
            actual_exit_price = target_price
            hit_tp = True
            break

        # 触及硬止损 (低于Spring日低点3%)
        if p <= stop_price:
            actual_exit_price = stop_price
            hit_sl = True
            break

    ret = (actual_exit_price / entry_price - 1) - 0.004  # 扣除成本

    if ret > -0.1 and ret < 0.8:  # 合理返回范围
        return {
            "strategy": "spring_pullback",
            "name": "Spring回踩确认策略",
            "signal": "spring_pullback",
            "confidence": int(sp.get("conf", 0) or 0),
            "details": f"Spring确认: 确认窗口{dyn_window}根守住低点, 入场={entry_price:.2f}, "
                       f"目标={target_price:.2f}, 硬止损={stop_price:.2f}",
            "event": {"type": "Spring", "idx": sp["idx"], "conf": int(sp.get("conf", 0) or 0)},
            "requirements_met": "Spring确认守住低点, 风险回报1:2",
            "trading": {
                "entry_idx": confirm_idx,
                "entry_price": float(entry_price),
                "stop_price": float(stop_price),
                "target_price": float(target_price),
                "horizon": horizon,
                "hit_tp": hit_tp,
                "hit_sl": hit_sl,
                "return": float(ret),
            },
        }


def evaluate_strategy_long_buy(code, datalen=500):
    """威科夫完整做多买点 (可执行集合): 由低风险左侧到右侧加仓。

    可执行买点 (30只×500根240分钟线实证, 持有20K/成本0.4%):
      - SC后二次测试(ST)       n=20 胜率85.0% PF=6.96  ← 低风险左侧试探
      - 弹簧(Spring/震仓)      n=75 胜率72.0% PF=4.32  ← 高盈亏比左侧
      - 弹簧二次测试            n=61 胜率55.7% PF=2.04  ← 高盈亏比左侧
      - 突破回踩(BU/LPS)       n=12 胜率50.0% PF=1.72  ← 加仓确认
    实证负期望的 放量突破(SOS)/中继突破/中继回踩 已从可执行集合剔除。

    在单只股票完整历史上识别全部结构性买点, 返回当前近 ACTIONABLE_LOOK 根
    内仍可执行的最佳买点 (含类目/入场/止损/目标/默认盈亏比), 供集成于
    策略报告展示。不在派发/下跌阶段产生信号。
    """
    from wyckoff.buypoints import analyze_buypoints

    try:
        df, all_bps, latest = analyze_buypoints(code, datalen=datalen)
    except Exception:
        return {"strategy": "long_buy_complete",
                "name": "威科夫完整做多买点", "signals": [], "error": "数据不可用"}
    if not latest:
        return {"strategy": "long_buy_complete",
                "name": "威科夫完整做多买点", "signals": [], "all": len(all_bps)}
    best = latest[0]
    return {
        "strategy": "long_buy_complete",
        "name": "威科夫完整做多买点",
        "signal": f"{best['label']} ({best['cls_label']})",
        "confidence": int(best.get("conf", 50) or 50),
        "details": best["msg"],
        "event": {"type": best["kind"], "idx": best["bar_idx"],
                  "conf": int(best.get("conf", 50) or 50)},
        "requirements_met": f"{best['cls_label']} · RR1:{best['rr']} · "
                            f"止损{best['stop_price']:.2f}",
        "trading": {
            "entry_idx": best["bar_idx"],
            "entry_price": float(best["entry_price"]),
            "stop_price": float(best["stop_price"]),
            "target_price": float(best["target_price"]),
            "rr": float(best["rr"]),
            "position": int(best["position"]),
        },
        "signals": latest,
        "all": len(all_bps),
    }
