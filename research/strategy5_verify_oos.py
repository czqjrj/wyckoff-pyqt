#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略5候选 样本外验证: 用与挖掘完全不同的20只股票(不同行业), 滚动窗口回测。
口径与 three_strategies_backtest.py 一致: 5根步长, next_open入场, -5%止损/+15%止盈/最多20天/0.4%成本, 每事件去重。"""
import sys, warnings, json
warnings.filterwarnings('ignore')
sys.path.append(r"E:\wyckoff-pyqt")
from collections import defaultdict
import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify

STOCKS = [
    "sh600031", "sz000002", "sh600016", "sh601668", "sh600886",
    "sz000063", "sh600009", "sz002475", "sz300015", "sz000568",
    "sh600690", "sh601211", "sz000100", "sh600089", "sz002371",
    "sh600438", "sz000538", "sh600570", "sz300124", "sh603501",
]
STOCKS = list(dict.fromkeys(STOCKS))
HORIZON = 20
COST = 0.004
MIN_CONF = 90
LONG_EV = ("Spring", "Shakeout", "ST", "LPS", "SC")

COMBOS = {
    "s4_baseline":  lambda r: True,
    "s5_noST":      lambda r: r["etype"] in ("Spring", "Shakeout"),
    "s5_pos07":     lambda r: r["etype"] in ("Spring", "Shakeout") and r["pos"] < 0.7,
    "s5_belowMA20": lambda r: r["etype"] in ("Spring", "Shakeout") and r["below_ma20"],
    "s5_pos05":     lambda r: r["etype"] in ("Spring", "Shakeout") and r["pos"] < 0.5,
    "s5_rsi50_pos05": lambda r: r["etype"] in ("Spring", "Shakeout") and r["rsi6"] >= 50 and r["pos"] < 0.5,
    "s5_rsi50_ma20":  lambda r: r["etype"] in ("Spring", "Shakeout") and r["rsi6"] >= 50 and r["below_ma20"],
    "s5_ma20_zlt1":   lambda r: r["etype"] in ("Spring", "Shakeout") and r["below_ma20"] and r["vol_z"] < 1,
}
CN = {
    "s4_baseline": "策略4基线(全部5事件)",
    "s5_noST": "仅Spring/Shakeout",
    "s5_pos07": "+pos<0.7",
    "s5_belowMA20": "+MA20下方",
    "s5_pos05": "+pos<0.5",
    "s5_rsi50_pos05": "+RSI>=50+pos<0.5",
    "s5_rsi50_ma20": "+RSI>=50+MA20下方",
    "s5_ma20_zlt1": "+MA20下方+量Z<1",
}
trades = defaultdict(list)


def simulate_hold(df, i, stop_loss=0.05, take_profit=0.15):
    n = len(df)
    entry_idx = min(i + 1, n - 1)
    entry = df["open"].iloc[entry_idx]
    if entry <= 0:
        return None
    exit_idx = min(entry_idx + HORIZON, n - 1)
    exit_p = df["close"].iloc[exit_idx]
    stop_price = entry * (1 - stop_loss)
    for j in range(entry_idx + 1, exit_idx + 1):
        if df["high"].iloc[j] >= entry * (1 + take_profit):
            exit_p = entry * (1 + take_profit)
            break
        if df["low"].iloc[j] <= stop_price:
            exit_p = stop_price
            break
    return (exit_p / entry - 1) - COST


def main():
    overall_seen = set()
    for symbol in STOCKS:
        df = fetch_kline(symbol, datalen=500, scale=240)
        if len(df) < 150:
            print(symbol, "数据不足", flush=True)
            continue
        n_stock = 0
        for i in range(90, len(df) - HORIZON - 10, 5):
            wdf = df.iloc[:i + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)
            nt = nine_tests(wdf, wevents, wpivots)
            vsa_labels = vsa_classify(wdf, scale=240)
            c = wdf.iloc[i]
            for ev in wevents:
                if ev["type"] not in LONG_EV:
                    continue
                if int(ev.get("conf", 0) or 0) < MIN_CONF:
                    continue
                if ev["idx"] < i - 10:
                    continue
                key = (symbol, ev["idx"])
                if key in overall_seen:
                    continue
                overall_seen.add(key)
                w20 = range(max(0, i - 20), i)
                hh = wdf["high"].iloc[w20].max()
                ll = wdf["low"].iloc[w20].min()
                pos = float((c["close"] - ll) / (hh - ll)) if hh - ll > 0 else 0.5
                rec = {
                    "etype": ev["type"],
                    "pos": pos,
                    "below_ma20": bool(c["close"] < c["price_ma20"]),
                    "rsi6": float(c["rsi_6"]),
                    "vol_z": float(c["vol_z_20"]),
                }
                ret = simulate_hold(df, i)
                if ret is None:
                    continue
                for name, fn in COMBOS.items():
                    if fn(rec):
                        trades[name].append(ret)
                n_stock += 1
                break
        print(f"{symbol}: {n_stock} 信号 (url:%s)" % "cache", flush=True)

    print("\n==== [样本外 20只] 候选组合对比 ====")
    print(f"{'组合':<26}{'n':>4}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'累计':>10}")
    for name, cn in CN.items():
        rets = np.array(trades[name])
        if len(rets) < 3:
            print(f"{cn:<26}{len(rets):>4}   (样本不足)")
            continue
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
        print(f"{cn:<26}{len(rets):>4}{np.mean(rets>0)*100:>7.1f}%{np.mean(rets)*100:>+8.2f}%"
              f"{pf:>8.2f}{float(np.prod(1+rets)-1)*100:>+10.1f}%")

    out = {k: [float(r) for r in v] for k, v in trades.items()}
    json.dump(out, open(r"C:\Users\seewo\AppData\Local\Temp\opencode\verify_trades_oos.json", "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()