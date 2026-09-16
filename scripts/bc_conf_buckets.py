"""BC conf→命中率校准校验: 按事件 conf 分桶统计 20 根方向命中率。

待办背景 (docs/event_env_gate_progress.md): 对弱信号 (BC/SOW) 按 conf 分桶看
高 conf 是否真高命中。SOW 侧已由放量门修正 (conf 高反命中低的来源 = 平凡放量
混入高 conf), BC 侧待查。本脚本:
  - 按 conf 分 5 档 (<40/40-49/50-59/60-69/>=70) 看是否单调;
  - 交叉前置环境门 (prior_r20 ≥+15% / ≤+4% / 中间) 看环境门是否已修正反向;
  - 记录 event_dir 看是否需确认方向 (BC 是 _REVERSAL_CONFIRM_DIR → 空头)。

用法:
  python scripts/bc_conf_buckets.py --limit 6000
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.events import detect_all
from wyckoff import sqldb

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


def _conf_bucket(c):
    if c < 40:
        return "低(<40)"
    if c < 50:
        return "40~49"
    if c < 60:
        return "50~59"
    if c < 70:
        return "60~69"
    return "高(>=70)"


def _gate_bucket(e):
    r = (e.get("feat") or {}).get("prior_r20")
    if r is None:
        return "na"
    if r >= 0.15:
        return "置涨>=+15%"
    if r <= 0.04:
        return "横盘<=+4%"
    return "中间"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    conf_bk = {b: {"n": 0, "hits": 0, "rets": []} for b in
               ("低(<40)", "40~49", "50~59", "60~69", "高(>=70)")}
    gate_bk = {b: {"n": 0, "hits": 0, "rets": []} for b in
               ("置涨>=+15%", "横盘<=+4%", "中间")}
    # conf × 门交叉: 确认环境门已把高 conf 中混入的反向样本挡掉
    cross = {g: {b: {"n": 0, "hits": 0} for b in ("低(<40)", "40~69", "高(>=70)")}
             for g in ("置涨>=+15%", "横盘<=+4%", "中间")}
    overall = {"n": 0, "hits": 0, "rets": []}
    pool = {"n": 0, "rets": []}
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
        n = len(df)
        last = {}
        for e in evs:
            if e.get("type") != "BC":
                continue
            i = int(e["idx"])
            if i < 0 or i + HORIZON >= n:
                continue
            prev = last.get("BC", -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last["BC"] = i
            conf = float(e.get("conf", 50))
            r = float(close[i + HORIZON] / close[i] - 1)
            hit = r < 0  # BC 确认方向 = 空头, 下跌记命中
            cb = _conf_bucket(conf)
            gb = _gate_bucket(e)
            overall["n"] += 1
            overall["hits"] += int(hit)
            overall["rets"].append(r)
            pool["rets"].append(r)
            for bk, key in ((conf_bk, cb), (gate_bk, gb)):
                bk[key]["n"] += 1
                bk[key]["hits"] += int(hit)
                bk[key]["rets"].append(r)
            ck = "低(<40)" if conf < 40 else ("高(>=70)" if conf >= 70 else "40~69")
            cross[gb][ck]["n"] += 1
            cross[gb][ck]["hits"] += int(hit)
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    o = overall
    ohr = o["hits"] / o["n"] * 100 if o["n"] else 0.0
    p_dn = np.mean([r < 0 for r in pool["rets"]]) if pool["rets"] else 0.0
    print(f"\n== BC 总览: n={o['n']} 命中(20根下跌)={ohr:.1f}% "
          f"池基准20根下跌={p_dn * 100:.1f}% ==")

    print("\n-- conf 分档 (期望单调: 高 conf → 高命中) --")
    for cb, bk in sorted(conf_bk.items()):
        if not bk["n"]:
            continue
        hr = bk["hits"] / bk["n"] * 100
        print(f"  [{cb:<8s}] n={bk['n']:<6d} 命中={hr:5.1f}% "
              f"相对={hr - p_dn * 100:+5.1f}pt 均值={np.mean(bk['rets']) * 100:+6.2f}%")

    print("\n-- 前置环境门 (prior_r20) --")
    for gb, bk in gate_bk.items():
        if not bk["n"]:
            continue
        hr = bk["hits"] / bk["n"] * 100
        print(f"  [{gb:<10s}] n={bk['n']:<6d} 命中={hr:5.1f}% "
              f"相对={hr - p_dn * 100:+5.1f}pt")

    print("\n-- conf × 门交叉 (命中率) --")
    for gb, row in cross.items():
        line = f"  [{gb:<10s}]"
        for ck in ("低(<40)", "40~69", "高(>=70)"):
            c = row[ck]
            if c["n"]:
                line += f"   {ck:<7s}: {c['hits'] / c['n'] * 100:5.1f}% (n={c['n']})"
            else:
                line += f"   {ck:<7s}: none"
        print(line)


if __name__ == "__main__":
    main()