#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场门禁验证: 合并 28只(样本内) + 20只(样本外) = 48只滚动信号,
按 上证MA20 牛/熊市 分层, 看 Spring/Shakeout 策略表现是否分化, 以及各组 buy&hold 基准对照。"""
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

IN_POOL = [
    "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
    "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
    "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
    "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
    "sh601166", "sz000725", "sh600028",
    "sh601088", "sz002230", "sh600809", "sz300059", "sh600585",
]
OOS_POOL = [
    "sh600031", "sz000002", "sh600016", "sh601668", "sh600886",
    "sz000063", "sh600009", "sz002475", "sz300015", "sz000568",
    "sh600690", "sh601211", "sz000100", "sh600089", "sz002371",
    "sh600438", "sz000538", "sh600570", "sz300124", "sh603501",
]
HORIZON = 20
COST = 0.004
MIN_CONF = 90
LONG_EV = ("Spring", "Shakeout", "ST", "LPS", "SC")


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


def load_market_ma20():
    import pandas as pd
    sh = fetch_kline("sh000001", datalen=600, scale=240)
    sh = add_indicators(sh, symbol="sh000001")
    return {str(pd.Timestamp(ts).date()): float(v) for ts, v in zip(sh["day"], sh["price_ma20"])}, \
           {str(pd.Timestamp(ts).date()): float(v) for ts, v in zip(sh["day"], sh["close"])}


def main():
    mkt_ma20, mkt_close = load_market_ma20()
    recs = []
    for symbol in IN_POOL + OOS_POOL:
        pool = "样本内" if symbol in IN_POOL else "样本外"
        df = fetch_kline(symbol, datalen=500, scale=240)
        if len(df) < 150:
            continue
        # buy&hold 基准
        bh = (df["close"].iloc[-1] / df["close"].iloc[90] - 1) * 100
        seen = set()
        n = 0
        for i in range(90, len(df) - HORIZON - 10, 5):
            wdf = df.iloc[:i + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)
            for ev in wevents:
                if ev["type"] not in LONG_EV:
                    continue
                if int(ev.get("conf", 0) or 0) < MIN_CONF:
                    continue
                if ev["idx"] < i - 10:
                    continue
                if (symbol, ev["idx"]) in seen:
                    continue
                seen.add((symbol, ev["idx"]))
                day = str(pd_start(wdf, i))
                m_close = mkt_close.get(day)
                m_ma = mkt_ma20.get(day)
                if m_close is None or m_ma is None:
                    break
                c = wdf.iloc[i]
                w20 = range(max(0, i - 20), i)
                hh = wdf["high"].iloc[w20].max()
                ll = wdf["low"].iloc[w20].min()
                pos = float((c["close"] - ll) / (hh - ll)) if hh - ll > 0 else 0.5
                rec = {
                    "pool": pool,
                    "etype": ev["type"],
                    "pos": pos,
                    "below_ma20": bool(c["close"] < c["price_ma20"]),
                    "rsi6": float(c["rsi_6"]),
                    "bull_mkt": bool(m_close > m_ma),
                    "ret": simulate_hold(df, i),
                }
                if rec["ret"] is None:
                    continue
                recs.append(rec)
                n += 1
                break
        print(f"{symbol} [{pool}] 信号={n} buyhold={bh:+.1f}%", flush=True)

    import pandas as pd

    def fmt(recs):
        if not recs:
            return "样本不足"
        rets = np.array([r["ret"] for r in recs])
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
        return (f"n={len(rets):<3} 胜率={np.mean(rets>0)*100:4.1f}% 均={np.mean(rets)*100:+5.2f}% "
                f"PF={pf:5.2f} 累计={float(np.prod(1+rets)-1)*100:+7.1f}%")

    def select(recs, key=None):
        if key is None:
            return recs
        return [r for r in recs if key(r)]

    print("\n==== 48只合并信号分层 ====")
    s_all = recs
    print(f"\n[整池] 总信号 {len(s_all)} | buyhold平均 {np.mean([r['bh'] for r in recs]):+.1f}%" if False else "")

    print("\n[基线 全部5事件]")
    print("  全部:        ", fmt(s_all))
    print("  牛市(MA20上):", fmt(select(s_all, lambda r: r["bull_mkt"])))
    print("  熊市(MA20下):", fmt(select(s_all, lambda r: not r["bull_mkt"])))

    spr = select(s_all, lambda r: r["etype"] in ("Spring", "Shakeout"))
    print("\n[Spring/Shakeout]")
    print("  全部:        ", fmt(spr))
    print("  牛市(MA20上):", fmt(select(spr, lambda r: r["bull_mkt"])))
    print("  熊市(MA20下):", fmt(select(spr, lambda r: not r["bull_mkt"])))
    spr_low = select(spr, lambda r: r["pos"] < 0.5)
    print("  牛市+低位<0.5:", fmt(select(spr_low, lambda r: r["bull_mkt"])))
    print("  牛市+低位<0.7:", fmt(select(spr, lambda r: r["bull_mkt"] and r["pos"] < 0.7)))
    print("  牛市+RSI>=50:", fmt(select(spr, lambda r: r["bull_mkt"] and r["rsi6"] >= 50)))

    print("\n[按股票池拆分: Spring/Shakeout]")
    print("  样本内28只:   ", fmt(select(spr, lambda r: r["pool"] == "样本内")))
    print("  样本外20只:   ", fmt(select(spr, lambda r: r["pool"] == "样本外")))

    json.dump(recs, open(r"C:\Users\seewo\AppData\Local\Temp\opencode\verify_48.json", "w", encoding="utf-8"), ensure_ascii=False, default=str)


def pd_start(wdf, i):
    return wdf["day"].iloc[i].strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()