"""模拟盘撮合层: 订单构造 / 买入 / 卖出 / 周期步进 / 再平衡。"""

import time
from datetime import datetime

import wyckoff.paper as paper

from .. import paper_log, paper_strategy_accuracy
from ._params import MIN_LOT, SLIP_BUY, SLIP_SELL, TRAILING_STOP, WEAK_MAX_POS


# ── 撮合: 买入/卖出 ─────────────────────────────────────
def _next_open(code, datalen=420):
    """取最新一根日线 (模拟盘以收盘后运行, 下一根"开盘价"用最近收盘近似+滑点)。"""
    from ..datasource import fetch_kline
    from ..indicators import add_indicators
    df = add_indicators(fetch_kline(code, datalen=datalen, scale=240), symbol=code)
    return df


def execute_date(code):
    """最近一根 K 线的日期标识 (用于排重/进度)。"""
    try:
        df = _next_open(code)
        return str(df["day"].iloc[-1])
    except Exception:
        return time.strftime("%Y-%m-%d")


def has_position(st, code):
    return any(p["symbol"] == code for p in st["positions"])


def _make_order(code, name, type_, conf, price, n_total, cash, sector=None,
                strategy="", st=None, stop_pct=None, take_pct=None):
    """构造买单订单公共字段 (qty 按全账户总权益等权预算分配, 整手)。

    sector 为持仓行业 (用于行业集中度风控 check_sector_concentration);
    strategy 记录信号来源策略 (策略管理器: paper_discipline_bull/价值吸筹/威科夫左侧买点)。
    stop_pct/take_pct: 左侧买点自带御设 (由买点离场价折算), 落入持仓保护条件单,
    否则按账户默认止盈止损。
    等权口径: 传入 st 时按 账户总权益/max_pos 分配 (现金+已有持仓市值),
    避免"首批买入吞掉大部分现金、后续仓权重失衡" (曾出现三仓 33万/17万/14万)。
    未传 st 时回退 现金/max_pos (兼容旧调用方与测试)。
    """
    equity_base = cash
    if st is not None:
        mv = sum(float(p.get("last", p["buy_px"])) * p["qty"]
                 for p in st.get("positions", []))
        equity_base = float(st["cash"]) + mv
    budget = equity_base * (1.0 / max(1, paper._CUR["max_pos"]))
    # 价值吸筹降权: 单仓资金×va_weight (弱策略敞口控制, 其余策略=1.0)
    if strategy == STRATEGY_VALUE_ACC:
        budget *= float(paper._CUR.get("va_weight", 1.0) or 1.0)
    if budget < MIN_LOT:
        return None
    qty = int(budget // (price * (1 + SLIP_BUY)) // 100 * 100)
    if qty <= 0:
        return None
    return {
        "symbol": code, "name": name, "type": type_, "conf": int(conf),
        "qty": qty, "price": round(price * (1 + SLIP_BUY), 3), "side": "buy",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "date": "", "bars": int(n_total) if n_total else 0,
        "sector": sector or "", "strategy": strategy,
        "stop_pct": stop_pct, "take_pct": take_pct,
    }


def place_buy_order(code, name, type_, conf, price, n_total, execute=True,
                    strategy=""):
    """下单买入 (execute=True 直接按现价撮合成交; False 进 pending 待成交)。

    独立入口: 自行加载状态、校验持仓/同持上限/资金。返回 (order, msg)。
    """
    st = paper.load_state()
    with _LOCK:
        if has_position(st, code):
            return None, "已持有"
        weak = paper._weak_market_flag()  # 刷新弱市标记 (UI 手动买入也走弱市限仓)
        st["weak"] = weak
        if weak and strategy == STRATEGY_VALUE_ACC:
            return None, "弱市已禁用价值吸筹"
        limit = paper._CUR["weak_max_pos"] if weak else paper._CUR["max_pos"]
        if len(st["positions"]) >= limit:
            return None, "同持已满"
        order = _make_order(code, name, type_, conf, price, n_total, st["cash"],
                            strategy=strategy, st=st)
        if order is None:
            return None, "金额不足一手"
        order["reason"] = "手动买入"
        if execute:
            return fill_buy(st, order)
        st["pending"].append(order)
        paper.save_state(st)
        return order, "已下单待撮合"


def _enqueue_buy(st, code, name, type_, conf, price, sector=None, strategy=""):
    """(进程内) 仅计入 pending, 不校验不落盘。调用方负责校验与 save_state。"""
    order = _make_order(code, name, type_, conf, price, 0, st["cash"],
                        sector=sector, strategy=strategy, st=st)
    if order is None:
        return None
    st["pending"].append(order)
    return order


def fill_buy(st, order, event_type: str = None):
    """口头成交: 扣现金、建仓。现金不足时不成交, 返回 (None, 原因)。"""
    price = order["price"]
    qty = order["qty"]
    cost = qty * price * paper._CUR["cost"]
    spend = qty * price + cost
    if st.get("cash", 0) < spend:
        return None, "现金不足"
    st["cash"] -= spend
    # 止损/止盈: 左侧买点自带御设 (run_cycle/条件单路径已折算为 stop_pct/take_pct);
    # 其余策略走账户默认 (价值吸筹无需特化: 实证无独立止盈/止损可采纳证据,
    # 与全局默认一致, 见 _rebalance/离场逻辑)。
    stop_pct = order.get("stop_pct")
    take_pct = order.get("take_pct")
    st["positions"].append({
        "symbol": order["symbol"], "name": order.get("name", ""),
        "type": order["type"], "conf": order.get("conf", 50),
        "qty": qty, "buy_px": price, "cost": round(cost, 2),
        "entry_ts": order["ts"], "entry_bars": order.get("bars", 0),
        "sector": order.get("sector", ""),
        "strategy": order.get("strategy", ""),
        "stop_pct": stop_pct,
        "take_pct": take_pct,
        "staged": False,
        "event_type": event_type,
        "entry_day": order.get("day")
        or datetime.now().strftime("%Y-%m-%d"),
    })
    st["orders"].append(order)
    paper._create_position_conditions(st, order["symbol"], name=order.get("name", ""),
                                buy_px=price)
    paper.save_state(st)
    try:
        paper_strategy_accuracy.mark_fired(order.get("strategy", ""),
                                           order["symbol"])
    except Exception:
        pass
    try:
        paper_log.log_buy(
            symbol=order["symbol"], name=order.get("name", ""),
            qty=qty, price=price, conf=order.get("conf", 0),
            strategy=order.get("strategy", ""), event_type=event_type or "",
            sector=order.get("sector", ""), reason=order.get("reason", "买入")
        )
    except Exception:
        pass
    paper._notify_trade("buy", symbol=order["symbol"],
                  name=order.get("name", ""), qty=qty, price=price,
                  strategy=order.get("strategy", ""),
                  reason=order.get("reason", "买入"),
                  event_type=event_type or "",
                  amount=round(spend, 2), ts=order["ts"])
    return order, "成交"


def step(st, df_by_code, trading=True):
    """推进一个周期: 用当前行情更新最新价 → 检查卖单条件 + 成交 in-arrow。

    df_by_code: {symbol: df(含 indicators)} 当前最新行情窗口 (由 UI/调度器提供)。
    不可用 (无行情) 时跳过。
    trading=False: 收盘/节假日后的"纯快照"模式 — 只 mark-to-market 更新最新价,
    不触发任何买卖撮合 (买/卖/止盈止损/待撮合买单全部冻结, 待下一个交易日时段内
    恢复)。供 run_cycle 在非交易时段禁止撮合用。
    """
    # 先检查条件单: 用户自定义的价格触发/止盈/止损/追踪优先于默认止盈止损。
    if trading:
        paper._check_conditions(st, df_by_code)
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        if df is None or len(df) == 0:
            continue
        bar_day = str(df["day"].iloc[-1]) or ""
        entry = float(pos["buy_px"])
        entry_day = str(pos.get("entry_day") or "")
        # 开仓当日 (最新 K 线交易日 == 买入触发日): 用成交价做基准标记,
        # 避免"买入前一日收盘价"对刚开的仓造成假浮亏; 次一交易日自动切换真实收盘价。
        if bar_day and entry_day and bar_day[:10] == entry_day[:10]:
            last = entry
        else:
            last = float(df["close"].iloc[-1])
        ret = last / entry - 1
        pos["last"] = round(last, 3)
        pos["last_ret"] = round(paper.float_ret(entry, last), 4)
        # 止损价: 固定止损 或 追踪止损 (从持仓期内最高价回撤, 防噪音洗出)。
        # 左侧买点持仓优先按其自带御设止损/止盈百分比。
        pos_take = float(pos.get("take_pct") or paper._CUR["take_profit"])
        pos_stop = float(pos.get("stop_pct") or paper._CUR["stop_loss"])
        # 移动止盈模式: 峰值回撤交给 trailing 条件单 (浮盈达激活价后回落平仓),
        # 此处兜底只留固定结构止损 (相对买入价), 避免双重追踪截断回落窗口。
        trailing_mode = bool(paper._CUR.get("trailing_stop", TRAILING_STOP)
                             and float(paper._CUR.get("trail_back_pct", 0)) > 0)
        if trailing_mode:
            stop_px = entry * (1 - pos_stop)
        elif paper._CUR.get("trailing_stop", TRAILING_STOP):
            hi = float(df["high"].iloc[-1])
            peak = float(pos.get("peak") or entry)
            if hi > peak:
                peak = hi
                pos["peak"] = round(peak, 3)
            stop_px = peak * (1 - pos_stop)
            atr_mult = float(paper._CUR.get("trail_atr_mult", TRAIL_ATR_MULT) or 0.0)
            if atr_mult > 0 and "atr" in df.columns and len(df):
                atr = float(df["atr"].iloc[-1] or 0.0)
                stop_px -= atr * atr_mult
        else:
            stop_px = entry * (1 - pos_stop)
        # 结构位: 用最近 10 根低点做动态支撑 (近似结构关键位)
        support = float(df["low"].iloc[-10:].min())
        # 卖出判定 (任一触发); 持仓 K 数按"交易日"推进: 仅当行情 K 线交易日
        # 发生变化才 +1, 避免同一天多周期被重复计入 (曾出现 20 周期≈10 小时即
        # "到期"的高估)。
        if bar_day and pos.get("counted_day") != bar_day:
            pos["counted_day"] = bar_day
            pos["entry_bars"] = int(pos.get("entry_bars", 0)) + 1
        reason = None
        held = int(pos["entry_bars"])
        if paper._t1_blocked(pos, df):
            # A股 T+1: 当日买入的证券次一交易日方可卖出, 本周期跳过卖出判定
            continue
        if not trading:
            # 非交易时段: 只 mark-to-market (上面已更新 last), 冻结卖出判定
            continue
        if not trailing_mode and ret >= pos_take:
            reason = "止盈"
        elif last <= stop_px:
            reason = "止损"
        elif support < stop_px and last <= support:
            reason = "破位"
        elif held >= paper._CUR["hold_bars"]:
            reason = "到期"
        if reason:
            # 模拟盘按市价即时成交: 一律以现价(含卖滑点)结算, 不再用 max(stop_px,last)
            # 高估止损价。历史实现止损在 last<stop_px 时按 stop_px 成交, 低估了实际损失。
            sell_price = last * (1 - SLIP_SELL)
            close_position(st, pos, sell_price, reason, event_type=pos.get("event_type"))
    # 处理待撮合买单 (非交易时段冻结, 待恢复后撮合)
    if trading:
        for o in list(st["pending"]):
            df = df_by_code.get(o["symbol"])
            if df is not None and len(df):
                close = float(df["close"].iloc[-1])
                o["price"] = round(close * (1 + SLIP_BUY), 3)
                o["day"] = str(df["day"].iloc[-1])
                if not o.get("date"):
                    o["date"] = str(df["day"].iloc[-1])
                order = dict(o)
                st["pending"].remove(o)
                fill_buy(st, order)


def _rebalance_portfolio(st, df_by_code):
    """满仓时向等权目标收敛, 把权重过低的持仓补足到 总权益/有效持仓上限。

    触发条件: len(positions) == 有效持仓上限 (弱市=WEAK_MAX_POS, 否则=MAX_POS)
    且现金富余; 弱市的单仓若已超 MAX_SINGLE_CONCENTRATION 集中度上限则不再补。
    加仓直接合并进已有持仓 (摊薄成本), 不新增同标的多仓; 保留已建立的追踪 peak 与
    entry_bars (加仓不改变止损保护起点)。本次交易日加仓的份额受 T+1 约束:
    合并持仓的 entry_day 顺延为加仓交易日, 当日禁止卖出。返回补仓笔数。

    df_by_code: 需含待补仓标的当根行情 (用于现价与成交)。
    """
    from wyckoff.strategies.constants import STRATEGY_VALUE_ACC
    if not paper._CUR.get("rebalance", False):
        return 0
    eff_max = paper._CUR["weak_max_pos"] if st.get("weak") else paper._CUR["max_pos"]
    if len(st.get("positions", [])) < eff_max:
        return 0
    positions = st.get("positions", [])
    mv_total = sum(float(p.get("last", p["buy_px"])) * p["qty"] for p in positions)
    equity = float(st["cash"]) + mv_total
    target = equity / max(1, eff_max)
    rebalanced = 0
    for pos in positions:
        sym = pos["symbol"]
        last = float(pos.get("last") or pos["buy_px"])
        cur_mv = float(pos.get("last", pos["buy_px"])) * pos["qty"]
        # 价值吸筹单仓资金权重: 降低弱策略敞口
        va_weight = float(paper._CUR.get("va_weight", 0.6) or 1.0)
        strategy = pos.get("strategy", "")
        effective_target = target
        if strategy == STRATEGY_VALUE_ACC:
            effective_target = target * va_weight
        # 容忍带: 仅当权重明显不足 (< 目标*0.75) 且现金可覆盖时才补
        short = effective_target * 0.75 - cur_mv
        if short <= 0:
            continue
        if st.get("weak"):
            # 弱市单仓集中度上限: 补仓后该股市值不得超过 总权益*max_single_conc
            # (弱市目标=总权益/1 会过度集中, 以单股集中度兜底)
            short = min(short, paper._CUR["max_single_conc"] * equity - cur_mv)
            if short <= 0:
                continue
        cost = last * (1 + SLIP_BUY)
        qty = int(short // (cost) // 100 * 100)  # 整手(百股)
        if qty <= 0:
            continue
        fee = qty * cost * paper._CUR["cost"]
        spend = qty * cost + fee
        if st["cash"] < spend:
            continue
        # 合并进已有持仓: 追加数量 + 摊薄成本
        old_qty = pos["qty"]
        pos["qty"] = int(old_qty) + qty
        new_buy = (float(pos["buy_px"]) * old_qty + cost * qty) / pos["qty"]
        pos["buy_px"] = round(new_buy, 3)
        pos["cost"] = round(float(pos.get("cost", 0)) + fee, 2)
        pos["last"] = round(last, 3)
        st["cash"] -= spend
        # A股 T+1: 加仓份额当日不可卖, 合并持仓 entry_day 顺延为加仓交易日,
        # 使整仓当日禁止卖出 (与 fill_buy 的新仓同口径)。
        try:
            _d = df_by_code.get(sym)
            if _d is not None and len(_d):
                pos["entry_day"] = str(_d["day"].iloc[-1])
        except Exception:
            pass
        st["orders"].append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "symbol": sym,
            "name": pos.get("name", ""), "strategy": pos.get("strategy", ""),
            "qty": qty, "price": round(cost, 3), "type": "再平衡加仓",
            "conf": pos.get("conf", 0), "side": "buy", "date": "",
        })
        try:
            paper_log.log_rebalance(sym, pos.get("name", ""), qty,
                                    round(cost, 3), old_qty, pos["qty"])
        except Exception:
            pass
        rebalanced += 1
    return rebalanced


def close_position(st, pos, sell_price, reason, event_type=None):
    """平仓: 回收现金、记录已平仓与净值。"""
    from wyckoff.strategies.sell_strategy import evaluate_sell_reason
    # 基于事件类型的卖出策略
    reason = evaluate_sell_reason(event_type, reason)
    price = round(sell_price, 3)
    gross = price * pos["qty"]  # 不含卖出成本的口径内部用
    fee = gross * paper._CUR["cost"]
    proceeds = gross - fee
    st["cash"] += proceeds
    outlay = pos["buy_px"] * pos["qty"] * paper.net_cost_rate()
    ret_total = (proceeds - outlay) / outlay
    st["closed"].append({
        "symbol": pos["symbol"], "name": pos.get("name", ""),
        "type": pos["type"], "conf": pos.get("conf", 50),
        "qty": pos["qty"], "buy_px": pos["buy_px"],
        "sell_px": price, "ret": round(ret_total, 4),
        "reason": reason, "entry_ts": pos["entry_ts"],
        "close_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bars": int(pos.get("entry_bars", 0)),
        "strategy": pos.get("strategy", ""),
    })
    st["positions"] = [p for p in st["positions"] if p is not pos]
    paper._record_equity(st)
    paper.save_state(st)
    try:
        paper_log.log_sell(
            symbol=pos["symbol"], name=pos.get("name", ""),
            qty=pos["qty"], buy_price=pos["buy_px"], sell_price=price,
            reason=reason, ret=ret_total,
            bars_held=int(pos.get("entry_bars", 0)),
            strategy=pos.get("strategy", ""), event_type=pos.get("event_type", "")
        )
    except Exception:
        pass
    paper._notify_trade("sell", symbol=pos["symbol"], name=pos.get("name", ""),
                  qty=pos["qty"], buy_price=pos["buy_px"], sell_price=price,
                  ret=ret_total, reason=reason,
                  bars=int(pos.get("entry_bars", 0)),
                  strategy=pos.get("strategy", ""))


def force_close_position(st, symbol: str, reason: str = "手动平仓"):
    """按代码手动平仓 (供 UI 持仓行操作)。找不到持仓返回 None, 成功返回平仓记录。

    A股 T+1: 当日买入 (entry_day == 今天) 的持仓拒绝当日平仓, 返回 None。
    """
    for pos in st["positions"]:
        if pos["symbol"] == symbol:
            entry_day = pos.get("entry_day") or ""
            if entry_day and entry_day == datetime.now().strftime("%Y-%m-%d"):
                return None
            close_position(st, pos, pos.get("last", pos["buy_px"]), reason)
            return pos
    return None


