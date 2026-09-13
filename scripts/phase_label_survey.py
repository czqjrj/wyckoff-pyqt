"""离线阶段带命中率调查: 直接读 SQLite 缓存K线, 跑 phase_segments, 汇总各阶段带后续方向。

与 wyckoff 内置先验 (wx_fb_prior.json) 同口径:
  - 每阶段带结束后未来 20 根上涨占比 up_ratio (沿用 fb_prior 定义);
  - 校准约定方向命中: accumulation/markdown → 后续应涨 (markdown 是反转偏多);
    distribution/markup → 后续应跌 (markup 是反转偏空)。
用途: 改动 phases.py (新增 _mark_tops / 放宽派发校验) 前后各跑一次, 对比各阶段
样本量与方向命中率变化 (重点: distribution 产量抬升、markup 方向命中靠近反转)。
用法:
  python scripts/phase_label_survey.py --limit 120
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.events import detect_all
from wyckoff.phases import phase_segments
from wyckoff import sqldb

HORIZON = 20
# fb_prior 约定: 各阶段"期望正义方向" (1=涨, -1=跌)。注意 markup/markdown 是
# 反转带 —— 段内净变动为正的 markup 末期, 其后 20 根应跌 (81.9% 实测)。
EXPECT = {"accumulation": 1, "markdown": 1, "distribution": -1, "markup": -1}


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
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--skip", type=int, default=0)
    args = ap.parse_args()

    syms = cached_klines()
    if args.skip:
        syms = syms[args.skip:args.skip + args.limit]
    else:
        syms = syms[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    stats = {}
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
            events = detect_all(df, pivots)
            segs = phase_segments(df, pivots, events)
        except Exception:
            continue
        close = df["close"].values
        n = len(df)
        for a, e, key, _label in segs:
            if e - a + 1 < 6 or e + HORIZON >= n:
                continue
            rec = stats.setdefault(key, {"n": 0, "segs": [], "ups": [], "lens": []})
            rec["n"] += 1
            rec["segs"].append(float(close[e] / close[a] - 1))
            rec["ups"].append(float(close[e + HORIZON] / close[e] - 1))
            rec["lens"].append(e - a + 1)
        done += 1
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s, 阶段带样本 "
          f"{sum(s['n'] for s in stats.values())}")

    print(f"\n== 各阶段带 (段末 20 根方向, 与 fb_prior 同口径) ==")
    total20 = [u for s in stats.values() for u in s["ups"]]
    p_up = np.mean([u > 0 for u in total20]) if total20 else 0.0
    print(f"池基准 (全部段末 20根上涨占比): {p_up * 100:.1f}%  (n={len(total20)})")
    for key in ("markdown", "accumulation", "markup", "distribution"):
        rec = stats.get(key)
        if not rec:
            print(f"  {key:<12s} 无样本")
            continue
        upr = np.mean([u > 0 for u in rec["ups"]])
        mean_net = np.mean(rec["segs"])
        mean_dur = np.mean(rec["lens"])
        hit = upr if EXPECT[key] > 0 else 1 - upr
        print(f"  {key:<12s} n={rec['n']:<5d} 段内净变={mean_net * 100:+.1f}% "
              f"段末20根上涨={upr * 100:5.1f}% 期望方向命中={hit * 100:5.1f}%  (均长={mean_dur:.0f})")


if __name__ == "__main__":
    main()