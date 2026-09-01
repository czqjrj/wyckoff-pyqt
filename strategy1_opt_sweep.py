#!/usr/bin/env python3
"""策略1 条件优化扫描 - 对比当前条件与收紧候选条件的回测表现

对多只股票上的策略1信号, 应用策略自带交易纪律 (同持3/持20K/-5%止损/+15%止盈/结构破位)
来评估在当前条件与优化条件下的实战价值 (胜率/平均收益/盈亏比/总收益)。
"""
import sys
import os
import json
import warnings
warnings.filterwarnings('ignore')

import numpy as np
from datetime import datetime

sys.path.append('.')
from wyckoff.datasource import fetch_kline
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify
from wyckoff_strategies_manager import WyckoffStrategyManager


STOCKS = [
    "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
    "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
    "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
    "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
    "sh601166", "sz000725", "sh600028", "sh601088", "sz002230",
    "sh600809", "sz300059", "sh600585",
    "sz300015", "sh601888", "sz002475", "sh600809", "sh601601",
    "sz000063", "sh600031", "sz002304", "sh601668", "sh600585",
    "sz002714", "sh600438", "sz300124", "sh601636", "sz000425",
    "sh600009", "sz002460", "sh600886",
]
STOCKS = list(dict.fromkeys(STOCKS))


def make_trade(res, df, i, horizon=20, cost=0.004):
    """按策略1自带交易纪律模拟一笔多头交易(用结构破位=硬止损优先, 止盈先触发)"""
    tp = res["trading"]["take_profit"]
    sl = res["trading"]["stop_loss"]
    entry_idx = min(i + 1, len(df) - 1)
    entry = df["open"].iloc[entry_idx]
    if entry <= 0:
        return None
    exit_idx = min(entry_idx + horizon, len(df) - 1)
    exit_p = df["close"].iloc[exit_idx]
    stop_price = entry * (1 - sl)
    for j in range(entry_idx + 1, exit_idx + 1):
        if df["high"].iloc[j] >= entry * (1 + tp):
            exit_p = entry * (1 + tp)
            exit_idx = j
            break
        if df["low"].iloc[j] <= stop_price:
            exit_p = stop_price
            exit_idx = j
            break
    ret = (exit_p / entry - 1) - cost
    return ret


def collect_trades(params):
    manager = WyckoffStrategyManager()
    trades = []
    for symbol in STOCKS:
        df = fetch_kline(symbol, datalen=500, scale=240)
        if len(df) < 160:
            continue
        seen = set()
        for i in range(90, len(df) - 30, 5):
            wdf = df.iloc[:i + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            wp = find_pivots(wdf, order=6)
            we = detect_all(wdf, wp)
            nt = nine_tests(wdf, we, wp)
            vsa = vsa_classify(wdf, scale=240)
            res = manager.evaluate_strategy_1(df, i, we, nt, vsa, params=params)
            if not res:
                continue
            ev = res.get("event", {})
            key = (res["strategy"], ev.get("idx"))
            if key in seen:
                continue
            seen.add(key)
            r = make_trade(res, df, i)
            if r is not None:
                trades.append(r)
    return trades


def summarize(label, trades):
    if not trades:
        print(f"{label:<46} n=0")
        return None
    rets = np.array(trades)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    cum = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    pf = np.abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    print(f"{label:<46} n={len(trades):<4} 胜率={(rets>0).mean()*100:5.1f}% "
          f"平均={rets.mean()*100:6.2f}% 总收益={(np.prod(1+rets)-1)*100:7.2f}% "
          f"盈比={pf:6.2f} 最大回撤={dd.min()*100:6.2f}%")
    return {"label": label, "n": len(trades), "wr": (rets > 0).mean() * 100,
            "avg": rets.mean() * 100, "total": (np.prod(1 + rets) - 1) * 100,
            "pf": pf, "max_dd": dd.min() * 100}


def main():
    print(f"=== 策略1 条件优化扫描 ({len(STOCKS)} 只股票, 自带交易纪律: 同持3/持20K/-5%止损/+15%止盈) ===\n")
    variants = {
        # (name, params)  params 覆盖 evaluate_strategy_1 阈值
        "基线(min_buy7)": {},
        "A: 放宽buy>=6": {"min_buy_passed": 6},
        "B: buy>=5+VR1.4+pos.8": {"min_buy_passed": 5, "min_vr": 1.4, "max_position": 0.8},
        "D: buy>=6+VR1.3": {"min_buy_passed": 6, "min_vr": 1.3},
    }
    results = [summarize(name, collect_trades(params)) for name, params in variants.items()]
    print("\n==== 汇总 ====")
    print(f"{'条件':<46}{'n':>5}{'胜率':>8}{'平均':>9}{'总收益':>10}{'盈比':>8}{'回撤':>9}")
    for r in results:
        if r:
            print(f"{r['label']:<46}{r['n']:>5}{r['wr']:>7.1f}%{r['avg']:>8.2f}%"
                  f"{r['total']:>9.2f}%{r['pf']:>8.2f}{r['max_dd']:>8.2f}%")

    valid = [r for r in results if r and r["n"] >= 8]
    if valid:
        best = max(valid, key=lambda r: r["wr"])
        print(f"\n推荐: {best['label']} (胜率{best['wr']:.1f}%, {best['n']}笔, 总收益{best['total']:.2f}%, 盈比{best['pf']:.2f})")
    else:
        print("\n各条件样本量不足(<8), 无法给出可信结论")


if __name__ == "__main__":
    main()
