"""模拟盘条件单层: 价格触发 / 止盈止损 / 追踪止损 + 自动条件生成。"""

import time

import wyckoff.paper as paper

from .. import paper_log, paper_strategy_accuracy
from ._params import SLIP_SELL


# ── 条件单 (价格触发 / 止盈止损 / 追踪止损) ─────────────────
# kind:
#   "buy_price"   价格买入条件单: 现价 满足 trigger 与 price 时买入
#   "sell_price"  价格卖出条件单: 现价 满足 trigger 与 price 时卖出
#   "take_profit" 止盈: 浮盈 ≥ pct 时卖出
#   "stop_loss"   止损: 浮亏 ≥ pct 时卖出
#   "trailing"    追踪止损: 从持仓期内最高价回撤 pct 时卖出
# trigger: "above"(≥) / "below"(≤)  (仅 buy_price/sell_price 用)
_COND_KINDS = ("buy_price", "sell_price", "take_profit", "stop_loss", "trailing")


def _cond(kind, symbol, price=None, pct=None, trigger="above", qty=0,
          name="", reason="", amount=None, activation=None, strategy=""):
    """构造一条条件单记录 (status="active")。
    activation: 移动止盈专用 - 浮盈价, 收盘价≥该价才启用回落跟踪 (未激活不触发)。
    strategy: 信号来源策略 (scan_individual 带入, 供条件单路径补策略归属)。"""
    c = {
        "cid": f"cond-{int(time.time() * 1_000_000)}-{next(paper._CID_SEQ)}",
        "kind": kind, "symbol": symbol, "name": name,
        "price": round(float(price), 3) if price is not None else None,
        "pct": float(pct) if pct is not None else None,
        "trigger": trigger, "qty": int(qty or 0), "amount": amount,
        "reason": reason,
        "activation": round(float(activation), 3) if activation is not None else None,
        "activated": False,
        "strategy": strategy or "",
        "status": "active", "created_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "matched_ts": "", "matched_price": None,
        "peak": None, "correct": None,  # correct: True/False/None(未评估)
    }
    return c


def add_condition(st, kind, symbol, price=None, pct=None, trigger="above",
                  qty=0, name="", reason="", save=True):
    """添加一条条件单到状态并落盘 (save=True)。返回 (cond, msg)。"""
    if kind not in _COND_KINDS:
        return None, f"不支持的条件单类型: {kind}"
    if kind in ("buy_price", "sell_price") and price is None:
        return None, "价格条件单需指定触发价"
    if kind in ("take_profit", "stop_loss", "trailing") and pct is None:
        return None, "止盈/止损/追踪条件单需指定百分比"
    c = _cond(kind, symbol, price=price, pct=pct, trigger=trigger,
              qty=qty, name=name, reason=reason)
    st.setdefault("conditions", []).append(c)
    if save:
        paper.save_state(st)
    return c, "已添加条件单"


def _apply_auto_conditions(st, cand, weak=False):
    """为迭代出的候选批量 upsert buy_price 条件单 (覆盖式去重, 不落盘)。

    由 run_scan/run_cycle 在最终 save_state 前调用一次, 避免扫描线程逐条
    写盘被最终快照覆盖。
    针对多轮扫描同一标的反复生成造成条件单堆积 (曾出现 42 个同代码重复) 的
    问题, 采用覆盖式去重:
      - 候选内同代码至多保留一条 (首次添加后, 后续同代码行仅刷新 name/reason)。
      - 若已存在同代码同类型 active 条件单, 则更新其触发价/名称/说明并视为新增,
        避免重复触发堆积; 同时把历史 outdated 的同代码 active 条件单转 cancelled。
    返回本次有效 upsert/新增的数量。
    """
    conds = st.setdefault("conditions", [])
    touched = 0
    held = {p["symbol"] for p in st.get("positions", [])}
    # 全局预清理: 把历史遗留的重复 active buy_price 条件合流为每代码一条
    # (曾因多轮扫描反复落盘堆积 42 个同代码重复), 先取消多余的再重建;
    # 已持标的的入场条件单一并取消 (持仓期间买入条件单无意义, 且会反复触发→取消)。
    seen = {}
    va_off = not paper._CUR.get("enable_va", True)
    for c in conds:
        if c.get("kind") != "buy_price" or c.get("status") != "active":
            continue
        code = c["symbol"]
        if va_off and c.get("strategy") == STRATEGY_VALUE_ACC:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "停用价值吸筹, 取消入场条件单"
            continue
        if code in held:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "已持仓, 取消入场条件单"
            continue
        if code in seen:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "批量去重: 重复条件单"
        else:
            seen[code] = c
    handled = set()  # 本批已处理(创建/更新)的代码, 保证候选内同代码只保留一条
    for e in cand or []:
        if weak and e.get("strategy") == STRATEGY_VALUE_ACC:
            continue  # 弱市停用价值吸筹入场条件单
        price = e.get("auto_cond_price")
        if not price:
            continue
        code = e["code"]
        if code in handled:
            continue
        if code in held:
            # 已持有: 不再为已持标的生成/刷新入场条件单 (防止条件单永续堆积与
            # 每次周期"触发→已持有→取消"的噪音; 该标的保护单由持仓防护回填管理)
            continue
        handled.add(code)
        reason = f"自动:{e.get('strategy','')}:{e.get('type')}({e.get('conf',0)})"
        # 策略级信号追踪: 廉价记录 (只落盘, 不拉行情), 供准确度/盈利聚合。
        # 同策略+同标的+同事件在冷却窗(20根)内重复扫描自动合并。
        try:
            paper_strategy_accuracy.record_signal(
                e.get("strategy", ""), code, code, e.get("name", ""),
                e.get("type", ""), e.get("conf", 0),
                time.strftime("%Y-%m-%d"), e.get("last", 0) or 0,
                fired=paper.has_position(st, code))
        except Exception:
            pass
        # 覆盖式: 同代码同类型 active 条件单 → 更新触发价等, 而非跳过
        existing = seen.get(code)
        if existing is not None:
            existing["price"] = round(float(price), 3)
            existing["name"] = e.get("name", "")
            existing["reason"] = reason
            existing["trigger"] = e.get("trigger", "above")
            existing["strategy"] = e.get("strategy", "")
            existing["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            touched += 1
            continue
        newc = _cond(
            "buy_price", code, price=float(price),
            trigger=e.get("trigger", "above"),
            name=e.get("name", ""), reason=reason,
            strategy=e.get("strategy", ""))
        conds.append(newc)
        seen[code] = newc
        touched += 1
    return touched


def place_condition(kind, symbol, price=None, pct=None, trigger="above",
                    qty=0, name="", reason=""):
    """独立入口: 加载状态并添加条件单。返回 (cond, msg)。"""
    st = paper.load_state()
    with _LOCK:
        return add_condition(st, kind, symbol, price=price, pct=pct,
                             trigger=trigger, qty=qty, name=name,
                             reason=reason, save=True)


def _create_position_conditions(st, symbol, name="", buy_px=None):
    """建仓后为持仓自动生成止盈/止损条件单, 并消费同标的入场条件单。

    为 symbol 生成 take_profit (pct=paper._CUR.take_profit) + stop_loss
    (pct=paper._CUR.stop_loss), 使条件单 tab 展示持仓保护, 且 _check_conditions 优先
    触发; 同标的已有 active 止盈/止损时不重复生成。
    同时把同标的 active 的 buy_price 入场条件标记为 done (已买入), 防止以后被再次
    触发造成同仓重复买入。
    """
    conds = st.setdefault("conditions", [])
    for c in conds:
        if c.get("kind") == "buy_price" and c.get("symbol") == symbol \
                and c.get("status") == "active":
            c["status"] = "done"
            c["note"] = "已买入, 入场条件单消费"
    if buy_px is not None:
        # 止盈/止损百分比: 左侧买点带御设 (取该持仓自带比值), 否则账户默认。
        pos = next((p for p in st.get("positions", []) if p.get("symbol") == symbol), None)
        take_pct = (pos or {}).get("take_pct") if pos else None
        stop_pct = (pos or {}).get("stop_pct") if pos else None
        active_kinds = {c.get("kind") for c in conds
                        if c.get("symbol") == symbol and c.get("status") == "active"}
        trailing_on = bool(paper._CUR.get("trailing_stop")
                           and float(paper._CUR.get("trail_back_pct") or 0) > 0)
        if trailing_on and "trailing" not in active_kinds:
            # 移动止盈: 浮盈达激活价后才启用峰值回落保护; 激活价默认=止盈线
            # (左侧买点带御设止盈则优先用其目标作为激活线)
            act_pct = float(paper._CUR.get("trail_activate_pct") or 0) \
                or (take_pct or paper._CUR["take_profit"])
            conds.append(_cond(
                "trailing", symbol, pct=float(paper._CUR["trail_back_pct"]), name=name,
                reason="持仓保护:移动止盈",
                activation=buy_px * (1 + act_pct)))
        elif not trailing_on and "take_profit" not in active_kinds:
            conds.append(_cond(
                "take_profit", symbol, pct=take_pct or paper._CUR["take_profit"], name=name,
                reason="持仓保护:take_profit"))
        if "stop_loss" not in active_kinds:
            # 固定止损永远保留为底线保护 (未激活移动止盈时控制下行风险)
            conds.append(_cond(
                "stop_loss", symbol, pct=stop_pct or paper._CUR["stop_loss"], name=name,
                reason="持仓保护:stop_loss"))


def _backfill_position_protection(st):
    """对缺失止盈/止损/追踪保护条件单的持仓自动补齐 (幂等)。

    覆盖历史遗留或非 fill_buy 路径建立的仓位。_create_position_conditions
    已有"同标的 active 保护不重复"的防护, 并顺带消费同标的入场 buy_price 条件单。
    返回补齐的持仓数量。
    """
    n = 0
    for p in st.get("positions", []):
        sym = p["symbol"]
        if any(c.get("kind") in ("take_profit", "stop_loss", "trailing")
               and c.get("symbol") == sym and c.get("status") == "active"
               for c in st.get("conditions", [])):
            continue
        _create_position_conditions(st, sym, name=p.get("name", ""),
                                    buy_px=float(p.get("buy_px", 0) or 0))
        n += 1
    return n


def cancel_condition(st, cid, save=True):
    """取消一条条件单 (status → "cancelled")。返回是否命中。"""
    for c in st.get("conditions", []):
        if c.get("cid") == cid and c.get("status") == "active":
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if save:
                paper.save_state(st)
            return True
    return False


def _match_trigger(trigger, price, cond_price):
    """价格条件比较: above 表示 ≥, below 表示 ≤。"""
    if trigger == "below":
        return price <= cond_price
    return price >= cond_price


def _check_conditions(st, df_by_code):
    """周期推进时检查条件单: 以最新收盘价判断是否触发。

    触发动作:
      - buy_price / sell_price: 现价满足 trigger 与 price 时执行买卖。
      - take_profit / stop_loss: 持仓浮盈/浮亏达到 pct 时平仓。
      - trailing: 持仓期内最高价回撤 pct 时平仓 (无持仓时触发作废)。
    返回触发的条件单数量。
    """
    triggered = 0
    for c in list(st.get("conditions", [])):
        if c.get("status") != "active":
            continue
        sym = c["symbol"]
        df = df_by_code.get(sym)
        if df is None or len(df) == 0:
            continue
        last = float(df["close"].iloc[-1])
        kind = c["kind"]

        if kind == "buy_price":
            if _match_trigger(c["trigger"], last, c["price"]):
                if paper.has_position(st, sym):
                    # 已持有该标的则不重复买入, 直接取消入场条件单
                    c["status"] = "cancelled"
                    c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    c["note"] = "已持有该标的, 入场条件单取消"
                    continue
                _fire_condition(st, c, last, df, side="buy")
                triggered += 1
        elif kind == "sell_price":
            pos = _find_pos(st, sym)
            if pos is None:
                continue
            if _t1_blocked(pos, df):
                continue
            if _match_trigger(c["trigger"], last, c["price"]):
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
        elif kind in ("take_profit", "stop_loss"):
            pos = _find_pos(st, sym)
            if pos is None:
                continue  # 无持仓的止盈止损无意义, 保留等待
            if _t1_blocked(pos, df):
                continue  # A股 T+1: 当日买入禁售
            entry = float(pos["buy_px"])
            ret = last / entry - 1
            if kind == "take_profit" and ret >= c["pct"]:
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
            elif kind == "stop_loss" and ret <= -c["pct"]:
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
        elif kind == "trailing":
            pos = _find_pos(st, sym)
            if pos is None:
                # 无持仓时追踪止损无法建立峰值, 保留但跳过
                continue
            if _t1_blocked(pos, df):
                continue  # A股 T+1: 当日买入禁售
            if c.get("activation") is not None and not c.get("activated"):
                # 移动止盈: 浮盈价未达激活价不触发 (峰值回落保护未启用)
                if last < c["activation"]:
                    continue
                c["activated"] = True
                gold = last
            else:
                gold = float(pos.get("last", pos["buy_px"]))
            peak = c.get("peak") or max(gold, float(pos.get("last", pos["buy_px"])))
            if last > peak:
                peak = last
            c["peak"] = peak
            if peak and last <= peak * (1 - c["pct"]):
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
    return triggered


def _find_pos(st, symbol):
    for p in st["positions"]:
        if p["symbol"] == symbol:
            return p
    return None


def _t1_blocked(pos, df):
    """A股 T+1: 当日买入的持仓当日禁止卖出 (次一交易日方可平仓)。

    仅在 fill_buy 携带了 entry_day (= 买入触发日的 K 线交易日) 时生效;
    历史持仓 (entry_day 为空) 不受限, 兼容旧数据与不传 day 的调用方。
    返回 True 表示当日禁售。
    """
    entry_day = pos.get("entry_day") or ""
    if not entry_day:
        return False
    try:
        bar_day = str(df["day"].iloc[-1])
    except Exception:
        bar_day = ""
    return bool(bar_day) and bar_day == entry_day


def _judge_condition_correct(c, last, entry=None, ret=None, peak=None,
                             side="sell", cond_price=None, pct=None):
    """根据条件类型和行情判断是否正确 (True=绿钩, False=红叉, None=未评估)。

    判断规则:
      - buy_price/above: 价格 ≥ 触发价 → True
      - buy_price/below: 价格 ≤ 触发价 → True
      - sell_price/above: 价格 ≥ 触发价 → True
      - sell_price/below: 价格 ≤ 触发价 → True
      - take_profit: 实际收益 ≥ pct → True
      - stop_loss: 实际亏损 ≥ pct → True (ret <= -pct)
      - trailing: 从峰值回撤 ≥ pct → True
    """
    kind = c.get("kind", "")
    trigger = c.get("trigger", "above")
    price = c.get("price")

    if kind in ("buy_price", "sell_price"):
        if trigger == "above":
            return price is not None and last >= price
        else:  # below
            return price is not None and last <= price

    if kind == "take_profit":
        if ret is not None and pct is not None:
            return ret >= pct
        return None

    if kind == "stop_loss":
        if ret is not None and pct is not None:
            return ret <= -pct
        return None

    if kind == "trailing":
        if not c.get("activated"):
            return None
        if peak is not None and pct is not None:
            # 触发时: 当前价是否已从峰值回撤 pct
            return last <= peak * (1 - pct)
        return None

    return None


def _fire_condition(st, c, last, df, side="buy", pos=None,
                    strategy=None, stop_pct=None, take_pct=None):
    """执行触发动作后把条件单标记为已触发 (status → "done")。

    策略/止盈/止损参数均可由调用方传入; 未传入时沿用候选快照/原有逻辑回退。
    """
    # 首先判断正确性
    entry_price = None
    condition_ret = None
    condition_peak = None
    # 从候选快照取该标的的行业/止损位 (供买入风控门禁判定; 非候选标的全为空 → fail-soft)
    _cond_cand_meta = {}
    for _e in st.get("candidates", []):
        if _e.get("code") == c["symbol"]:
            _cond_cand_meta = _e
            break

    if side == "buy":
        # 统一权益/3 等权口径: 传 st 让 _make_order 按 账户总权益/max_pos 分配,
        # 避免条件单路径走现金/3 导致"先买的大、后买的小"的顺序衰减与资金闲置。
        budget = c.get("amount") or st["cash"]
        # 条件单路径补齐策略归属与离场特化: 与 run_cycle 直接买入同口径。
        # 策略优先取传入参数, 兼容旧数据则回退候选快照。
        if strategy is None:
            strategy = c.get("strategy") or _cond_cand_meta.get("strategy", "")
        if stop_pct is None or take_pct is None:
            entry_px = _cond_cand_meta.get("entry_price")
            if strategy == STRATEGY_LONG_LEFT and entry_px:
                stop_px = float(_cond_cand_meta.get("stop_price") or 0)
                target_px = float(_cond_cand_meta.get("target_price") or 0)
                if stop_px:
                    stop_pct = round((entry_px - stop_px) / entry_px, 4) if stop_pct is None else stop_pct
                if target_px > stop_px:
                    take_pct = round((target_px - entry_px) / entry_px, 4) if take_pct is None else take_pct
        order = paper._make_order(c["symbol"], c.get("name", ""), "条件单",
                            c.get("qty", 0) or 0, last, 0, budget, st=st,
                            strategy=strategy,
                            stop_pct=stop_pct, take_pct=take_pct)
        if paper.has_position(st, c["symbol"]):
            # 已持有该标的: 取消条件单, 防止对同一标的重建仓 (避免资金/仓位被重复占用)
            c["status"] = "cancelled"
            c["note"] = "已持有该标的, 入场条件单取消"
            c["correct"] = None
        elif paper._risk_blocks_entry(st, {
                "code": c["symbol"], "name": c.get("name", ""),
                "stop_price": _cond_cand_meta.get("stop_price"),
                "sector": _cond_cand_meta.get("sector", "")}, last):
            # 买入风控门禁 (与 run_cycle 直接入场路径同口径): 回撤上限/单笔风险/
            # 行业集中度/单股集中度/资金利用率。此前条件单触发绕过全部门禁,
            # 只查现金与同持上限, 造成"被风控拦截的标的仍可能通过条件单买入"。
            c["status"] = "cancelled"
            c["note"] = "风控门禁拦截, 未成交"
            c["correct"] = None
        elif order is None or len(st["positions"]) >= (
                paper._CUR["weak_max_pos"] if st.get("weak") else paper._CUR["max_pos"]):
            # 预算不足/同持已满: 条件单转取消, 防止永久悬挂
            c["status"] = "cancelled"
            c["note"] = "资金/同持上限不足(含弱市限仓), 未成交"
            c["correct"] = None
        else:
            order["day"] = str(df["day"].iloc[-1]) if df is not None else ""
            order["reason"] = "价格条件单触发"
            res, msg = paper.fill_buy(st, order, event_type=c.get("kind") or "条件单")
            if res is None:
                # 现金不足等: 不成交, 取消条件单防止悬挂
                c["status"] = "cancelled"
                c["note"] = msg or "未成交"
                c["correct"] = None
            else:
                c["matched_price"] = order["price"]
                c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                c["status"] = "done"
                # 买入条件单: 判断触发价是否被满足
                # buy_price: 我们在 pick_candidates 中设置 price = last * 1.002 (略高于当前价)
                # 触发意味着 last >= price，所以 correct=True
                c["correct"] = True  # buy_price 已触发突破
                try:
                    paper_log.log_condition_fired(
                        c["symbol"], c.get("name", ""), c["kind"],
                        c.get("price"), last, action="买入", reason="价格触发")
                except Exception:
                    pass
    else:
        if pos is None:
            pos = _find_pos(st, c["symbol"])
        if pos is None:
            c["status"] = "cancelled"
            c["note"] = "无持仓可平"
            c["correct"] = None
            return
        sell_price = round(last * (1 - SLIP_SELL), 3)
        entry_price = float(pos["buy_px"])
        condition_ret = last / entry_price - 1

        # 计算 trailing 的 peak
        if c["kind"] == "trailing" and c.get("peak") is not None:
            condition_peak = c["peak"]
        elif c["kind"] == "trailing":
            # 从持仓峰值计算
            condition_peak = max(last, entry_price)

        paper.close_position(st, pos, sell_price, f"条件单:{c['kind']}", event_type=None)
        c["matched_price"] = sell_price
        c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        c["status"] = "done"
        try:
            paper_log.log_condition_fired(
                c["symbol"], c.get("name", ""), c["kind"],
                c.get("price"), last, action="卖出", reason=f"条件单:{c['kind']}")
        except Exception:
            pass

        # 判断卖出条件单是否正确
        c["correct"] = _judge_condition_correct(
            c, last, entry=entry_price, ret=condition_ret,
            peak=condition_peak, side="sell",
            cond_price=c.get("price"), pct=c.get("pct")
        )


