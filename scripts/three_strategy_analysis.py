#!/usr/bin/env python3
"""三策略统一回测分析 (策略4·纪律 / 威科夫左侧买点 / 价值吸筹).

对样本股票池的完整历史逐信号评估, 输出三口径统计:
  1. 信号频率: 每股平均信号数 + 每 1000 根信号数;
  2. 预测准确度: 信号 bar 收盘 → 5/10/20 根后收盘收益, 多头 ret>0 记命中
     (与 paper_strategy_accuracy 方向化命中口径一致);
  3. 盈利能力 (简单规则回测, 与模拟盘一致的出场纪律):
       - 纪律/价值吸筹: 次日开盘买入, 固定止损 3% / 止盈 15% / 成本 0.4%,
         持有 ≤20 根到期, 尾日收盘平仓;
       - 左侧买点: 按买点自带 entry/stop/target (below 挂单回踩触发),
         同规则 (止损/止盈/成本/持有上限)。
  另输出事件类型 / conf 段 / 个股 细分, 供优化方案定位信号子集。

判据复用策略管理器: candidates.discipline_latest、evaluators.
evaluate_strategy_value_accumulation、buypoints.struct_buy_points
(与模拟盘 scan_individual 同口径)。

用法:
  python scripts/three_strategy_analysis.py
  python scripts/three_strategy_analysis.py --datalen 500 --min-conf 90
  python scripts/three_strategy_analysis.py --codes sh600519,sz000001
  python scripts/three_strategy_analysis.py --report docs/three_strategy_analysis.md
"""
import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault(
    "WYCKOFF_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "paper_replay_data"),
)

import pandas as pd  # noqa: E402

from wyckoff.datasource import fetch_kline  # noqa: E402
from wyckoff.events import detect_all  # noqa: E402
from wyckoff.indicators import add_indicators, find_pivots  # noqa: E402
from wyckoff.strategies.evaluators import evaluate_strategy_value_accumulation  # noqa: E402

# 纪律口径与模拟盘收紧事件集一致 (paper.LONG_EVENT_TYPES = {Spring, ST, LPS})
DISCIPLINE_EVENT_TYPES = frozenset({"Spring", "ST", "LPS"})
# 左侧起仓类目 (buypoints.KIND_LEFT)
LEFT_KINDS = frozenset({"st_bottom", "lps", "spring", "spring_retest"})
# 价值吸筹/纪律事件冷却 (根): 同事件类型在窗内只录取一条信号
COOLDOWN = {"paper_discipline_bull": 10, "screener_value_accumulation": 20}

DEFAULT_CODES = (
    "sh600519,sz000001,sh600036,sh601318,sz000858,sh600887,sz000333,sh600900,"
    "sz300750,sz002594,sh688981,sz002415,sh600030,sh601166,sh600276,sz002475,"
    "sh601012,sz000568,sh600809,sh603288,sz000651,sh601888,sh600585,sz002304"
)


# ── 数据与信号生成 ─────────────────────────────────────────
def load_stock(code, datalen):
    df = add_indicators(fetch_kline(code, datalen=datalen, scale=240), symbol=code)
    if df is None or len(df) < 120:
        return None
    piv = find_pivots(df, order=6)
    evs = detect_all(df, piv)
    return {"code": code, "df": df, "piv": piv, "evs": evs or [],
            "close": df["close"].astype(float).values,
            "open": df["open"].astype(float).values,
            "high": df["high"].astype(float).values,
            "low": df["low"].astype(float).values,
            "day": list(df["day"])}


def _cooldown_ok(last_seen, type_, idx, cooldown):
    prev = last_seen.get(type_)
    return prev is None or (idx - prev) >= cooldown


def discipline_signals(rec, min_conf, window):
    """纪律信号: 每条合格事件一条 (同事件类型冷却 window 根)。"""
    out = []
    last_seen = {}
    evs = sorted(rec["evs"], key=lambda e: e.get("idx", 0))
    for e in evs:
        t = e.get("type")
        idx = int(e.get("idx", 0) or 0)
        if t not in DISCIPLINE_EVENT_TYPES:
            continue
        conf = int(e.get("conf", 0) or 0)
        if conf < min_conf:
            continue
        if idx < 0 or idx >= len(rec["df"]):
            continue
        if not _cooldown_ok(last_seen, t, idx, window):
            continue
        last_seen[t] = idx
        out.append({"strategy": "paper_discipline_bull", "type": t,
                    "idx": idx, "conf": conf})
    return out


_va_cache = {}


def value_signals(rec, horizon):
    """价值吸筹信号: 逐 bar (事件预筛) 复用模拟盘判据, 同事件类型冷却 20 根。"""
    out = []
    last_seen = {}
    df = rec["df"]
    evs = rec["evs"]
    n = len(df)
    for j in range(90, n - horizon):
        # 预筛: 近20根内有无吸筹事件 (避免对整段历史跑阶段判定)
        if not any(int(e.get("idx") or -1) >= j - 20 for e in evs):
            continue
        key = (id(rec), j)
        hit = _va_cache.get(key, "MISS")
        if hit == "MISS":
            try:
                w = df.iloc[:j + 1]
                sig = evaluate_strategy_value_accumulation(w, j, evs, rec["piv"])
            except Exception:
                sig = None
            _va_cache[key] = sig
        else:
            sig = hit
        if not sig:
            continue
        ev = sig["event"]
        etype = ev["type"]
        eidx = int(ev.get("idx") or 0)
        this = eidx
        if not _cooldown_ok(last_seen, etype, this, COOLDOWN["screener_value_accumulation"]):
            continue
        last_seen[etype] = this
        out.append({"strategy": "screener_value_accumulation",
                    "type": etype, "idx": this,
                    "conf": int(ev.get("conf", 0) or 0)})
    return out


def left_signals(rec):
    """左侧买点信号: 全历史结构买点中 KIND_LEFT 类目 (买点自带 entry/stop/target)。"""
    from wyckoff.buypoints import struct_buy_points
    out = []
    try:
        bps = struct_buy_points(rec["df"], rec["evs"], rec["piv"]) or []
    except Exception:
        return out
    for b in bps:
        if b.get("kind") not in LEFT_KINDS:
            continue
        out.append({"strategy": "long_buy_left", "type": b.get("type_label")
                    if isinstance(b.get("type_label"), str) else b.get("label", b.get("kind")),
                    "kind": b["kind"], "idx": int(b["bar_idx"]),
                    "conf": int(b.get("conf", 50) or 50),
                    "entry": float(b["entry_price"]), "stop": float(b["stop_price"]),
                    "target": float(b["target_price"]) if b.get("target_price") else None})
    return out


def all_signals(rec, min_conf, window, horizon, include_order):
    by = {}
    if "paper_discipline_bull" in include_order:
        by["paper_discipline_bull"] = discipline_signals(rec, min_conf, window)
    if "screener_value_accumulation" in include_order:
        by["screener_value_accumulation"] = value_signals(rec, horizon)
    if "long_buy_left" in include_order:
        by["long_buy_left"] = left_signals(rec)
    return by


# ── 逐信号评估 ────────────────────────────────────────────
def eval_signal(rec, sig, stop_pct, tp_pct, cost, hold, horizon):
    """返回 {hit:{h:ret}, trade:{ret, bars, reason, win, entry, exit}} 或尾截跳过。"""
    n = len(rec["df"])
    si = sig["idx"]
    if si + 1 >= n:
        return None
    close = rec["close"]
    # 方向准确度 (信号 bar 收盘 → 5/10/20 根后, 尾段不足则缺; 与 accuracy 面板一致)
    hits = {}
    for h in (5, 10, 20):
        if si + h >= n or close[si] <= 0:
            continue
        hits[str(h)] = round(float(close[si + h] / close[si] - 1), 6)
    # 盈利能力 (规则回测)
    t0 = si + 1
    if sig["strategy"] == "long_buy_left":
        entry = sig["entry"]
        stop = sig["stop"]
        target = sig["target"]
    else:
        entry = float(rec["open"][t0])
        if entry <= 0:
            among = rec["close"][si:]
            entry = float(among[pd.notna(among)][0]) if len(among) else entry
        stop = entry * (1 - stop_pct)
        target = entry * (1 + tp_pct) if tp_pct else None
    ret = None
    bars = 0
    reason = "到期"
    end = min(n - 1, si + hold)
    for j in range(t0, end + 1):
        bx = int(bars + 1)
        high = float(rec["high"][j])
        low = float(rec["low"][j])
        if target is not None and high >= target and stop is not None and low <= stop:
            ret, reason, bars = (stop / entry - 1, "止损", bx)
            break
        if target is not None and high >= target:
            ret, reason, bars = (target / entry - 1, "止盈", bx)
            break
        if low <= stop:
            ret, reason, bars = (stop / entry - 1, "止损", bx)
            break
        bars = bx
    if ret is None:
        exitpx = float(rec["close"][end])
        ret = exitpx / entry - 1
        bars = end - t0 + 1
    ret -= cost
    return {"hits": hits, "trade": {
        "ret": ret, "bars": bars, "reason": reason,
        "win": ret > 0, "entry": entry, "type": sig.get("type"),
        "conf": sig.get("conf"), "kind": sig.get("kind")}}


# ── 聚合 ──────────────────────────────────────────────────
def _mean(xs):
    return round(statistics.mean(xs), 6) if xs else None


def _hit(xs):
    return round(sum(1 for v in xs if v > 0) / len(xs), 4) if xs else None


def aggregate(sigs_by_stock, trades, signame):
    """sigs_by_stock: list[(rec, sig)] 已评估; trades: [dict|None]。"""
    n = len(sigs_by_stock)
    rel = [s for (r, s), t in zip(sigs_by_stock, trades) if t]
    n_eval = len(rel)
    rets = [t["trade"]["ret"] for t in trades if t]
    bars = [t["trade"]["bars"] for t in trades if t]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    cum = 1.0
    for r in rets:
        cum *= 1.0 + r
    hits = {h: [t["hits"][h] for t in trades if t and h in t["hits"]] for h in ("5", "10", "20")}
    return {
        "strategy": signame, "n": n, "n_eval": n_eval,
        "hit5": _hit(hits["5"]), "hit10": _hit(hits["10"]), "hit20": _hit(hits["20"]),
        "avg5": _mean(hits["5"]), "avg10": _mean(hits["10"]), "avg20": _mean(hits["20"]),
        "win_rate": round(len(wins) / len(rets), 4) if rets else None,
        "avg_ret": _mean(rets), "cum_ret": round(cum - 1, 4) if rets else None,
        "pl_ratio": round(statistics.mean(wins) / abs(statistics.mean(losses)), 3)
                    if wins and losses else None,
        "expectancy": round((len(wins) / len(rets)) * (statistics.mean(wins) if wins else 0)
                            - (1 - len(wins) / len(rets)) * (abs(statistics.mean(losses)) if losses else 0), 4)
                    if rets else None,
        "avg_bars": round(statistics.mean(bars), 1) if bars else None,
        "freq_per_stock": round(n / n_stock(), 2) if n else 0,
    }


# 全局股票数 (供频率统计)
_N_STOCK = [0]


def n_stock():
    return _N_STOCK[0] or 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--codes", default=DEFAULT_CODES,
                    help="逗号分隔股票代码")
    ap.add_argument("--datalen", type=int, default=500)
    ap.add_argument("--min-conf", type=int, default=90, help="纪律 conf 下限")
    ap.add_argument("--window", type=int, default=10, help="纪律事件近端窗口")
    ap.add_argument("--horizon", type=int, default=20, help="持有/评估周期 (根)")
    ap.add_argument("--stop", type=float, default=0.03)
    ap.add_argument("--tp", type=float, default=0.15)
    ap.add_argument("--cost", type=float, default=0.004)
    ap.add_argument("--strategy", default="all",
                    choices=["all", "paper_discipline_bull",
                             "screener_value_accumulation", "long_buy_left"])
    ap.add_argument("--report", default=None, help="输出 Markdown 报告路径")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    include_order = ["paper_discipline_bull", "long_buy_left",
                     "screener_value_accumulation"]
    if args.strategy != "all":
        include_order = [args.strategy]

    stocks = []
    failed = []
    for c in codes:
        rec = load_stock(c, args.datalen)
        if rec is None:
            failed.append(c)
            continue
        stocks.append(rec)
    _N_STOCK[0] = len(stocks)
    print(f"股票池: {len(stocks)} 只 (失败 {len(failed)} {failed})")

    sigs = {s: [] for s in include_order}
    trades = {s: [] for s in include_order}
    n_signals_all = {s: 0 for s in include_order}
    for rec in stocks:
        by = all_signals(rec, args.min_conf, args.window, args.horizon,
                         include_order)
        for s in include_order:
            for sig in by.get(s, []):
                n_signals_all[s] += 1
    print("信号数(全量): " + ", ".join(f"{s}={n_signals_all[s]}" for s in include_order))

    for rec in stocks:
        by = all_signals(rec, args.min_conf, args.window, args.horizon,
                         include_order)
        for s in include_order:
            for sig in by.get(s, []):
                t = eval_signal(rec, sig, args.stop, args.tp, args.cost,
                                args.horizon, args.horizon)
                sigs[s].append((rec, sig))
                trades[s].append(t)

    # 汇总表
    print("\n=== 三策略汇总 ===")
    rows = []
    for s in include_order:
        agg = aggregate(sigs[s], trades[s], s)
        rows.append(agg)
        print(f"{s:26s} n={agg['n']:4d} | 命中5/10/20={agg['hit5']} {agg['hit10']} {agg['hit20']} "
              f"| 均值20={agg['avg20']} | 回测胜率={agg['win_rate']} 平均={agg['avg_ret']} "
              f"累计={agg['cum_ret']} 盈亏比={agg['pl_ratio']} 期望={agg['expectancy']} "
              f"持={agg['avg_bars']}")

    # 事件类型细分
    print("\n=== 事件/类目细分明细 (命中20根 与 回测胜率) ===")
    detail = {}
    for s in include_order:
        by_t = {}
        for (rec, sig), t in zip(sigs[s], trades[s]):
            k = sig.get("type") or sig.get("kind") or "-"
            if t:
                d = by_t.setdefault(k, {"h": [], "r": []})
                d["h"].append(t["hits"].get("20"))
                d["r"].append(t["trade"]["ret"])
        detail[s] = by_t
        for k in sorted(by_t, key=lambda x: -len(by_t[x]["r"])):
            d = by_t[k]
            hs = [h for h in d["h"] if h is not None]
            r = d["r"]
            wr = round(sum(1 for v in r if v > 0) / len(r), 3)
            avg = round(statistics.mean(r), 4)
            hit = round(sum(1 for v in hs if v > 0) / len(hs), 3) if hs else None
            print(f"  {s:26s} {k:14s} n={len(r):3d} hit20={hit} 回测胜率={wr} 平均={avg}")

    # conf 段 (纪律)
    if "paper_discipline_bull" in include_order:
        print("\n=== 纪律 conf 段细分 ===")
        conf_bins = {"90-99": (90, 100), "100-109": (100, 110), ">=110": (110, 10 ** 9)}
        for lbl, (lo, hi) in conf_bins.items():
            rl = []
            for (rec, sig), t in zip(sigs["paper_discipline_bull"],
                                     trades["paper_discipline_bull"]):
                if t and lo <= (sig.get("conf") or 0) < hi:
                    rl.append(t["trade"]["ret"])
            if rl:
                wr = round(sum(1 for v in rl if v > 0) / len(rl), 3)
                avg = round(statistics.mean(rl), 4)
                print(f"  conf {lbl:8s} n={len(rl):3d} 回测胜率={wr} 平均={avg}")

    # 个股信号数
    print("\n=== 个股信号数 ===")
    for rec in stocks:
        by = all_signals(rec, args.min_conf, args.window, args.horizon,
                         include_order)
        line = f"  {rec['code']}: " + "  ".join(f"{s.split('_')[-1]}={len(by.get(s, []))}"
                                               for s in include_order)
        print(line)

    if args.report:
        _write_report(args.report, args, rows, detail, stocks, include_order)
        print(f"\n报告已写入: {args.report}")


def _write_report(path, args, rows, detail, stocks, include_order):
    L = ["# 三策略回测分析报告", "",
         "- 生成时间: 2026-09-06 自动回测",
         f"- 股票池: {len(stocks)} 只 · datalen={args.datalen} · "
         f"纪律 min_conf={args.min_conf} · 窗口={args.window}根",
         f"- 出场纪律: 纪律/价值=止损{args.stop*100:.0f}%/止盈{args.tp*100:.0f}% + 成本{args.cost} 持≤{args.horizon}根; "
         f"左侧=买点自带 entry/stop/target",
         "",
         "| 策略 | 信号数 | 命中5 | 命中10 | 命中20 | 均值20 | 回测胜率 | 平均 | 累计 | 盈亏比 | 期望 | 持有根数 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    def _pct(v):
        return f"{v*100:.0f}%" if v is not None else "-"

    def _pct_signed(v):
        return f"{v*100:+.2f}%" if v is not None else "-"

    for r in rows:
        L.append(f"| {r['strategy']} | {r['n']} | {_pct(r['hit5'])} | {_pct(r['hit10'])} | "
                 f"{_pct(r['hit20'])} | {_pct_signed(r['avg20'])} | {_pct(r['win_rate'])} | "
                 f"{_pct_signed(r['avg_ret'])} | {_pct_signed(r['cum_ret'])} | {r['pl_ratio']} | "
                 f"{r['expectancy']} | {r['avg_bars']} |")
    L += ["", "> 命中=信号后 N 根方向化命中 (多头 ret>0); 回测=固定止盈止损+成本规则平仓。", ""]
    L.append("## 事件/类目细分")
    L += ["| 策略 | 类型 | n | 命中20 | 回测胜率 | 平均 |", "|---|---|---|---|---|---|"]
    for s in include_order:
        for k in sorted(detail[s], key=lambda x: -len(detail[s][x]["r"])):
            d = detail[s][k]
            hs = [h for h in d["h"] if h is not None]
            r = d["r"]
            wr = round(sum(1 for v in r if v > 0) / len(r), 3) if r else None
            avg = round(statistics.mean(r), 4) if r else None
            hit = round(sum(1 for v in hs if v > 0) / len(hs), 3) if hs else None
            f0 = _pct
            f1 = _pct_signed
            L.append(f"| {s} | {k} | {len(r)} | {f0(hit)} | {f0(wr)} | {f1(avg)} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
