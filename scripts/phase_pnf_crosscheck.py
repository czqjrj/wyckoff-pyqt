"""相位拐点 × PnF 方向 交叉验证: 检验"拐点处 PnF 净方向与期望方向一致"是否
提升段末 20 根方向命中 (markdown/markup 结合点数图的依据)。

方法:
- 对缓存 240 分钟标的重放 phase_segments (默认自适应), 取历史段, 段末 e 为拐点;
- 用与 analysis 一致的 box 重建逐K线 PnF 列方向 (walkforward: 列内新高/新低
  计续column, 反向走 reversal*box 即翻转), 得 col_dir[i] ∈ {-1,0,+1};
- 按段末 e 处 PnF 方向与相位期望方向 (EXPECT) 是否一致分组, 对比段末 20 根
  期望方向命中率。一致组命中显著高于不一致组 → PnF 强化拐点判断有增量。

用法:
  python scripts/phase_pnf_crosscheck.py --limit 120
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff import sqldb
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.phases import phase_segments
from wyckoff.pnf import build_pnf

HORIZON = 20
EXPECT = {"accumulation": 1, "markdown": 1, "distribution": -1, "markup": -1}


def col_dir_series(close, box, reversal=3):
    """逐K线 PnF 列方向 (-1 下跌列 / +1 上涨列 / 0 未定)。"""
    n = len(close)
    arr = np.zeros(n, dtype=int)
    if n == 0 or box <= 0:
        return arr
    direction = 1 if close[0] >= close[1] else -1 if n > 1 else 0
    col_hi = col_lo = float(close[0])
    for i, c in enumerate(close):
        if direction >= 0:
            if c >= col_hi:
                col_hi = c
            elif c <= col_hi - reversal * box:
                direction = -1
                col_lo = c
        else:
            if c <= col_lo:
                col_lo = c
            elif c >= col_lo + reversal * box:
                direction = 1
                col_hi = c
        arr[i] = direction
    return arr


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
    ap.add_argument("--horizon", type=int, default=20)
    args = ap.parse_args()
    H = args.horizon

    syms = cached_klines()
    if args.skip:
        syms = syms[args.skip:args.skip + args.limit]
    else:
        syms = syms[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    stats = {}  # key -> {"n","hit", 一致组, 不一致组, 无方向数}
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
        try:
            segs = phase_segments(df, pivots, events)
        except Exception:
            continue
        close = df["close"].values
        n = len(df)
        try:
            _cols, box = build_pnf(df)   # 与 analysis 同 box 口径 (默认 pct)
        except Exception:
            continue
        col_dir = col_dir_series(close, box)

        rec = stats.setdefault("_n_stat", {"seg": 0, "shadow": 0})
        for a, e, key, _label in segs:
            if key not in EXPECT or e + H >= n:
                continue
            rec["seg"] += 1
            rec["shadow"] += int(col_dir[e] == 0)
            expect_up = EXPECT[key] > 0
            truth_up = float(close[e + H]) > close[e]
            hit = int(truth_up == expect_up)
            d = stats.setdefault(key, {"n": 0, "hit": 0, "same": 0, "same_h": 0,
                                       "diff": 0, "diff_h": 0})
            d["n"] += 1
            d["hit"] += hit
            if col_dir[e] != 0:
                same = (col_dir[e] > 0) == expect_up
                if same:
                    d["same"] += 1
                    d["same_h"] += hit
                else:
                    d["diff"] += 1
                    d["diff_h"] += hit
        done += 1
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s\n", flush=True)

    rec = stats.get("_n_stat", {})
    print(f"horizon={H}: 拐点段 n={rec.get('seg', 0)}, "
          f"段末 PnF 无方向(跳过)={rec.get('shadow', 0)}\n")
    tot = alls = allh = 0
    for key in ("markdown", "accumulation", "markup", "distribution"):
        d = stats.get(key)
        if not d or d["n"] == 0:
            print(f"  {key:<12s} 无样本")
            continue
        tot += d["n"]
        alls += d["n"]
        allh += d["hit"]
        s_rate = d["same_h"] / d["same"] if d["same"] else 0.0
        f_rate = d["diff_h"] / d["diff"] if d["diff"] else 0.0
        base = d["hit"] / d["n"]
        print(f"  {key:<12s} n={d['n']:<4d} 全样本命中={base * 100:5.1f}%  "
              f"PnF一致={s_rate * 100:5.1f}%(n={d['same']:>3d})  "
              f"PnF相反={f_rate * 100:5.1f}%(n={d['diff']:>3d})  "
              f"增益={s_rate - f_rate:+.1f}pt")
    if alls:
        print(f"\n  合计: 全样本命中={allh / alls * 100:.1f}%  (n={alls})")


if __name__ == "__main__":
    main()
