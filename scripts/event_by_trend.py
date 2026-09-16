"""按环境分桶的 SC/BC 事件命中率: 寻找"环境门"落地点。

复用 event_label_survey 的全量事件管线, 对 SC/BC 按
  1) 趋势排列 (feat.trend: MA20>MA50 且 close>MA50)
  2) boll_pct 位置 (低<0.35 / 中 / 高>0.65)
  3) 事件前 20 根涨跌幅 (大跌<-8% / 横 / 大涨>+8%)
分桶统计 20 根方向命中率 (SC=看多确认, BC=看空确认), 找有效子集。
用法:
  python scripts/event_by_trend.py --limit 6000 [--bucket trend,boll,prior]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff import sqldb
from wyckoff.config import _REVERSAL_CONFIRM_DIR, event_dir
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


def _dir(typ):
    if typ in _REVERSAL_CONFIRM_DIR:
        return _REVERSAL_CONFIRM_DIR[typ]
    return event_dir(typ)


def _boll_bucket(bp):
    if bp is None or not np.isfinite(bp):
        return "na"
    if bp < 0.35:
        return "低<0.35"
    if bp > 0.65:
        return "高>0.65"
    return "中"


def _prior_bucket(r20):
    if r20 <= -0.08:
        return "跌>8%"
    if r20 >= 0.08:
        return "涨>8%"
    return "横盘"


def _prior_fine(r20):
    for lo, hi, name in ((-1.0, -0.15, "<-15%"), (-0.15, -0.08, "-15~-8%"),
                         (-0.08, -0.04, "-8~-4%"), (-0.04, 0.04, "横盘"),
                         (0.04, 0.08, "+4~8%"), (0.08, 0.15, "+8~15%"),
                         (0.15, 1.0, ">+15%")):
        if lo <= r20 < hi:
            return name
    return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    # bucket_dim -> bucket_val -> type -> {n, hits, rets}
    stats = {"trend": {"0": {}, "1": {}},
             "boll": {b: {} for b in ("低<0.35", "中", "高>0.65", "na")},
             "prior": {b: {} for b in ("跌>8%", "横盘", "涨>8%")},
             "prior_fine": {b: {} for b in
                            ("<-15%", "-15~-8%", "-8~-4%", "横盘",
                             "+4~8%", "+8~15%", ">+15%")}}
    overall = {"SC": {"n": 0, "hits": 0, "rets": []},
               "BC": {"n": 0, "hits": 0, "rets": []}}
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
        n = len(df)
        close = df["close"].values
        ma20 = df["price_ma20"].values
        ma50 = df["price_ma50"].values
        boll_up = df["boll_up"].values if "boll_up" in df else None
        boll_dn = df["boll_dn"].values if "boll_dn" in df else None
        last = {}
        for e in evs:
            typ = e.get("type", "?")
            if typ not in ("SC", "BC"):
                continue
            i = int(e["idx"])
            if i < 20 or i + HORIZON >= n:
                continue
            prev = last.get(typ, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[typ] = i
            d = _dir(typ)
            r = float(close[i + HORIZON] / close[i] - 1)
            r20 = float(close[i] / close[i - 20] - 1)
            rec_o = overall[typ]
            rec_o["n"] += 1
            rec_o["rets"].append(r)
            if (r < 0 if d < 0 else r > 0):
                rec_o["hits"] += 1
            trend = int(np.isfinite(ma20[i]) and np.isfinite(ma50[i])
                        and ma20[i] > ma50[i] and close[i] > ma50[i])
            bp = None
            if boll_up is not None and boll_up[i] - boll_dn[i] > 1e-9:
                bp = float((close[i] - boll_dn[i]) /
                           (boll_up[i] - boll_dn[i]))
            buckets = {
                "trend": str(trend),
                "boll": _boll_bucket(bp),
                "prior": _prior_bucket(r20),
                "prior_fine": _prior_fine(r20),
            }
            for dim, bval in buckets.items():
                if bval not in stats[dim]:
                    stats[dim][bval] = {}
                rec = stats[dim][bval].setdefault(
                    typ, {"n": 0, "hits": 0, "rets": []})
                rec["n"] += 1
                rec["rets"].append(r)
                if (r < 0 if d < 0 else r > 0):
                    rec["hits"] += 1
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    for typ, d in (("SC", 1), ("BC", -1)):
        o = overall[typ]
        hr = o["hits"] / o["n"] * 100 if o["n"] else 0.0
        print(f"\n== {typ} (方向 {d:+d}) 总体: n={o['n']} 命中={hr:.1f}% "
              f"均值={np.mean(o['rets']) * 100:+.2f}% ==")
        for dim in ("trend", "boll", "prior", "prior_fine"):
            print(f"  -- {dim} --")
            for bval, buck in sorted(stats[dim].items()):
                rec = buck.get(typ)
                if not rec or rec["n"] < 30:
                    continue
                hr2 = rec["hits"] / rec["n"] * 100
                print(f"    {bval:<9s} n={rec['n']:<6d} 命中={hr2:5.1f}% "
                      f"相对自身={hr2 - hr:+5.1f}pt 均值={np.mean(rec['rets']) * 100:+6.2f}%")


if __name__ == "__main__":
    main()
