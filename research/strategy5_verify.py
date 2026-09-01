#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略5候选验证: 30只全样本滚动窗口回测, 对比 策略4基线 与 各 Spring/Shakeout 低位反转组合。
口径与 three_strategies_backtest.py 一致: 5根步长, next_open入场, -5%止损/+15%止盈/最多20天/0.4%成本, 每事件去重。"""
import sys, warnings, json
warnings.filterwarnings('ignore')
sys.path.append(r"E:\wyckoff-pyqt")
from collections import defaultdict
import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify

STOCKS = [
    "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
    "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
    "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
    "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
    "sh601166", "sz000725", "sh600028",
    "sh601088", "sz002230", "sh600809", "sz300059", "sh600585",
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
    overall_seen = defaultdict(set)
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
                if key in overall_seen["__dedup__"]:
                    continue
                overall_seen["__dedup__"].add(key)
                w20 = range(max(0, i - 20), i)
                hh = wdf["high"].iloc[w20].max()
                ll = wdf["low"].iloc[w20].min()
                pos = float((c["close"] - ll) / (hh - ll)) if hh - ll > 0 else 0.5
                feat = ev.get("feat") or {}
                rec = {
                    "etype": ev["type"],
                    "pos": pos,
                    "below_ma20": c["close"] < c["price_ma20"],
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
        print(f"{symbol}: {n_stock} 信号", flush=True)

    print("\n==== 候选组合对比 (30只, 近500天, 滚动回测) ====")
    print(f"{'组合':<26}{'n':>4}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'累计':>10}{'前/后半WR':>14}")
    for name, cn in CN.items():
        rets = np.array(trades[name])
        if len(rets) < 3:
            print(f"{cn:<26}{len(rets):>4}   (样本不足)")
            continue
        rets_sorted = sorted(trades[name])
        half = len(rets) // 2
        w1 = np.mean([1 if r > 0 else 0 for r in rets_sorted[:half]]) * 100
        w2 = np.mean([1 if r > 0 else 0 for r in rets_sorted[half:]]) * 100
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
        print(f"{cn:<26}{len(rets):>4}{np.mean(rets>0)*100:>7.1f}%{np.mean(rets)*100:>+8.2f}%"
              f"{pf:>8.2f}{float(np.prod(1+rets)-1)*100:>+10.1f}%{f'{w1:.0f}/{w2:.0f}':>14}")

    out = {k: [float(r) for r in v] for k, v in trades.items()}
    json.dump(out, open(r"C:\Users\seewo\AppData\Local\Temp\opencode\verify_trades.json", "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()