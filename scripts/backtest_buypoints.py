#!/usr/bin/env python3
"""威科夫完整做多买点 事件驱动回测。

对事件检测器一次性跑完整历史 (识别是因果的), 组装所有结构性买点, 然后用
每类买点的入场/止损/目标执行"止盈/止损/到期"出场模拟, 统计各买点类别
(归类: 低风险左侧/高盈亏比左侧/稳健主升启动/趋势中继加仓) 的胜率与收益。

用法:
    python scripts/backtest_buypoints.py                 # 内置股票池
    python scripts/backtest_buypoints.py --stocks sh600036 sz000001
    python scripts/backtest_buypoints.py --limit 30 --horizon 25 --out dir
"""
import argparse
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.buypoints import CLASS_META, KIND_META, struct_buy_points
from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots

DEFAULT_STOCKS = [
    "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
    "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
    "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
    "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
    "sh601166", "sz000725", "sh600028", "sh601088", "sz002230",
    "sh600809", "sz300059", "sh600585", "sh600009", "sz300015",
]


def _sim_trade(df, bp, horizon=20, cost=0.004):
    """按买点的入场/止损/目标模拟单笔多头交易 (决策 bar 次日起判)。"""
    bar = int(bp["bar_idx"])
    n = len(df)
    entry = float(bp["entry_price"])
    stop = float(bp["stop_price"])
    target = float(bp["target_price"]) if bp.get("target_price") else None
    if bar + horizon >= n:
        return None
    if entry <= 0 or stop <= 0 or entry <= stop:
        return None
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    end = bar + horizon
    exit_p = float(close[end])
    exit_j = end
    reason = "H"
    for j in range(bar + 1, end + 1):
        if target is not None and target > entry and high[j] >= target:
            exit_p, exit_j, reason = target, j, "TP"
            break
        if stop < entry and low[j] <= stop:
            exit_p, exit_j, reason = stop, j, "SL"
            break
    return {
        "kind": bp["kind"],
        "cls": bp["cls"],
        "conf": int(bp.get("conf", 50)),
        "rr": float(bp.get("rr", 2.0)),
        "position": int(bp.get("position", 2)),
        "entry": entry,
        "exit": float(exit_p),
        "reason": reason,
        "entry_idx": bar,
        "exit_idx": exit_j,
        "ret": float(exit_p / entry - 1) - cost,
    }


# 仓位档位 → 组合投入权重 (KIND_META position: 1小仓试探/2半仓/3加仓)
POSITION_WEIGHT = {1: 0.30, 2: 0.60, 3: 1.00}


def _portfolio_sim(trades, cap=1.00):
    """实盘化组合模拟: 按仓位档位定投入权重, 同时投入上限 cap (≤100%杠杆),
    超容量信号跳过 (同持上限纪律)。trades 的 entry_idx/exit_idx 即全局K线
    时间轴 (各股均 datalen 根, 索引对齐), 跨股票合并构建账户权益曲线。

    返回 (equity_points, stats) — equity 序列为每次平仓后的账户权益。
    相比"每信号全仓复利"口径, 该模型反映仓位档位+同持上限下的真实回撤。
    """
    events = []
    for t in trades:
        events.append((t["entry_idx"], 0, t))   # 开仓 (次优先)
        events.append((t["exit_idx"], 1, t))    # 平仓 (优先)
    events.sort(key=lambda e: (e[0], e[1], e[2]["entry"]))
    open_pos = []      # [{w, exit, ret}]
    used = 0.0
    equity = 1.0
    points = [(0, 1.0)]
    skipped = 0
    for _bar, is_close, t in events:
        if is_close:
            for p in list(open_pos):
                if p["exit"] == _bar:
                    equity *= (1 + p["w"] * p["ret"])
                    used -= p["w"]
                    open_pos.remove(p)
            points.append((_bar, equity))
        else:
            w = POSITION_WEIGHT.get(t["position"], 0.6)
            if used + w <= cap + 1e-9:
                open_pos.append({"w": w, "exit": t["exit_idx"],
                                 "ret": t["ret"]})
                used += w
            else:
                skipped += 1
    vals = np.array([p[1] for p in points])
    peak = np.maximum.accumulate(vals)
    dd = vals / peak - 1
    stats = {
        "n": len(trades),
        "skipped_over_cap": skipped,
        "total_return": round(float(equity - 1) * 100, 1),
        "max_dd": round(float(dd.min()) * 100, 1),
        "cap": cap,
    }
    return points, stats


def _stats(trades):
    rets = np.array([t["ret"] for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    cum = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    return {
        "n": len(trades),
        "win_rate": round(float((rets > 0).mean()) * 100, 1),
        "avg": round(float(rets.mean()) * 100, 2),
        "med": round(float(np.median(rets)) * 100, 2) if len(rets) else 0,
        "total_return": round(float(np.prod(1 + rets) - 1) * 100, 1),
        "max_dd": round(float(dd.min()) * 100, 1),
        "pf": round(float(np.abs(wins.sum() / losses.sum())), 2) if len(losses) and losses.sum() != 0 else None,
        "avg_win": round(float(wins.mean()) * 100, 2) if len(wins) else 0,
        "avg_loss": round(float(losses.mean()) * 100, 2) if len(losses) else 0,
        "tp": sum(1 for t in trades if t["reason"] == "TP"),
        "sl": sum(1 for t in trades if t["reason"] == "SL"),
    }


def backtest(stocks, datalen=500, horizon=20, cost=0.004):
    all_trades = []
    per_stock = {}
    for code in stocks:
        try:
            df = fetch_kline(code, datalen=datalen, scale=240)
            if len(df) < 150:
                continue
            df = add_indicators(df, symbol=code)
            pv = find_pivots(df, order=6)
            ev = detect_all(df, pv)
            bps = struct_buy_points(df, ev, pv)
            trades = [_sim_trade(df, b) for b in bps]
            trades = [t for t in trades if t and t["reason"] != ""]
            all_trades.extend(trades)
            per_stock[code] = len(trades)
        except Exception as e:
            print(f"  ! {code}: {e}", flush=True)
    return all_trades, per_stock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--datalen", type=int, default=500)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--cost", type=float, default=0.004)
    ap.add_argument("--out", default="buypoint_backtest_data")
    args = ap.parse_args()

    stocks = args.stocks or DEFAULT_STOCKS
    if args.limit:
        stocks = list(dict.fromkeys(DEFAULT_STOCKS + (stocks or []) ))[:args.limit]

    print("=== 威科夫完整做多买点回测 ===")
    print(f"股票: {len(stocks)} 只 | 数据: {args.datalen} 根 | 持有: {args.horizon} | 成本: {args.cost}")
    trades, per_stock = backtest(stocks, args.datalen, args.horizon, args.cost)
    print(f"\n总交易: {len(trades)} 笔")

    by_kind = {}
    by_cls = {}
    for t in trades:
        by_kind.setdefault(t["kind"], []).append(t)
        by_cls.setdefault(t["cls"], []).append(t)

    print("\n── 按买点种类 ──")
    rows = []
    for kind in sorted(KIND_META, key=lambda k: len(by_kind.get(k, [])), reverse=True):
        if not by_kind.get(kind):
            continue
        s = _stats(by_kind[kind])
        rows.append((KIND_META[kind][0], s, KIND_META[kind][1]))
        print(f"  {KIND_META[kind][0]:<18} n={s['n']:>4} 胜率={s['win_rate']:>5.1f}% "
              f"均收益={s['avg']:>6.2f}% 累计={s['total_return']:>8.1f}% PF={s['pf']} "
              f"TP={s['tp']} SL={s['sl']}")

    print("\n── 按实战归类 ──")
    cls_stats = {}
    for cls in CLASS_META:
        if not by_cls.get(cls):
            continue
        s = _stats(by_cls[cls])
        cls_stats[cls] = s
        print(f"  {CLASS_META[cls][0]:<12} n={s['n']:>4} 胜率={s['win_rate']:>5.1f}% "
              f"均收益={s['avg']:>6.2f}% 累计={s['total_return']:>8.1f}% PF={s['pf']}")

    if not trades:
        print("\n样本为 0, 终止。")
        return

    points, pf_stats = _portfolio_sim(trades)
    print("\n── 实盘化组合模拟 (仓位档位×同持上限, 容量100%) ──")
    print(f"  交易 {pf_stats['n']} 笔  超容量跳过 {pf_stats['skipped_over_cap']} 笔")
    print(f"  账户累计收益 (复利) = {pf_stats['total_return']}%  最大回撤 = {pf_stats['max_dd']}%")
    print("  对照: 全信号全仓复利口径的最大回撤最高达 -34.7% (高盈亏比左侧)")

    os.makedirs(args.out, exist_ok=True)
    fn = os.path.join(args.out, f"buypoint_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    report = {
        "generated_at": datetime.now().isoformat(),
        "stocks": len(stocks),
        "datalen": args.datalen,
        "horizon": args.horizon,
        "cost": args.cost,
        "total_trades": len(trades),
        "per_stock": per_stock,
        "portfolio": pf_stats,
        "by_kind": {k: _stats(v) for k, v in by_kind.items()},
        "by_class": cls_stats,
    }
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {fn}")


if __name__ == "__main__":
    main()
