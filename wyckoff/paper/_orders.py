"""模拟盘高级订单层: OCO/括号单/分批建仓平仓/追踪止损 (离线保留, 未接入撮合)。"""

import time

import wyckoff.paper as paper

from ._params import AdvancedOrder, OrderSide, OrderStatus, OrderType


# ── 高级订单管理 ────────────────────────────────────────────
def create_oco_order(st, symbol: str, name: str, qty: int,
                     take_profit_price: float, stop_loss_price: float,
                     side: OrderSide = OrderSide.SELL) -> tuple[str, str]:
    """创建 OCO 订单 (一单成交，一单撤销)。

    返回: (take_profit_order_id, stop_loss_order_id)
    """
    parent_id = f"oco-{int(time.time() * 1_000_000)}"

    tp_order = AdvancedOrder(
        order_id=f"{parent_id}-tp",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=side,
        qty=qty,
        price=take_profit_price,
        parent_id=parent_id,
        tags={"oco_role": "take_profit"},
    )

    sl_order = AdvancedOrder(
        order_id=f"{parent_id}-sl",
        symbol=symbol,
        name=name,
        order_type=OrderType.STOP,
        side=side,
        qty=qty,
        stop_price=stop_loss_price,
        parent_id=parent_id,
        tags={"oco_role": "stop_loss"},
    )

    tp_order.child_ids = [sl_order.order_id]
    sl_order.child_ids = [tp_order.order_id]

    st.setdefault("advanced_orders", []).extend([tp_order.to_dict(), sl_order.to_dict()])
    paper.save_state(st)

    return tp_order.order_id, sl_order.order_id


def create_bracket_order(st, symbol: str, name: str, qty: int,
                         entry_price: float, take_profit_price: float,
                         stop_loss_price: float,
                         side: OrderSide = OrderSide.BUY) -> dict:
    """创建括号单 (入场单 + 止盈单 + 止损单)。

    返回: 入场单、止盈单、止损单的 ID 字典
    """
    parent_id = f"bracket-{int(time.time() * 1_000_000)}"

    entry_order = AdvancedOrder(
        order_id=f"{parent_id}-entry",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=side,
        qty=qty,
        price=entry_price,
        parent_id=parent_id,
        tags={"bracket_role": "entry"},
    )

    exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

    tp_order = AdvancedOrder(
        order_id=f"{parent_id}-tp",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=exit_side,
        qty=qty,
        price=take_profit_price,
        parent_id=parent_id,
        tags={"bracket_role": "take_profit", "depends_on": entry_order.order_id},
    )

    sl_order = AdvancedOrder(
        order_id=f"{parent_id}-sl",
        symbol=symbol,
        name=name,
        order_type=OrderType.STOP,
        side=exit_side,
        qty=qty,
        stop_price=stop_loss_price,
        parent_id=parent_id,
        tags={"bracket_role": "stop_loss", "depends_on": entry_order.order_id},
    )

    entry_order.child_ids = [tp_order.order_id, sl_order.order_id]
    tp_order.child_ids = [sl_order.order_id]
    sl_order.child_ids = [tp_order.order_id]

    orders = [entry_order.to_dict(), tp_order.to_dict(), sl_order.to_dict()]
    st.setdefault("advanced_orders", []).extend(orders)
    paper.save_state(st)

    return {
        "entry": entry_order.order_id,
        "take_profit": tp_order.order_id,
        "stop_loss": sl_order.order_id,
    }


def create_scale_in_order(st, symbol: str, name: str, total_qty: int,
                          entry_prices: list[float],
                          side: OrderSide = OrderSide.BUY) -> list[str]:
    """创建分批建仓订单。

    entry_prices: 每批的限价列表
    """
    n_batches = len(entry_prices)
    if n_batches == 0:
        return []

    base_qty = total_qty // n_batches
    remainder = total_qty % n_batches
    order_ids = []
    parent_id = f"scalein-{int(time.time() * 1_000_000)}"

    for i, price in enumerate(entry_prices):
        qty = base_qty + (1 if i < remainder else 0)
        if qty <= 0:
            continue
        order = AdvancedOrder(
            order_id=f"{parent_id}-{i}",
            symbol=symbol,
            name=name,
            order_type=OrderType.LIMIT,
            side=side,
            qty=qty,
            price=price,
            parent_id=parent_id,
            tags={"scale_role": "entry", "batch": i},
        )
        order_ids.append(order.order_id)
        st.setdefault("advanced_orders", []).append(order.to_dict())

    paper.save_state(st)
    return order_ids


def create_scale_out_order(st, symbol: str, name: str, total_qty: int,
                           exit_prices: list[float],
                           side: OrderSide = OrderSide.SELL) -> list[str]:
    """创建分批平仓订单。"""
    n_batches = len(exit_prices)
    if n_batches == 0:
        return []

    base_qty = total_qty // n_batches
    remainder = total_qty % n_batches
    order_ids = []
    parent_id = f"scaleout-{int(time.time() * 1_000_000)}"

    for i, price in enumerate(exit_prices):
        qty = base_qty + (1 if i < remainder else 0)
        if qty <= 0:
            continue
        order = AdvancedOrder(
            order_id=f"{parent_id}-{i}",
            symbol=symbol,
            name=name,
            order_type=OrderType.LIMIT,
            side=side,
            qty=qty,
            price=price,
            parent_id=parent_id,
            tags={"scale_role": "exit", "batch": i},
        )
        order_ids.append(order.order_id)
        st.setdefault("advanced_orders", []).append(order.to_dict())

    paper.save_state(st)
    return order_ids


def create_trailing_stop_order(st, symbol: str, name: str, qty: int,
                               trail_pct: float, activation_price: float = None,
                               side: OrderSide = OrderSide.SELL) -> str:
    """创建追踪止损订单。"""
    order = AdvancedOrder(
        order_id=f"trail-{int(time.time() * 1_000_000)}",
        symbol=symbol,
        name=name,
        order_type=OrderType.TRAILING,
        side=side,
        qty=qty,
        trail_pct=trail_pct,
        trail_price=activation_price,
        tags={"trail_activated": activation_price is not None},
    )

    st.setdefault("advanced_orders", []).append(order.to_dict())
    paper.save_state(st)
    return order.order_id


def check_advanced_orders(st, df_by_code: dict) -> int:
    """检查并执行高级订单。返回成交数量。"""
    filled = 0
    orders = st.get("advanced_orders", [])

    for order_dict in list(orders):
        if order_dict.get("status") != "pending":
            continue

        order = AdvancedOrder.from_dict(order_dict)
        df = df_by_code.get(order.symbol)
        if df is None or len(df) == 0:
            continue

        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])

        executed = False
        fill_price = None

        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and low <= order.price:
                executed = True
                fill_price = min(order.price, float(df["open"].iloc[-1]))
            elif order.side == OrderSide.SELL and high >= order.price:
                executed = True
                fill_price = max(order.price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.STOP:
            if order.side == OrderSide.BUY and high >= order.stop_price:
                executed = True
                fill_price = max(order.stop_price, float(df["open"].iloc[-1]))
            elif order.side == OrderSide.SELL and low <= order.stop_price:
                executed = True
                fill_price = min(order.stop_price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.STOP_LIMIT:
            # 止损限价: 触发止损价后按限价成交
            triggered = False
            if order.side == OrderSide.BUY and high >= order.stop_price:
                triggered = True
            elif order.side == OrderSide.SELL and low <= order.stop_price:
                triggered = True

            if triggered and order.limit_price is not None:
                if order.side == OrderSide.BUY and low <= order.limit_price:
                    executed = True
                    fill_price = min(order.limit_price, float(df["open"].iloc[-1]))
                elif order.side == OrderSide.SELL and high >= order.limit_price:
                    executed = True
                    fill_price = max(order.limit_price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.TRAILING:
            # 追踪止损: 价格创新高后回撤 trail_pct 触发
            if order.trail_price is None:
                # 未激活: 价格突破激活价时激活
                if order.side == OrderSide.SELL and high >= (order.trail_price or 0):
                    order.trail_price = high
                    order_dict["trail_price"] = high
                    order_dict["tags"]["trail_activated"] = True
            else:
                # 已激活: 更新追踪价
                if order.side == OrderSide.SELL:
                    if high > order.trail_price:
                        order.trail_price = high
                        order_dict["trail_price"] = high
                    stop_trigger = order.trail_price * (1 - order.trail_pct)
                    if low <= stop_trigger:
                        executed = True
                        fill_price = stop_trigger
                else:  # BUY trailing (较少见)
                    if low < order.trail_price:
                        order.trail_price = low
                        order_dict["trail_price"] = low
                    stop_trigger = order.trail_price * (1 + order.trail_pct)
                    if high >= stop_trigger:
                        executed = True
                        fill_price = stop_trigger

        if executed and fill_price:
            # 执行成交
            fill_price = round(fill_price * (1 + SLIP_BUY if order.side == OrderSide.BUY else 1 - SLIP_SELL), 3)
            order.filled_qty = order.qty
            order.avg_fill_price = fill_price
            order.status = OrderStatus.FILLED
            order.updated_ts = time.strftime("%Y-%m-%d %H:%M:%S")

            # 更新状态
            order_dict.update(order.to_dict())

            # 处理 OCO/括号单的联动撤销
            if order.parent_id:
                _cancel_sibling_orders(st, order)

            filled += 1

    if filled > 0:
        paper.save_state(st)
    return filled


def _cancel_sibling_orders(st, filled_order: AdvancedOrder):
    """成交时撤销同组的其他订单 (OCO/括号单)。"""
    orders = st.get("advanced_orders", [])
    for o in orders:
        if o.get("parent_id") == filled_order.parent_id and o.get("order_id") != filled_order.order_id:
            if o.get("status") == "pending":
                o["status"] = "cancelled"
                o["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")


def cancel_advanced_order(st, order_id: str) -> bool:
    """撤销高级订单。"""
    orders = st.get("advanced_orders", [])
    for o in orders:
        if o.get("order_id") == order_id and o.get("status") == "pending":
            o["status"] = "cancelled"
            o["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # 同时撤销子订单
            for child_id in o.get("child_ids", []):
                for oc in orders:
                    if oc.get("order_id") == child_id and oc.get("status") == "pending":
                        oc["status"] = "cancelled"
                        oc["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            paper.save_state(st)
            return True
    return False

