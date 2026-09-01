#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终稳定性验证: 48只合并, Spring/Shakeout 信号在 大盘MA20下(弱势) 的表现,
按 股票池(样本内/外) 与 时间(前半/后半) 拆分, 确认是否稳健后才入库策略5。"""
import sys, warnings, json
warnings.filterwarnings('ignore')
sys.path.append(r"E:\wyckoff-pyqt")
from collections import defaultdict
import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.vsa import vsa_classify
import pandas as pd

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


def load_market():
    sh = fetch_kline("sh000001", datalen=600, scale=240)
    sh = add_indicators(sh, symbol="sh000001")
    ma20 = {str(d.date()): float(v) for d, v in zip(sh["day"], sh["price_ma20"])}
    close = {str(d.date()): float(v) for d, v in zip(sh["day"], sh["close"])}
    return ma20, close


def fmt(recs):
    if not recs:
        return "样本不足"
    rets = np.array([r["ret"] for r in recs])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
    return (f"n={len(rets):<3} 胜率={np.mean(rets>0)*100:4.1f}% 均={np.mean(rets)*100:+5.2f}% "
            f"PF={pf:5.2f} 累计={float(np.prod(1+rets)-1)*100:+7.1f}%")


def main():
    mkt_ma20, mkt_close = load_market()
    recs = []
    for symbol in IN_POOL + OOS_POOL:
        pool = "样本内" if symbol in IN_POOL else "样本外"
        df = fetch_kline(symbol, datalen=500, scale=240)
        if len(df) < 150:
            continue
        seen = set()
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
                day = str(wdf["day"].iloc[i].date())
                m_close = mkt_close.get(day)
                m_ma = mkt_ma20.get(day)
                if m_close is None or m_ma is None:
                    break
                ret = simulate_hold(df, i)
                if ret is None:
                    continue
                recs.append({
                    "pool": pool,
                    "etype": ev["type"],
                    "date": day,
                    "bull_mkt": bool(m_close > m_ma),
                    "ret": ret,
                })
                break
        print(f"{symbol} ok", flush=True)

    spr = [r for r in recs if r["etype"] in ("Spring", "Shakeout")]
    by_mkt = {"熊市(MA20下)": [r for r in spr if not r["bull_mkt"]],
              "牛市(MA20上)": [r for r in spr if r["bull_mkt"]]}

    print("\n==== Spring/Shakeout 按市场状态: 总/股票池/时间 ====")
    # 整体时间排序边界
    spr_sorted = sorted(spr, key=lambda r: r["date"])
    mid_date = spr_sorted[len(spr_sorted)//2]["date"] if spr_sorted else None
    print(f"时间中点: {mid_date}")

    for mkt, group in by_mkt.items():
        g_sorted = sorted(group, key=lambda r: r["date"])
        half = len(g_sorted)//2
        lo, hi = g_sorted[:half] if half else g_sorted, g_sorted[half:] if half else []
        print(f"\n[{mkt}] 全部: {fmt(group)}")
        print(f"   时间前半: {fmt(lo)}")
        print(f"   时间后半: {fmt(hi)}")
        for pool in ("样本内", "样本外"):
            sub = [r for r in group if r["pool"] == pool]
            print(f"   {pool}: {fmt(sub)}")

    # 对照组: ST 事件
    st = [r for r in recs if r["etype"] == "ST"]
    print("\n==== ST事件 (对照, 判断排除合理性) ====")
    print(f"[ST] 全部: {fmt(st)}")
    print(f"[ST] 熊市: {fmt([r for r in st if not r['bull_mkt']])}")
    print(f"[ST] 牛市: {fmt([r for r in st if r['bull_mkt']])}")

    json.dump(recs, open(r"C:\Users\seewo\AppData\Local\Temp\opencode\verify_final.json", "w", encoding="utf-8"),
              ensure_ascii=False)


if __name__ == "__main__":
    main()