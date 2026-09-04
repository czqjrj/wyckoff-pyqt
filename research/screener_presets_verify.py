#!/usr/bin/env python3
"""综合选股预设策略回测验证: 对 ScreenerWidget 的 5 个预设策略做历史滚动回测。

口径与 research/ 既有脚本一致:
  采样点 90..len-H-10, 步长5; 滚动窗口每点重算 add_indicators + find_pivots(order6)
  + detect_all + judge_phase; next_open 入场; -5%止损 / +15%止盈(盘中) / 最多20天 /
  0.4%成本; 每股每预设 cooldown 20 根, 避免重叠窗口重复计同一笔成本。

基本面 (PE/PB/市值) 与主力资金流无历史数据 → 用 K 线可推导的等价代理:
  value_accumulation  底部整固 + 20根内出现 {Spring,Shakeout,SC,ST,LPS}
  momentum_breakout   阶段∈{上升趋势,底部整固} + 技术分(与 screener._score_technical
                      同实现)>=14 (多头排列/MA20上行/MACD金叉/RSI适中/低波动蓄势)
  oversold_bounce     阶段∈{下跌趋势,顶部构筑,底部整固} + 深度超卖 (rsi6<30 或 20日低位 pos<0.30)
  small_cap_growth    底部整固 + 20根内强多头事件 + MA20>MA50 且价站上MA20 (代理'小盘成长入轨')
  fund_flow           阶段∈{底部整固,上升趋势} + 20日资金比(Σbody*vol/Σ|body|*vol)>0.1 且近5日仍净流入
"""
import json
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings('ignore')

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.phases import judge_phase
from wyckoff.screener import _score_technical

sys.path.append(r"E:\wyckoff-pyqt")

IN_STOCKS = [
    "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
    "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
    "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
    "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
    "sh601166", "sz000725", "sh600028",
    "sh601088", "sz002230", "sh600809", "sz300059", "sh600585",
]
OOS_STOCKS = [
    "sh600031", "sz000002", "sh600016", "sh601668", "sh600886",
    "sz000063", "sh600009", "sz002475", "sz300015", "sz000568",
    "sh600690", "sh601211", "sz000100", "sh600089", "sz002371",
    "sh600438", "sz000538", "sh600570", "sz300124", "sh603501",
]
STOCKS = list(dict.fromkeys(IN_STOCKS + OOS_STOCKS))

HORIZON = 20
COST = 0.004
LONG_EV = ("Spring", "Shakeout", "SC", "ST", "LPS")

PRESETS = [
    ("value_accumulation", "价值吸筹"),
    ("momentum_breakout", "强势突破"),
    ("oversold_bounce", "超跌反弹"),
    ("small_cap_growth", "小盘成长"),
    ("fund_flow", "主力抢筹"),
]
VARIANTS = [
    ("value_conf90", "价值吸筹+conf>=90"),
    ("value_10b", "价值吸筹(近10根)"),
    ("value_conf90_10b", "价值吸筹+conf>=90+近10根"),
    ("s4_baseline", "策略4参照(conf>=90近10根)"),
]
COOLDOWN = 20

OUT = r"C:\Users\seewo\AppData\Local\Temp\opencode\preset_trades.json"
LOG = r"C:\Users\seewo\AppData\Local\Temp\opencode\preset_verify_progress.log"

ISET = set(IN_STOCKS)


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


def check_presets(wdf, i):
    """返回 {preset_key: bool} 在采样点 i 的命中集合。wdf 已含指标列。"""
    pivots = find_pivots(wdf, order=6)
    events = detect_all(wdf, pivots)
    phase, _ = judge_phase(wdf, pivots, events)
    base = phase.split(" ")[0] if phase else ""
    cpos = wdf.iloc[i]

    recent20 = [e for e in events if e["idx"] >= i - 20]
    recent10 = [e for e in events if e["idx"] >= i - 10]
    t20 = {e["type"] for e in recent20}
    t10 = {e["type"] for e in recent10}
    ev20 = bool(t20 & set(LONG_EV))
    ev10 = bool(t10 & set(LONG_EV))
    conf90_20 = bool([e for e in recent20 if e["type"] in LONG_EV and int(e.get("conf", 0) or 0) >= 90])
    conf90_10 = bool([e for e in recent10 if e["type"] in LONG_EV and int(e.get("conf", 0) or 0) >= 90])

    w20 = range(max(0, i - 20), i)
    hh = wdf["high"].iloc[w20].max()
    ll = wdf["low"].iloc[w20].min()
    pos = float((cpos["close"] - ll) / (hh - ll)) if hh - ll > 0 else 0.5
    rsi6 = float(cpos["rsi_6"]) if np.isfinite(cpos["rsi_6"]) else 50.0

    body = wdf["close"].iloc[i - 19:i + 1] - wdf["open"].iloc[i - 19:i + 1]
    vol = wdf["volume"].iloc[i - 19:i + 1].astype(float)
    num20 = float(np.sum(body * vol))
    den20 = float(np.sum(np.abs(body) * vol)) or 1.0
    flow20 = num20 / den20
    b5 = wdf["close"].iloc[i - 4:i + 1] - wdf["open"].iloc[i - 4:i + 1]
    v5 = wdf["volume"].iloc[i - 4:i + 1].astype(float)
    flow5 = float(np.sum(b5 * v5)) / (float(np.sum(np.abs(b5) * v5)) or 1.0)

    m20 = float(wdf["price_ma20"].iloc[i])
    m50 = float(wdf["price_ma50"].iloc[i])

    tech_score = _score_technical(wdf)["score"]
    acc = base == "底部整固"
    up = base in ("上升趋势", "底部整固")

    return {
        "value_accumulation": acc and ev20,
        "momentum_breakout": up and tech_score >= 14,
        "oversold_bounce": base in ("下跌趋势", "顶部构筑", "底部整固") and (rsi6 < 30 or pos < 0.30),
        "small_cap_growth": acc and ev20 and m20 > m50 and cpos["close"] > m20,
        "fund_flow": up and flow20 > 0.10 and flow5 > 0,
        # 价值吸筹变体 / 策略4参照 (口径与 research 既有脚本一致)
        "value_conf90": acc and conf90_20,
        "value_10b": acc and ev10,
        "value_conf90_10b": acc and conf90_10,
        "s4_baseline": conf90_10,
    }


def log(msg):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    print(msg, flush=True)


def run_all():
    trades = {key: {"in": [], "oos": []} for key, _ in PRESETS + VARIANTS}
    for symbol in STOCKS:
        t0 = time.time()
        df = fetch_kline(symbol, datalen=500, scale=240)
        if len(df) < 150:
            log(f"{symbol}: 数据不足")
            continue
        pool = "in" if symbol in ISET else "oos"
        next_allow = {key: -1 for key, _ in PRESETS + VARIANTS}
        n_trade = 0
        for i in range(90, len(df) - HORIZON - 10, 5):
            wdf = df.iloc[:i + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            hits = check_presets(wdf, i)
            ret = simulate_hold(df, i)
            if ret is None:
                continue
            for key, _ in PRESETS + VARIANTS:
                if hits[key] and i >= next_allow[key]:
                    trades[key][pool].append(round(float(ret), 6))
                    next_allow[key] = i + COOLDOWN
                    n_trade += 1
        log(f"{symbol}: {n_trade} 笔 ({time.time() - t0:.0f}s)")
        json.dump(trades, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    return trades


def stats(arr):
    rets = np.asarray(arr, dtype=float)
    if len(rets) == 0:
        return None
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float('inf')
    return {
        "n": len(rets),
        "wr": (rets > 0).mean() * 100,
        "avg": rets.mean() * 100,
        "pf": pf,
        "cum": float(np.prod(1 + rets) - 1) * 100,
    }


def main():
    trades = run_all()

    print("\n\n==== 综合选股预设策略回测汇总 ====")
    print(f"{'策略':<8}{'池':<4}{'n':>4}{'胜率':>7}{'均收益':>8}{'盈亏比':>7}{'累计':>9}{'前半WR/后半WR':>14}")
    for key, cn in PRESETS:
        for pool in ("in", "oos"):
            arr = trades[key][pool]
            s = stats(arr)
            tag = "样本内" if pool == "in" else "样本外"
            if s is None:
                print(f"{cn:<8}{tag:<4}   (无信号)")
                continue
            half = len(arr) // 2
            w1 = np.mean([r > 0 for r in arr[:half]]) * 100 if half else 0
            w2 = np.mean([r > 0 for r in arr[half:]]) * 100 if half else 0
            print(f"{cn:<8}{tag:<4}{s['n']:>4}{s['wr']:>6.1f}%{s['avg']:>+7.2f}%"
                  f"{s['pf']:>7.2f}{s['cum']:>+8.1f}%{f'{w1:.0f}/{w2:.0f}':>14}")
        # 合并行
        merged = trades[key]["in"] + trades[key]["oos"]
        s = stats(merged)
        half = len(merged) // 2
        w1 = np.mean([r > 0 for r in merged[:half]]) * 100 if half else 0
        w2 = np.mean([r > 0 for r in merged[half:]]) * 100 if half else 0
        print(f"{cn:<8}{'合并48':<4}{s['n']:>4}{s['wr']:>6.1f}%{s['avg']:>+7.2f}%"
              f"{s['pf']:>7.2f}{s['cum']:>+8.1f}%{f'{w1:.0f}/{w2:.0f}':>14}")

    print("\n==== 价值吸筹变体 / 策略4参照 (仅合并48) ====")
    for key, cn in VARIANTS:
        merged = trades[key]["in"] + trades[key]["oos"]
        s = stats(merged)
        arr = merged
        half = len(arr) // 2
        w1 = np.mean([r > 0 for r in arr[:half]]) * 100 if half else 0
        w2 = np.mean([r > 0 for r in arr[half:]]) * 100 if half else 0
        if s is None:
            print(f"{cn:<24}  (无信号)")
            continue
        print(f"{cn:<24}{s['n']:>4}{s['wr']:>6.1f}%{s['avg']:>+7.2f}%"
              f"{s['pf']:>7.2f}{s['cum']:>+8.1f}%{f'{w1:.0f}/{w2:.0f}':>14}")

    print(f"\n逐笔收益已保存: {OUT}")
    print(f"进度日志: {LOG}")


if __name__ == "__main__":
    main()
