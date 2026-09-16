"""按个股自身趋势环境分组的 VSA 标签命中率: 验证"环境门"是否可落地。

把每根信号按其 features.trend (MA20>MA50 且 close>MA50 → 1, 否则 0) 分桶,
对比各标签在多头排列/空头排列环境下 20 根方向命中率。若看多标签 (SC/DEM/ETR/SV/NS)
在多头排列环境下命中率显著更高, 则"信号叠加个股趋势门"是有效改进。
用法:
  python scripts/vsa_by_trend.py --limit 6000
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff import sqldb
from wyckoff.indicators import add_indicators
from wyckoff.vsa import vsa_classify

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


def _vsa_dir(lb):
    from wyckoff.fusion import VSA_BEAR, VSA_BULL
    return 1 if lb in VSA_BULL else (-1 if lb in VSA_BEAR else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    # buckets: trend -> label -> {n, hits}
    stats = {0: {}, 1: {}}
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
        except Exception:
            continue
        try:
            sigs = vsa_classify(df, scale=240)
        except Exception:
            continue
        last = {}
        n = len(df)
        close = df["close"].values
        for s in sigs:
            lb = s.get("label", "?")
            i = int(s["idx"])
            if i < 0 or i + HORIZON >= n:
                continue
            prev = last.get(lb, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[lb] = i
            d = _vsa_dir(lb)
            if d == 0:
                continue
            trend = int((s.get("features") or {}).get("trend", 0))
            rec = stats[trend].setdefault(lb, {"n": 0, "hits": 0, "rets": []})
            rec["n"] += 1
            r = float(close[i + HORIZON] / close[i] - 1)
            rec["rets"].append(r)
            if (r < 0 if d < 0 else r > 0):
                rec["hits"] += 1
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    for trend, up in ((0, "空头排列 trend=0"), (1, "多头排列 trend=1")):
        buck = stats[trend]
        total = sum(r["n"] for r in buck.values())
        pool = [r for b in buck.values() for r in b["rets"]]
        p_up = np.mean([r > 0 for r in pool]) if pool else 0.0
        print(f"\n== {up}: 总样本 {total}, 池基准 20根上涨占比 {p_up * 100:.1f}% ==")
        rows = []
        for lb, rec in buck.items():
            if rec["n"] < 50:
                continue
            wr = rec["hits"] / rec["n"]
            rows.append((lb, rec["n"], wr, np.mean(rec["rets"])))
        for lb, n, wr, mean in sorted(rows, key=lambda x: -x[2]):
            lift = (wr - p_up) * 100
            print(f"  {lb:<6s} n={n:<6d} 命中={wr * 100:5.1f}% 相对基准={lift:+5.1f}pt "
                  f"均值={mean * 100:+6.2f}%")


if __name__ == "__main__":
    main()
