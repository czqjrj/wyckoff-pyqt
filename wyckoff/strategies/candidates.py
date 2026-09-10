"""威科夫策略管理器 · 模拟盘候选插件表与统一选股入口。

拆分自 manager.py: 模拟盘一切候选统一经 scan_individual 产出 (paper.py 不内置
选股逻辑)。新增策略只需注册 key/中文名/producer 函数/是否受门禁, 扫描流水线
与优先序逻辑无需改动 (插件化)。

门禁判定收敛于 wyckoff.discipline (经 evaluators.check_discipline_gates,
scan_individual 仅按 market_ok 预判做拦截, 与并行的 paper 口径一致)。
"""

from wyckoff.strategies.constants import (
    DISCIPLINE_EVENT_WINDOW,
    LONG_EVENT_TYPES,
    LONG_MIN_CONF,
    STRATEGY_DISCIPLINE,
    STRATEGY_LONG_LEFT,
    STRATEGY_VALUE_ACC,
    VA_EXCLUDE_BJ,
    VA_EXCLUDE_ST,
    VA_MIN_CONF,
    VA_MIN_PRICE,
)
from wyckoff.strategies.evaluators import evaluate_strategy_value_accumulation

# ── 模拟盘策略注册信息 (选股策略的单一来源) ──────────────────────
# 优先序: 纪律 > 价值吸筹 > 左侧买点 (回测期望 4.87% / 3.21% / 2.52%,
# 命中20 81% / 75% / 67%; 左侧不受门禁, 弱市仍可兜底入场)
STRATEGY_ORDER = (STRATEGY_DISCIPLINE, STRATEGY_LONG_LEFT)
STRATEGY_CN = {
    STRATEGY_DISCIPLINE: "策略4·纪律",
    STRATEGY_VALUE_ACC: "价值吸筹",
    STRATEGY_LONG_LEFT: "威科夫左侧买点",
}

# 各策略是否受大盘门禁管束 (左侧买点属独立赛道, 诞生于大盘弱市, 不受门禁)
CANDIDATE_GATED = {
    STRATEGY_DISCIPLINE: True,
    STRATEGY_VALUE_ACC: True,
    STRATEGY_LONG_LEFT: False,
}


def is_low_quality(code, price=None, name=None) -> bool:
    """判断标的是否属低质池: 北交所 / ST·退市 / 低价 (可选)。"""
    c = str(code).lower()
    if VA_EXCLUDE_BJ and c.startswith("bj"):
        return True
    if VA_EXCLUDE_ST:
        nm = name or ""
        if any(x in nm for x in ("ST", "退", "N ", "C ")):
            return True
    if price is not None and VA_MIN_PRICE > 0 and float(price) < VA_MIN_PRICE:
        return True
    return False


def discipline_latest(evs, n, min_conf=90, event_types=None):
    """纪律口径: 最近 N 根内的最新强多头事件 (conf≥min_conf)。"""
    event_types = event_types if event_types is not None else LONG_EVENT_TYPES
    latest = None
    for e in evs or []:
        if e.get("type") not in event_types:
            continue
        if (e.get("idx") or 0) < n - DISCIPLINE_EVENT_WINDOW:
            continue
        conf = int(e.get("conf", 0) or 0)
        if conf < min_conf:
            continue
        if latest is None or (e.get("idx") or 0) > latest["idx"]:
            latest = e
    return latest


def value_accum_candidate(code, df, evs, piv, name=""):
    """策略管理器·价值吸筹候选 (底部整固 + 20根内吸筹事件, conf 下限)。"""
    try:
        sig = evaluate_strategy_value_accumulation(df, len(df) - 1, evs, piv)
    except Exception:
        return None
    if not sig:
        return None
    ev = sig["event"]
    if int(ev.get("conf", 0) or 0) < VA_MIN_CONF:
        return None
    if is_low_quality(code, name=name):
        return None
    return {"strategy": STRATEGY_VALUE_ACC,
            "type": ev["type"], "idx": int(ev.get("idx") or 0),
            "conf": int(ev.get("conf", 0) or 0)}


def left_buy_candidate(code, df, evs, piv, name=""):
    """威科夫完整做多买点·左侧起仓 (独立赛道, 自带入场/止损/目标/盈亏比)。

    只取可执行的左侧买点 (近 look 根、price 在入场~目标之间), 最优一条打包。
    右侧 (突破回踩 BU/LPS) 需先左侧起仓再加仓, 一期只接入左侧起仓。
    不受 "大盘站上 MA20" 全局门禁管束 (左侧买点诞生于大盘弱市)。
    """
    from wyckoff.buypoints import CLASS_META, KIND_LEFT, latest_buy_points
    try:
        bps = latest_buy_points(df, evs, piv)
    except Exception:
        return None
    if not bps:
        return None
    left = [b for b in bps if b["kind"] in KIND_LEFT]
    if not left:
        return None
    left.sort(key=lambda b: (CLASS_META[b["cls"]][1], b["rr"], b["conf"]),
              reverse=True)
    b = left[0]
    conf = int(b.get("conf", 50) or 50)
    if conf < LONG_MIN_CONF:
        return None
    if is_low_quality(code, price=float(b["entry_price"]), name=name):
        return None
    return {
        "strategy": STRATEGY_LONG_LEFT,
        "type": b["type_label"] if isinstance(b.get("type_label"), str)
                else b["label"],
        "idx": int(b["bar_idx"]),
        "conf": conf,
        "kind": b["kind"],
        "entry_price": float(b["entry_price"]),
        "stop_price": float(b["stop_price"]),
        "target_price": float(b["target_price"]) if b.get("target_price") else None,
        "rr": float(b.get("rr", 2.0)),
        "position": int(b.get("position", 2)),
        "note": b.get("msg", ""),
    }


# ── 候选 producer 注册表 (每个策略一个 producer, 输入 ctx 输出候选或 None) ──
def _produce_discipline(ctx):
    latest = discipline_latest(ctx["evs"], ctx["n"], ctx["min_conf"],
                               ctx["event_types"])
    if latest is None:
        return None
    return {"type": latest["type"], "idx": int(latest.get("idx") or 0),
            "conf": int(latest.get("conf", 0) or 0)}


def _produce_left_buy(ctx):
    return left_buy_candidate(ctx["symbol"], ctx["df"], ctx["evs"], ctx["piv"],
                              name=ctx["name"])


def _produce_value_acc(ctx):
    return value_accum_candidate(ctx["symbol"], ctx["df"], ctx["evs"], ctx["piv"],
                                 name=ctx["name"])


CANDIDATE_PRODUCERS = {
    STRATEGY_DISCIPLINE: _produce_discipline,
    STRATEGY_LONG_LEFT: _produce_left_buy,
    STRATEGY_VALUE_ACC: _produce_value_acc,
}


def scan_individual(code, df=None, min_conf=90, gates_ok=None,
                    name="", event_types=None, strategies=None):
    """对单只股票按优先序产出模拟盘候选 (纪律→价值吸筹→左侧买点)。

    这是模拟盘选股在管理器中的唯一实现; paper.py 不再内置任何选股逻辑。
    策略优先序由 STRATEGY_ORDER 驱动 (回测期望由高到低), 是否受大盘门禁
    由 CANDIDATE_GATED 声明; 新增策略只需注册 producer 无需改动扫描逻辑。

    event_types: 纪律口径的强多头事件集 (默认 LONG_EVENT_TYPES;
                 paper.py 可传其实证收紧后的 {Spring,ST,LPS})。
    gates_ok: 大盘门禁预判 (all_pass, reason) 或 None (默认视为通过)。
    strategies: 可选策略 key 子集 (如 ("long_buy_left",)); None/空表示全策略
                按 STRATEGY_ORDER 并线。用于「单策略扫描」模式, 避免优先序
                掩盖低优先级策略的候选。
    返回: 候选 dict (含 "gated": 是否受板块/资金流门禁管束) 或 None。
    """
    # 数据源/指标模块在调用时按属性解析 (单测会 monkeypatch 模块属性),
    # 故不用模块级 import 绑定, 与历史行为一致。
    from wyckoff.datasource import fetch_kline
    from wyckoff.events import detect_all
    from wyckoff.indicators import add_indicators, find_pivots
    from wyckoff.utils import normalize_symbol

    symbol = normalize_symbol(code)
    if df is None:
        df = add_indicators(fetch_kline(symbol, datalen=400, scale=240),
                            symbol=symbol)
    if df is None or len(df) < 200:
        return None
    piv = find_pivots(df, order=6)
    evs = detect_all(df, piv)
    market_ok = bool(gates_ok[0] if gates_ok else True)
    ctx = {
        "symbol": symbol,
        "df": df,
        "evs": evs,
        "piv": piv,
        "name": name,
        "n": len(df),
        "min_conf": min_conf,
        "event_types": event_types if event_types is not None else LONG_EVENT_TYPES,
    }
    order = STRATEGY_ORDER if not strategies else \
        [k for k in STRATEGY_ORDER if k in strategies]
    for key in order:
        cand = CANDIDATE_PRODUCERS[key](ctx)
        if cand is None:
            continue
        cand["strategy"] = key
        cand["gated"] = gated = CANDIDATE_GATED.get(key, True)
        if gated and not market_ok:
            continue
        return cand
    return None
