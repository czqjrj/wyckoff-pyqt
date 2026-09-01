#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子挖掘: 在策略4基线(强多头事件{Spring,Shakeout,ST,LPS,SC} conf>=90 近10根)上
采集全部候选因子与纪律持有收益(次日开盘入场 + -5%止损/+15%止盈/最多20天/0.4%成本),
输出各因子分层统计, 寻找能提升胜率/盈亏比的子集。"""
import sys, json, warnings, os
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

OUT = r"C:\Users\seewo\AppData\Local\Temp\opencode\mining_data.json"


def simulate_hold(df, i, horizon=HORIZON, cost=COST, stop_loss=0.05, take_profit=0.15):
    n = len(df)
    entry_idx = min(i + 1, n - 1)
    entry = df["open"].iloc[entry_idx]
    if entry <= 0:
        return None
    exit_idx = min(entry_idx + horizon, n - 1)
    exit_p = df["close"].iloc[exit_idx]
    stop_price = entry * (1 - stop_loss)
    for j in range(entry_idx + 1, exit_idx + 1):
        if df["high"].iloc[j] >= entry * (1 + take_profit):
            exit_p = entry * (1 + take_profit)
            exit_idx = j
            break
        if df["low"].iloc[j] <= stop_price:
            exit_p = stop_price
            exit_idx = j
            break
    return (exit_p / entry - 1) - cost


def extract_record(symbol, i, df, wdf, wevents, nt, vsa_labels, ev):
    """在采样点i对事件ev提取全部候选因子字段。价格/指标列从滚动窗口 wdf 取行 i。"""
    feat = ev.get("feat") or {}
    rec = {
        "stock": symbol,
        "event_idx": ev["idx"],
        "event_type": ev["type"],
        "conf": int(ev.get("conf", 0) or 0),
        "confirmed": ev.get("confirmed"),
        "dist": i - ev["idx"],
    }
    c = wdf.iloc[i]
    cols = wdf.columns.tolist()
    # 威科夫九大检验
    rec["buy_passed"] = nt["buy_passed"]
    rec["sell_passed"] = nt["sell_passed"]
    # 价格位置 (近20日)
    w20 = range(max(0, i - 20), i)
    hh = wdf["high"].iloc[w20].max()
    ll = wdf["low"].iloc[w20].min()
    rec["pos"] = float((c["close"] - ll) / (hh - ll)) if hh - ll > 0 else 0.5
    # 近5根高价值VSA (CHOC/DEM/SUP/LPS/Spring/ETR vr>=1.2)
    hv = False
    for s in vsa_labels:
        if s["label"] in ("CHOC", "DEM", "SUP", "LPS", "Spring", "ETR") \
                and 0 <= s["idx"] <= i and i - s["idx"] <= 5:
            vr = s.get("features", {}).get("vr", 0)
            if vr >= 1.2:
                hv = True
                break
    rec["near_high_vsa"] = hv
    # 技术指标
    if "price_ma20" in cols:
        rec["close_above_ma20"] = bool(c["close"] > c["price_ma20"])
        rec["ma20_above_ma50"] = bool(c["price_ma20"] > c["price_ma50"])
    if "rsi_6" in cols:
        rec["rsi6"] = float(c["rsi_6"])
    if "vol_z_20" in cols:
        rec["vol_z20"] = float(c["vol_z_20"])
    if "atr" in cols:
        rec["atr_pct"] = float(c["atr"] / c["close"] * 100)
    if "boll_up" in cols and "boll_dn" in cols:
        band = c["boll_up"] - c["boll_dn"]
        rec["boll_pct"] = float((c["close"] - c["boll_dn"]) / band) if band > 0 else 0.5
    # 事件特征 feat
    rec["feat_vr"] = feat.get("vr")
    rec["feat_rw"] = feat.get("rw")
    rec["feat_cpos"] = feat.get("cpos")
    rec["feat_trend"] = feat.get("trend")
    rec["feat_pos60"] = feat.get("pos60")
    rec["feat_boll_pct"] = feat.get("boll_pct")
    rec["feat_bw_pct"] = feat.get("bw_pct")
    rec["feat_reson"] = feat.get("reson")
    rec["feat_rsi_6"] = feat.get("rsi_6")
    rec["feat_kdj_d"] = feat.get("kdj_d")
    # 纪律持有收益
    ret = simulate_hold(df, i)
    rec["ret"] = ret
    return rec


def mine_stock(symbol, seen):
    df = fetch_kline(symbol, datalen=500, scale=240)
    if len(df) < 150:
        return []
    recs = []
    for i in range(90, len(df) - HORIZON - 10, 5):
        wdf = df.iloc[:i + 1].copy()
        wdf = add_indicators(wdf, symbol=symbol)
        wpivots = find_pivots(wdf, order=6)
        wevents = detect_all(wdf, wpivots)
        nt = nine_tests(wdf, wevents, wpivots)
        vsa_labels = vsa_classify(wdf, scale=240)
        for ev in wevents:
            if ev["type"] not in LONG_EV:
                continue
            if int(ev.get("conf", 0) or 0) < MIN_CONF:
                continue
            if ev["idx"] < i - 10:
                continue
            key = (symbol, ev["idx"])
            if key in seen:
                continue
            seen.add(key)
            rec = extract_record(symbol, i, df, wdf, wevents, nt, vsa_labels, ev)
            recs.append(rec)
            break  # 每个采样点只取事件最近的信号
    return recs


def bucket(rec, name):
    """返回因子分层标签, 未知/缺失 -> None"""
    def _b(cond):
        return cond if isinstance(cond, (str, bool)) else None

    if name == "event_type":
        return rec["event_type"]
    if name == "conf":
        c = rec["conf"]
        return "90-92" if c <= 92 else ("93-95" if c <= 95 else "96-100")
    if name == "confirmed":
        return str(rec["confirmed"])
    if name == "dist":
        d = rec["dist"]
        return "0-2" if d <= 2 else ("3-5" if d <= 5 else "6-10")
    if name == "buy_passed":
        b = rec["buy_passed"]
        return ">=7" if b >= 7 else ("5-6" if b >= 5 else ("3-4" if b >= 3 else "0-2"))
    if name == "pos":
        p = rec["pos"]
        return "0-0.4" if p < 0.4 else ("0.4-0.6" if p < 0.6 else ("0.6-0.8" if p < 0.8 else "0.8-0.9"))
    if name == "near_high_vsa":
        return "有" if rec["near_high_vsa"] else "无"
    if name == "close_above_ma20":
        return "ON-MA20上" if rec["close_above_ma20"] else "MA20下"
    if name == "ma20_above_ma50":
        return "MA20>MA50" if rec["ma20_above_ma50"] else "MA20<=MA50"
    if name == "rsi6":
        r = rec["rsi6"]
        return "RSI<30" if r < 30 else ("30-50" if r < 50 else ("50-70" if r < 70 else "RSI>=70"))
    if name == "vol_z20":
        z = rec["vol_z20"]
        return "Z<0" if z < 0 else ("0-1" if z < 1 else ("1-2" if z < 2 else "Z>=2"))
    if name == "atr_pct":
        a = rec["atr_pct"]
        return "<2%" if a < 2 else ("2-3%" if a < 3 else ("3-5%" if a < 5 else ">=5%"))
    if name == "boll_pct":
        b = rec["boll_pct"]
        return "<0.3" if b < 0.3 else ("0.3-0.7" if b < 0.7 else ">0.7")
    if name == "feat_vr":
        v = rec["feat_vr"]
        if v is None: return None
        return "vr<1" if v < 1 else ("1-1.5" if v < 1.5 else ("1.5-2.5" if v < 2.5 else "vr>=2.5"))
    if name == "feat_rw":
        v = rec["feat_rw"]
        if v is None: return None
        return "rw<1.2" if v < 1.2 else ("1.2-2" if v < 2 else "rw>=2")
    if name == "feat_pos60":
        v = rec["feat_pos60"]
        if v is None: return None
        return "<0.3" if v < 0.3 else ("0.3-0.6" if v < 0.6 else ("0.6-0.8" if v < 0.8 else ">=0.8"))
    if name == "feat_boll_pct":
        v = rec["feat_boll_pct"]
        if v is None: return None
        return "<0.3" if v < 0.3 else ("0.3-0.7" if v < 0.7 else ">0.7")
    if name == "feat_trend":
        v = rec["feat_trend"]
        return "上升" if v == 1 else "其他"
    if name == "feat_reson":
        v = rec["feat_reson"]
        return "共振>=1" if (v or 0) >= 1 else "无共振"
    if name == "feat_cpos":
        v = rec["feat_cpos"]
        if v is None: return None
        return "<0.5" if v < 0.5 else ">=0.5"
    return None


def show_bucket(recs, name, min_n=5):
    groups = defaultdict(list)
    for r in recs:
        b = bucket(r, name)
        if b is None:
            continue
        groups[b].append(r["ret"])
    if not groups:
        return
    print(f"  因子[{name}]:")
    for g in sorted(groups, key=lambda k: -len(groups[k])):
        rets = np.array(groups[g])
        n = len(rets)
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        wr = (rets > 0).mean() * 100
        avg = rets.mean() * 100
        pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
        cum = float(np.prod(1 + rets) - 1) * 100
        flag = "  ***" if (n >= min_n and wr >= 55 and avg > 0) else ""
        print(f"    {g:<14} n={n:<4} 胜率={wr:5.1f}%  均收益={avg:+6.2f}%  盈亏比={pf:5.2f}  累计={cum:+7.2f}%{flag}")


def main():
    all_recs = []
    seen = set()
    for symbol in STOCKS:
        try:
            recs = mine_stock(symbol, seen)
            all_recs.extend(recs)
            print(f"{symbol}: {len(recs)} 信号", flush=True)
        except Exception as e:
            print(f"{symbol}: 出错 {e}", flush=True)
            import traceback; traceback.print_exc()

    rets = np.array([r["ret"] for r in all_recs])
    print(f"\n==== 基线统计 (策略4: {len(LONG_EV)}事件 conf>={MIN_CONF} 近10根) ====")
    print(f"总样本: {len(all_recs)}  胜率={(rets>0).mean()*100:.1f}%  均收益={rets.mean()*100:+.2f}%")
    wins_sum = rets[rets > 0].sum()
    losses_sum = rets[rets <= 0].sum()
    pf = abs(wins_sum / losses_sum) if losses_sum != 0 else float('inf')
    print(f"盈亏比={pf:.2f}  累计={float(np.prod(1+rets)-1)*100:+.2f}%")

    json.dump(all_recs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"\n数据已保存: {OUT}")

    factors = [
        "event_type", "conf", "confirmed", "dist", "buy_passed", "pos",
        "near_high_vsa", "close_above_ma20", "ma20_above_ma50", "rsi6",
        "vol_z20", "atr_pct", "boll_pct", "feat_vr", "feat_rw", "feat_pos60",
        "feat_boll_pct", "feat_trend", "feat_reson", "feat_cpos",
    ]
    print("\n==== 因子分层统计 (*** = n>=5 且 胜率>=55% 且 均收益>0) ====")
    for f in factors:
        show_bucket(all_recs, f)


if __name__ == "__main__":
    main()