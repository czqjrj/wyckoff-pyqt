"""模拟盘 (Paper Trading) 核心引擎。

自动筛选 → 自动下单 → 自动卖出 → 收益统计 全自动闭环, 无真实资金风险。

设计要点 (对齐项目已验证的实证参数, 见 docs/profitability_bt.md):
  - 选股: 全市场 universe 逐股 detect_all, 只取强多头事件 (Spring/Shakeout/
    ST/LPS/SC/SOW_INVALID) 且 conf 达标 (默认 ≥90) 的标的, 按 conf 排序。
  - 撮合: 候选按 conf 填充, 同持上限内用"最近收盘价 + 滑点"买入
    (无未来函数: 同一周期内新成交仓位不参与当期卖出评估)。
  - 卖出触发: 持有满 HOLD_BARS 根到期 / 结构位-5%止损 / 破位 / +15%止盈。
  - 统计: 分类型/总账户 胜率、盈亏比、净值曲线、最大回撤。
  - 存储: 单 JSON (wx_paper.json), 与项目其他 wx_* 数据文件同目录同风格。
数据目录用 paths.DATA_DIR, 测试用 WYCKOFF_DATA_DIR 隔离。
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from .paths import PAPER_FILE
from .settings_keys import S

_LOCK = threading.RLock()

# ── 可配置策略参数 ─────────────────────────────────────────
# 持仓周期 K 根 (日线, ~1 个月)
HOLD_BARS = 20
# 同持最大股票数 (资金/风险分散, 参考组合回测 MAXPOS=3)
MAX_POSITIONS = 3
# 每笔资金占比 (1/MAX_POSITIONS 等权)
_POS_WEIGHT = 1.0 / MAX_POSITIONS
# 单边成本 (含佣金+印花税+滑点, 参考 backtest.cost=0.004)
COST = 0.004
# 买入滑点 (价格摩擦, 占成交价比例)
SLIP_BUY = 0.001
SLIP_SELL = 0.001
# 止损: 结构位 ± 0.5×ATR, 简化用固定百分比 (回测实证 -5% 盈亏比最优)
STOP_LOSS = 0.05
# 止盈: 盈利 +15% 落袋 (结合止损的不对称盈亏比)
TAKE_PROFIT = 0.15
# 初始终端资金 (模拟资产)
INIT_CASH = 1_000_000.0
# 单笔最低可交易金额 (避免碎股/零股本)
MIN_LOT = 100.0
# conf 过滤下限: 只做实证收益显著为正的强信号 (参见 docs/profitability_bt.md)
MIN_CONF = 90


def apply_paper_params(settings=None):
    """从用户设置 dict 解析并覆盖模拟盘策略参数, 返回当前生效参数字典 `_CUR`.

    不传/键缺失时回退到模块常量默认值, 保证与旧行为完全一致 (测试兼容)。
    支持键 (见 settings_keys.Paper): INIT_CASH/MAX_POS/HOLD_BARS/STOP_LOSS/
    TAKE_PROFIT/COST/MIN_CONF。调用方 (run_cycle/UI) 在周期执行前调用即可生效。
    """
    settings = settings or {}

    def _get(key, default):
        v = settings.get(key)
        return default if v is None or v == "" else v

    global _CUR
    _CUR = {
        "init_cash": float(_get(S.Paper.INIT_CASH, INIT_CASH)),
        "max_pos": max(1, int(_get(S.Paper.MAX_POS, MAX_POSITIONS))),
        "hold_bars": max(1, int(_get(S.Paper.HOLD_BARS, HOLD_BARS))),
        "stop_loss": float(_get(S.Paper.STOP_LOSS, STOP_LOSS)),
        "take_profit": float(_get(S.Paper.TAKE_PROFIT, TAKE_PROFIT)),
        "cost": float(_get(S.Paper.COST, COST)),
        "min_conf": int(_get(S.Paper.MIN_CONF, MIN_CONF)),
    }
    return _CUR


# 当前生效参数 (默认=模块常量; 由 apply_paper_params 按用户设置覆盖)。
_CUR = {
    "init_cash": INIT_CASH,
    "max_pos": MAX_POSITIONS,
    "hold_bars": HOLD_BARS,
    "stop_loss": STOP_LOSS,
    "take_profit": TAKE_PROFIT,
    "cost": COST,
    "min_conf": MIN_CONF,
}

# 强多头事件: 方向命中显著优于随机且可裸多落地 (与 docs/profitability_bt.md 一致)
try:
    from .config import STRONG_TIER_TYPES, event_dir
    # SC/SOW_INVALID 方向为中性 (event_dir==0) 但属底部反转/空头失效, 一并纳入
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "Shakeout", "ST", "LPS", "SC", "SOW_INVALID"}
    ) & frozenset(STRONG_TIER_TYPES)
except Exception:  # pragma: no cover - 防御首启缺失
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "Shakeout", "ST", "LPS", "SC", "SOW_INVALID"})


def file_path() -> str:
    return PAPER_FILE


def load_state():
    """读取模拟盘状态; 文件不存在或损坏 → 全新默认账户。"""
    try:
        with open(PAPER_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("cash", float(_CUR["init_cash"]))
            st.setdefault("positions", [])   # 持仓: {symbol, name, qty, cost,
            st.setdefault("orders", [])      #       entry_ts, entry_bars, type, conf}
            st.setdefault("closed", [])      # 已平仓: {symbol, ..., buy_px, sell_px,
            st.setdefault("equity_hist", []) #        qty, ret, reason, type, close_ts}
            st.setdefault("candidates", [])  # 最新候选快照
            st.setdefault("pending", [])     # 等待下一根开盘买入的委托
            st.setdefault("meta", {})
            return st
    except Exception:
        pass
    return _new_state()


def _new_state():
    return {
        "cash": float(_CUR["init_cash"]),
        "positions": [],
        "orders": [],
        "closed": [],
        "equity_hist": [],
        "candidates": [],
        "pending": [],
        "meta": {},
    }


def save_state(st):
    """原子写盘。"""
    try:
        os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
        tmp = PAPER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, PAPER_FILE)
        return True
    except Exception:
        return False


# ── 选股: 全市场自动筛选 ────────────────────────────────
def pick_candidates(universe=None, max_codes=60, min_conf=None,
                    cancel_event=None):
    """扫描 universe 中触发强多头事件的高 conf 标的, 返回候选单 (降序 conf)。

    每只股票只留"最近 N 根内最新"的强多头事件; conf 由 event 的 conf 字段
    (启发式 + online model 校准 + 类型封顶后) 提供。universe 为空用全市场。
    min_conf 缺省取当前生效参数 (apply_paper_params 设置或模块默认 90)。
    """
    if min_conf is None:
        min_conf = _CUR["min_conf"]
    from .datasource import fetch_kline
    from .indicators import add_indicators, find_pivots
    from .events import detect_all
    from .utils import normalize_symbol
    from .fundamental import fetch_sector, fetch_market_universe

    if universe is None:
        try:
            universe = fetch_market_universe(100) or []
        except Exception:
            universe = []
    universe = [normalize_symbol(c) for c in universe]
    out = []
    for code in universe[:max_codes]:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            df = add_indicators(fetch_kline(code, datalen=400, scale=240),
                                symbol=code)
            if df is None or len(df) < 200:
                continue
            piv = find_pivots(df, order=6)
            evs = detect_all(df, piv)
            if not evs:
                continue
            # 仅在最近 10 根内、强多头、conf 达标的事件
            latest = None
            for e in evs:
                if e["type"] not in LONG_EVENT_TYPES:
                    continue
                if (e.get("idx") or 0) < len(df) - 10:
                    continue
                conf = int(e.get("conf", 0) or 0)
                if conf < min_conf:
                    continue
                if latest is None or (e.get("idx") or 0) > latest["idx"]:
                    latest = e
            if latest is None:
                continue
            base_conf = int(latest.get("conf", 0) or 0)
            latest["code"] = code
            latest["name"] = _stock_name(code)
            latest["last"] = round(float(df["close"].iloc[-1]), 2)
            latest["sector"] = ""
            try:
                latest["sector"] = fetch_sector(code) or ""
            except Exception:
                pass
            # A: 产业链加分/门禁 — 不改变 base_conf 的入池过滤, 只影响排序优先级;
            #    数据不可用/无板块 (离线) 时 adj=0 完全退化为原行为。
            try:
                if latest["sector"]:
                    from .chain import chain_conf_adjust
                    adj = chain_conf_adjust(latest["sector"], base_conf)
                    latest["base_conf"] = base_conf
                    latest["conf"] = max(min_conf, min(100, base_conf + adj))
                    latest["chain_adj"] = adj
            except Exception:
                pass
            out.append(latest)
        except Exception:
            continue
    out.sort(key=lambda e: -(int(e.get("conf", 0) or 0)))
    return out


def _stock_name(code):
    try:
        from .screener import _get_stock_name
        return _get_stock_name(str(code)[-6:])
    except Exception:
        return ""


# ── 撮合: 买入/卖出 ─────────────────────────────────────
def _next_open(code, datalen=420):
    """取最新一根日线 (模拟盘以收盘后运行, 下一根"开盘价"用最近收盘近似+滑点)。"""
    from .datasource import fetch_kline
    from .indicators import add_indicators
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


def _make_order(code, name, type_, conf, price, n_total, cash):
    """构造买单订单公共字段 (qty 按当前现金预算分配, 整手)。"""
    budget = cash * (1.0 / max(1, _CUR["max_pos"]))
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
    }


def place_buy_order(code, name, type_, conf, price, n_total, execute=True):
    """下单买入 (execute=True 直接按现价撮合成交; False 进 pending 待成交)。

    独立入口: 自行加载状态、校验持仓/同持上限/资金。返回 (order, msg)。
    """
    st = load_state()
    with _LOCK:
        if has_position(st, code):
            return None, "已持有"
        if len(st["positions"]) >= _CUR["max_pos"]:
            return None, "同持已满"
        order = _make_order(code, name, type_, conf, price, n_total, st["cash"])
        if order is None:
            return None, "金额不足一手"
        if execute:
            return fill_buy(st, order)
        st["pending"].append(order)
        save_state(st)
        return order, "已下单待撮合"


def _enqueue_buy(st, code, name, type_, conf, price):
    """(进程内) 仅计入 pending, 不校验不落盘。调用方负责校验与 save_state。"""
    order = _make_order(code, name, type_, conf, price, 0, st["cash"])
    if order is None:
        return None
    st["pending"].append(order)
    return order


def fill_buy(st, order):
    """口头成交: 扣现金、建仓。"""
    price = order["price"]
    qty = order["qty"]
    cost = qty * price * _CUR["cost"]
    spend = qty * price + cost
    st["cash"] -= spend
    st["positions"].append({
        "symbol": order["symbol"], "name": order.get("name", ""),
        "type": order["type"], "conf": order.get("conf", 50),
        "qty": qty, "buy_px": price, "cost": round(cost, 2),
        "entry_ts": order["ts"], "entry_bars": order.get("bars", 0),
        "staged": False,
    })
    st["orders"].append(order)
    save_state(st)
    return order, "成交"


def step(st, df_by_code):
    """推进一个周期: 用当前行情更新最新价 → 检查卖单条件 + 成交 in-arrow。

    df_by_code: {symbol: df(含 indicators)} 当前最新行情窗口 (由 UI/调度器提供)。
    不可用 (无行情) 时跳过。
    """
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        if df is None or len(df) == 0:
            continue
        last = float(df["close"].iloc[-1])
        entry = float(pos["buy_px"])
        ret = last / entry - 1
        pos["last"] = round(last, 3)
        pos["last_ret"] = round(ret, 4)
        # 结构位: 用最近 10 根低点做动态支撑 (近似结构关键位)
        support = float(df["low"].iloc[-10:].min())
        stop_px = entry * (1 - _CUR["stop_loss"])
        # 卖出判定 (任一触发); 每周期推进持仓 K 数 (run_cycle 末尾落盘)
        pos["entry_bars"] = int(pos.get("entry_bars", 0)) + 1
        reason = None
        held = int(pos["entry_bars"])
        if ret >= _CUR["take_profit"]:
            reason = "止盈"
        elif last <= stop_px:
            reason = "止损"
        elif support < stop_px and last <= support:
            reason = "破位"
        elif held >= _CUR["hold_bars"]:
            reason = "到期"
        if reason:
            sell_price = max(stop_px, last) * (1 - SLIP_SELL)
            close_position(st, pos, sell_price, reason)
    # 处理待撮合买单
    for o in list(st["pending"]):
        df = df_by_code.get(o["symbol"])
        if df is not None and len(df):
            close = float(df["close"].iloc[-1])
            o["price"] = round(close * (1 + SLIP_BUY), 3)
            if not o.get("date"):
                o["date"] = str(df["day"].iloc[-1])
            order = dict(o)
            st["pending"].remove(o)
            fill_buy(st, order)


def close_position(st, pos, sell_price, reason):
    """平仓: 回收现金、记录已平仓与净值。"""
    price = round(sell_price, 3)
    gross = price * pos["qty"]  # 不含卖出成本的口径内部用
    fee = gross * _CUR["cost"]
    proceeds = gross - fee
    st["cash"] += proceeds
    ret_total = (price / pos["buy_px"] - 1)
    st["closed"].append({
        "symbol": pos["symbol"], "name": pos.get("name", ""),
        "type": pos["type"], "conf": pos.get("conf", 50),
        "qty": pos["qty"], "buy_px": pos["buy_px"],
        "sell_px": price, "ret": round(ret_total, 4),
        "reason": reason, "entry_ts": pos["entry_ts"],
        "close_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bars": int(pos.get("entry_bars", 0)),
    })
    st["positions"] = [p for p in st["positions"] if p is not pos]
    st["equity_hist"].append({
        "ts": time.strftime("%Y-%m-%d"),
        "cash": round(st["cash"], 2),
        "equity": round(equity(st, {}), 2),
    })
    save_state(st)


def equity(st, df_by_code):
    """总资产 = 现金 + 持仓市值。"""
    mv = 0.0
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else pos.get("last", pos["buy_px"])
        mv += px * pos["qty"]
    return st["cash"] + mv


# ── 收益统计 ─────────────────────────────────────────────
def stats(st):
    """账户/策略统计。返回 dict (供 UI/报告)。"""
    closed = st["closed"]
    out = {
        "cash": round(st["cash"], 2),
        "n_positions": len(st["positions"]),
        "n_closed": len(closed),
        "n_orders": len(st["orders"]),
        "n_pending": len(st["pending"]),
        "total_return": 0.0,
        "win_rate": None,
        "pl_ratio": None,
        "avg_ret": None,
        "best": None,
        "worst": None,
        "max_drawdown": None,
        "by_type": {},
        "by_reason": {},
    }
    rets = [c["ret"] for c in closed if c.get("ret") is not None]
    if rets:
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out["win_rate"] = round(len(wins) / len(rets), 4)
        out["avg_ret"] = round(statistics.mean(rets), 4)
        out["best"] = round(max(rets), 4)
        out["worst"] = round(min(rets), 4)
        if losses:
            avg_win = statistics.mean(wins) if wins else 0.0
            out["pl_ratio"] = round(avg_win / abs(statistics.mean(losses)), 3)
        # 分类型
        by_type = {}
        for c in closed:
            t = c.get("type", "?")
            b = by_type.setdefault(t, {"n": 0, "rets": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
        for t, b in by_type.items():
            rs = b["rets"]
            b["avg"] = round(statistics.mean(rs), 4)
            b["win"] = round(sum(1 for r in rs if r > 0) / len(rs), 4)
            b.pop("rets", None)
        out["by_type"] = by_type
        # 平仓原因分布
        by_reason = {}
        for c in closed:
            r = c.get("reason", "?")
            by_reason.setdefault(r, {"n": 0, "avg": []})["n"] += 1
            by_reason[r]["avg"].append(c["ret"])
        for r, b in by_reason.items():
            b["avg"] = round(statistics.mean(b["avg"]), 4)
        out["by_reason"] = by_reason

    # 总收益率: 当前总资产 (现金+持仓市值) 相对初始模拟资金
    hist = st.get("equity_hist") or []
    init = float(_CUR["init_cash"])
    if hist:
        last_hist = hist[-1].get("equity", init)
        out["total_return"] = round(last_hist / init - 1, 4)
        # 最大回撤 (用净值曲线)
        if np is not None and len(hist) >= 2:
            eqs = [h.get("equity", init) for h in hist]
            arr = np.asarray(eqs, dtype=float)
            peak = np.maximum.accumulate(arr)
            dd = arr / peak - 1
            out["max_drawdown"] = round(float(dd.min()), 4)
    return out


def signal_stats_text(st):
    """Markdown 统计段 (报告导出用)。"""
    s = stats(st)
    L = []
    L.append("### 模拟盘收益统计")
    L.append("")
    L.append(f"- 总资产: **{s['cash']:,}** 当前持仓 {s['n_positions']} 只, "
             f"已平仓 {s['n_closed']} 笔, 订单 {s['n_orders']} 笔")
    L.append(f"- 累计收益: **{s['total_return']*100:+.2f}%**  "
             f"最大回撤: {f'{s['max_drawdown']*100:.2f}%' if s['max_drawdown'] is not None else '-'}")
    if s["win_rate"] is not None:
        L.append(f"- 胜率: **{s['win_rate']*100:.1f}%**  平均每笔: "
                 f"{s['avg_ret']*100:+.2f}%  盈亏比: {s['pl_ratio']}")
    if s["by_type"]:
        L.append("")
        L.append("| 事件 | 笔数 | 胜率 | 平均收益 |")
        L.append("|------|------|------|----------|")
        for t, b in sorted(s["by_type"].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| {t} | {b['n']} | {b['win']*100:.0f}% | "
                     f"{b['avg']*100:+.2f}% |")
    L.append("")
    return "\n".join(L)


def run_cycle(settings=None, min_conf=None, universe=None):
    """无头自动运行一个周期: 筛选→下单→步进→统计。返回统计。

    供 cron / 调度线程 / 手动触发。每周期持仓 K 数 +1,
    到期/止盈/止损/破位在该周期内平仓。
    settings 传入界面设置 dict (S.Paper.* 键) 覆盖策略参数; min_conf 显式传入
    时优先于 settings (兼容旧调用方)。
    """
    from .datasource import fetch_kline
    from .indicators import add_indicators

    apply_paper_params(settings)
    if min_conf is None:
        min_conf = _CUR["min_conf"]

    st = load_state()
    # 1) 选股
    cand = pick_candidates(universe=universe, min_conf=min_conf)
    st["candidates"] = cand
    # 2) 下单: 仓位未满时取候选填补 (同持上限内), 进 pending 待本周期撮合
    for e in cand:
        if len(st["positions"]) >= _CUR["max_pos"]:
            break
        code = e["code"]
        if has_position(st, code) or any(o["symbol"] == code for o in st["pending"]):
            continue
        px = float(e.get("last", 0) or 0)
        if px <= 0:
            continue
        _enqueue_buy(st, code, e.get("name", ""), e["type"],
                     e.get("conf", 50), px)
    # 3) 步进+平仓判定 (持仓 + 待撮合用最新行情)
    df_by_code = {}
    codes = {p["symbol"] for p in st["positions"]}
    codes |= {o["symbol"] for o in st["pending"]}
    for code in codes:
        try:
            df_by_code[code] = add_indicators(
                fetch_kline(code, datalen=420, scale=240), symbol=code)
        except Exception:
            pass
    step(st, df_by_code)
    save_state(st)
    return stats(st)