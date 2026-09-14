"""离线阶段划分参数扫描: 对 BOTTOM/TOP rec_lo 与区间带宽 band 的多组配置,
跑 phase_segments 汇总各阶段样本量与段末 20 根方向命中, 选出"吸筹/派发产量
提升而不牺牲方向命中"的配置。

用途: 支撑阶段分布失衡校准 (accuracy_report.md: 派发仅 1.1%/吸筹 6.3%) —
放大结构型阶段样本的同时验证 fwd 收益不退化。与 phase_label_survey.py 同口径
(段末 20 根方向, EXPECT 同 fb_prior: 吸筹/派发顺势, markup/markdown 反转)。

用法:
  python scripts/phase_param_sweep.py --limit 120
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.phases import phase_segments
from wyckoff import sqldb

HORIZON = 20
EXPECT = {"accumulation": 1, "markdown": 1, "distribution": -1, "markup": -1}

# 扫描配置: (名称, phase_segments 覆盖参数)。默认空 dict → 全自适应
# (rec_lo 下限 0.05 ~ 上限 0.06 随 ATR%, band≈30-60%; floor 校准见
# _adapt_min_rec: 3~4% 弱信号稀释派发命中)。
CONFIGS = [
    ("基线(固定 rec_lo=0.08 band=0.45)", dict(rec_lo=0.08, vol_pct=None, band=0.45)),
    ("自适应(默认)", dict()),
    ("rec_lo=0.05", dict(rec_lo=0.05)),
    ("rec_lo=0.06", dict(rec_lo=0.06)),
    ("band固定0.45+rec自适应", dict(band=0.45)),
]


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

    stats = {name: {} for name, _ in CONFIGS}
    t0 = time.time()
    done = 0
    for k, sym in enumerate(syms):
        try:
            res = load_cached(sym)
            if res is None:
                continue
            df, _src = res
            if df is None or len(df) < 120:
                continue
            df = add_indicators(df, symbol=sym)
            pivots = find_pivots(df, order=6)
            events = detect_all(df, pivots)
        except Exception:
            continue
        close = df["close"].values
        n = len(df)
        for name, kw in CONFIGS:
            try:
                segs = phase_segments(df, pivots, events, **kw)
            except Exception:
                continue
            st = stats[name]
            for a, e, key, _label in segs:
                if e - a + 1 < 6 or e + HORIZON >= n:
                    continue
                rec = st.setdefault(key, {"n": 0, "ups": [], "lens": [], "nets": []})
                rec["n"] += 1
                rec["ups"].append(float(close[e + HORIZON] / close[e] - 1))
                rec["lens"].append(e - a + 1)
                rec["nets"].append(float(close[e] / close[a] - 1))
        done += 1
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s\n", flush=True)

    for name, st in stats.items():
        totals = {k: r["n"] for k, r in st.items()}
        print(f"== {name} ==")
        allups = [u for r in st.values() for u in r["ups"]]
        base = np.mean([u > 0 for u in allups]) if allups else 0.0
        print(f"  池基准(段末20根上涨): {base * 100:.1f}%  (n={len(allups)})")
        for key in ("markdown", "accumulation", "markup", "distribution"):
            rec = st.get(key)
            if not rec:
                print(f"  {key:<12s} 无样本")
                continue
            upr = np.mean([u > 0 for u in rec["ups"]])
            hit = upr if EXPECT[key] > 0 else 1 - upr
            print(f"  {key:<12s} n={rec['n']:<5d} 段末20根上涨={upr * 100:5.1f}% "
                  f"期望方向命中={hit * 100:5.1f}%  均长={np.mean(rec['lens']):5.0f}")
        print()


if __name__ == "__main__":
    main()