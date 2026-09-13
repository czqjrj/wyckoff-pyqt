"""离线 VSA 标签命中率调查: 直接读 SQLite 缓存K线, 逐根跑 vsa_classify, 汇总各标签方向命中率。

与生产 record_signals 同口径:
  - 每标签未来 HORIZONS 根方向化命中: 标称多头 (vsa_dir>0) → ret>0 记命中;
    标称空头 (vsa_dir<0) → ret<0 记命中; 中性不计。
  - 冷却窗去重: 同标的同标签 15 根内只保留一条 (防洪水式信号刷屏)。
用途: 改动 vsa.py 检测规则前后各跑一次, 对比各标签命中率/样本量变化。
用法:
  python scripts/vsa_label_survey.py --limit 300
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.config import vsa_dir
from wyckoff.indicators import add_indicators
from wyckoff import sqldb
from wyckoff.vsa import vsa_classify

HORIZONS = (5, 20, 40)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--skip", type=int, default=0)
    args = ap.parse_args()

    syms = cached_klines()
    if args.skip:
        syms = syms[args.skip:args.skip + args.limit]
    else:
        syms = syms[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    stats = {}
    pool = {h: [] for h in HORIZONS}
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
        except Exception as e:
            print(f"  vsa_classify({sym}) 失败: {e}", flush=True)
            continue
        # 冷却窗去重: 同标的同标签 COOLDOWN 根内只留首条
        last = {}
        n = len(df)
        close = df["close"].values
        for s in sigs:
            lb = s.get("label", "?")
            i = int(s["idx"])
            if i < 0 or i + max(HORIZONS) >= n:
                continue
            prev = last.get(lb, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[lb] = i
            d = vsa_dir(lb)
            if d == 0:
                continue
            rec = stats.setdefault(lb, {"n": 0, "rets": {h: [] for h in HORIZONS},
                                       "hits": {h: 0 for h in HORIZONS}})
            rec["n"] += 1
            for h in HORIZONS:
                r = float(close[i + h] / close[i] - 1)
                rec["rets"][h].append(r)
                if (r < 0 if d < 0 else r > 0):
                    rec["hits"][h] += 1
                pool[h].append(r)
        done += 1
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s, 信号样本 {sum(s['n'] for s in stats.values())}")

    pool20 = pool[20]
    p_up = np.mean([r > 0 for r in pool20]) if pool20 else 0.0
    print(f"池基准 (全部方向信号 20根上涨占比): {p_up * 100:.1f}%  (n={len(pool20)})")

    print("\n== 各标签 20 根方向命中率 (n>=1, 相对池基准) ==")
    rows = []
    for lb, rec in stats.items():
        wr = rec["hits"][20] / rec["n"] if rec["n"] else 0.0
        rows.append((lb, rec["n"], wr,
                     np.mean(rec["rets"][20]) if rec["rets"][20] else 0.0))
    for lb, n, wr, mean in sorted(rows, key=lambda x: -x[2]):
        lift = (wr - p_up) * 100
        print(f"  {lb:<6s} n={n:<5d} 命中={wr * 100:5.1f}% 相对基准={lift:+5.1f}pt "
              f"均值={mean * 100:+6.2f}%")


if __name__ == "__main__":
    main()