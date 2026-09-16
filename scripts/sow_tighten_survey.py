"""SOW 收紧调查: 按前置条件分桶 SOW 的 20 根方向命中率, 找可收紧子集。

SOW 现状 (docs/event_label_low_n.txt 全量 5345 只): n=306, 命中 69.0%,
conf 均值仅 31。本文目标是验证"收紧可提升质量"的候选维度:
  1) 前置 UTAD/LPSY 共振 (事件前 40 根内是否已有派发侧铺垫);
  2) 破位深度 (低点 vs 支撑 floor 的深度);
  3) 放量强度 vol_ratio_20;
  4) 事件 conf 分档 (看高 conf 是否真对应高命中 → 决定是否直接提 conf)。

用法:
  python scripts/sow_tighten_survey.py --limit 6000
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff import sqldb
from wyckoff.config import EVENT_COLORS
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots

HORIZON = 20
COOLDOWN = 15


def cached_klines():
    conn = sqldb._conn()
    sqldb._ensure_init(conn)
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM kline_cache WHERE scale = 240").fetchall()
    return [r[0] for r in rows]


def load_cached(symbol, max_age=10 ** 9):
    try:
        return sqldb.kline_load(symbol, 240, max_age)
    except Exception:
        return None


def _bucket(cond):
    return {k: {"n": 0, "hits": 0, "rets": [], "conf": []}
            for k in cond}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    # SOW 前置 40 根内是否出现派发铺垫事件 (UTAD/LPSY/BC, 方向确认后才有意义)
    buckets = {
        "前置UTAD/LPSY/BC共振": _bucket({"有前置铺垫", "无前置铺垫"}),
        "破位深度": _bucket({"<6%", "6~12%", ">12%"}),
        "vol_ratio_20": _bucket({"<1.6", "1.6~2.2", ">=2.2"}),
        "conf档": _bucket({"低(<40)", "中(40~69)", "高(>=70)"}),
    }
    overall = {"n": 0, "hits": 0, "rets": [], "conf": []}
    t0 = time.time()
    done = 0
    for k, sym in enumerate(syms):
        try:
            res = load_cached(sym)
            if res is None:
                continue
            df, src = res
            if df is None or len(df) < 120:
                continue
            df = add_indicators(df, symbol=sym)
            pivots = find_pivots(df, order=6)
            evs = detect_all(df, pivots)
        except Exception:
            continue
        close = df["close"].values
        low = df["low"].values
        vol_r = df["vol_ratio_20"].values if "vol_ratio_20" in df else None
        n = len(df)
        last = {}
        for e in evs:
            if e.get("type") != "SOW":
                continue
            i = int(e["idx"])
            if i < 20 or i + HORIZON >= n:
                continue
            prev = last.get("SOW", -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last["SOW"] = i
            # 前置 40 根内是否有派发铺垫事件
            pre_d = [x["type"] for x in evs
                     if x["idx"] > i - 40 and x["idx"] < i
                     and x["type"] in ("UTAD", "LPSY", "BC")]
            has = "有前置铺垫" if pre_d else "无前置铺垫"
            # 破位深度 vs 参照: 用事件低点相对前 60 根支撑 (近似 floor=事件价格)
            price = float(e.get("price", low[i]))
            floor = float(df["low"].iloc[max(0, i - 60):i].min())
            depth = price / floor - 1 if floor > 0 else 0.0
            if depth > -0.12:
                depth_b = "<6%" if depth > -0.06 else "6~12%"
            else:
                depth_b = ">12%"
            vr = float(vol_r[i]) if vol_r is not None and np.isfinite(vol_r[i]) else 0.0
            if vr < 1.6:
                vr_b = "<1.6"
            elif vr < 2.2:
                vr_b = "1.6~2.2"
            else:
                vr_b = ">=2.2"
            conf = float(e.get("conf", 50))
            conf_b = "低(<40)" if conf < 40 else ("中(40~69)" if conf < 70 else "高(>=70)")

            r = float(close[i + HORIZON] / close[i] - 1)  # SOW 空头: 下跌记命中
            hit = r < 0
            overall["n"] += 1
            overall["hits"] += int(hit)
            overall["rets"].append(r)
            overall["conf"].append(conf)
            for dim, val in (("前置UTAD/LPSY/BC共振", has),
                             ("破位深度", depth_b), ("vol_ratio_20", vr_b),
                             ("conf档", conf_b)):
                rec = buckets[dim][val]
                rec["n"] += 1
                rec["hits"] += int(hit)
                rec["rets"].append(r)
                rec["conf"].append(conf)
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    o = overall
    ohr = o["hits"] / o["n"] * 100 if o["n"] else 0.0
    print(f"\n== SOW 总览: n={o['n']} 命中={ohr:.1f}% "
          f"conf均值={np.mean(o['conf']):.1f} ==")
    for dim, cond in buckets.items():
        print(f"  -- {dim} --")
        for val, rec in cond.items():
            if not rec["n"]:
                continue
            hr = rec["hits"] / rec["n"] * 100
            print(f"    {val:<16s} n={rec['n']:<5d} 命中={hr:5.1f}% "
                  f"相对= {hr - ohr:+5.1f}pt conf均值={np.mean(rec['conf']):5.1f} "
                  f"均值={np.mean(rec['rets']) * 100:+6.2f}%")
    print("\nEVENT_COLORS SOW =", EVENT_COLORS.get("SOW"))


if __name__ == "__main__":
    main()
