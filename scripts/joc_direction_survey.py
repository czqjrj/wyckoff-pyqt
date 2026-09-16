"""JOC 方向专项验证: 放宽检测定义采集 JOC 候选样本, 统计 20 根方向命中。

背景: detect_joc_lps_bu 全量 5345 只产出 JOC=0 / BU=0 (死代码), 无法用现
有检测判断"JOC 是否该看多"。本脚本用放宽定义 (仅放量1.8x + 收盘破60日前高)
标记 JOC 候选, 统计未来 20 根上涨占比 vs 池基准, 判断方向正确性:

  - 若上涨占比 >50% 且 >池基准 → JOC 该看多 (修复检测放宽);
  - 若贴近随机/反向 (像 SOS 29%) → JOC 应降中性或回收触发 (与 SOS 对齐)。

用法:
  python scripts/joc_direction_survey.py --limit 6000
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from wyckoff.indicators import add_indicators
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--min_vol", type=float, default=1.8,
                    help="放量倍数门 (默认 1.8, 与 detect_joc_lps_bu 同源)")
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    cands = []
    pool = {"n": 0, "rets": []}  # 全部方向事件基准
    t0 = time.time()
    done = 0
    for k, sym in enumerate(syms):
        try:
            res = load_cached(sym)
            if res is None:
                continue
            df, src = res
            if df is None or len(df) < 100:
                continue
            df = add_indicators(df, symbol=sym)
        except Exception:
            continue
        close = df["close"].values
        high = df["high"].values
        volume = df["volume"].values
        vol_ma20 = df["vol_ma20"].values
        n = len(df)
        if n < 100:
            continue
        prev_hi60 = pd.Series(high).rolling(60).max().shift(1).values
        boll_pct = df["boll_pct"].values if "boll_pct" in df.columns else None
        # 放宽 JOC 定义: 放量1.8x + 收盘破60日前高 + 距前高锚问题留给方向验证
        raw = (np.arange(n) >= 61) & (volume >= vol_ma20 * args.min_vol) \
            & (prev_hi60 > 0) & (close > prev_hi60 * 1.01)
        raw_idx = np.where(raw)[0]
        if len(raw_idx) and 20 <= raw_idx[0] <= n - HORIZON - 1:
            # 冷却去重
            sel = [raw_idx[0]]
            for i in raw_idx[1:]:
                if i - sel[-1] >= COOLDOWN and i + HORIZON < n:
                    sel.append(i)
            for i in sel:
                if i + HORIZON >= n:
                    continue
                r = float(close[i + HORIZON] / close[i] - 1)
                bp = float(boll_pct[i]) if boll_pct is not None and np.isfinite(boll_pct[i]) else None
                cands.append({"sym": sym, "idx": i, "r": r, "vr":
                              float(volume[i] / max(vol_ma20[i], 1e-9)),
                              "boll_pct": bp})
        # 池基准: 每只随机采样 30 个 bar 的未来收益
        rng = np.random.default_rng(k)
        for _ in range(30):
            i = int(rng.integers(40, n - HORIZON - 1))
            pool["rets"].append(float(close[i + HORIZON] / close[i] - 1))
            pool["n"] += 1
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s) "
                  f"候选累计 {len(cands)}", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s, JOC 候选 {len(cands)}")

    if not cands:
        print("无 JOC 候选 (放宽定义仍 0 产出?)")
        return

    rets = np.array([c["r"] for c in cands])
    pool_up = np.mean([r > 0 for r in pool["rets"]])
    up = np.mean(rets > 0)
    print(f"\n== JOC 放宽候选 20 根方向 ==")
    print(f"  池基准20根上涨: {pool_up * 100:.1f}%  (n={pool['n']})")
    print(f"  JOC 候选: n={len(cands)} 上涨占比={up * 100:.1f}% "
          f"增量={ (up - pool_up) * 100:+5.1f}pt 均值={np.mean(rets) * 100:+6.2f}%")

    # 子分桶: boll_pct 高/低、vr 高/中
    print("\n-- 子分桶 --")
    for name, cond in (
        ("boll_pct>0.8", lambda c: c["boll_pct"] is not None and c["boll_pct"] > 0.8),
        ("boll_pct<=0.8", lambda c: c["boll_pct"] is not None and c["boll_pct"] <= 0.8),
        ("vr>=2.4", lambda c: c["vr"] >= 2.4),
        ("vr<2.4", lambda c: c["vr"] < 2.4),
    ):
        sel = [c for c in cands if cond(c)]
        if not sel:
            continue
        rr = np.array([c["r"] for c in sel])
        print(f"  [{name:<12s}] n={len(sel):<5d} 上涨占比={np.mean(rr > 0) * 100:5.1f}% "
              f"增量={ (np.mean(rr > 0) - pool_up) * 100:+5.1f}pt")


if __name__ == "__main__":
    main()