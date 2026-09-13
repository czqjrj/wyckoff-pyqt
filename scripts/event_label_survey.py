"""离线事件层命中率调查: 直接读 SQLite 缓存K线, 跑 detect_all, 汇总各事件后续方向。

口径与 VSA 调查一致:
  - 每事件未来 20 根方向化命中: 多头事件 → ret>0 记命中; 空头事件 → ret<0 记命中;
  - SC/BC 按"确认后"反转口径归向 (SC=恐慌抛售后看反弹→多, BC=狂热上冲后看回落→空);
  - 冷却窗去重: 同标的同类型 15 根内只保留一条。
用途: 找出贴近/劣于随机的事件类型 (重点 SOS/JOC/PSY), 供收紧检测条件。
用法:
  python scripts/event_label_survey.py --limit 6000 [--only SOS,JOC,PSY]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.config import event_dir, _REVERSAL_CONFIRM_DIR
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


def _dir(typ):
    if typ in _REVERSAL_CONFIRM_DIR:
        return _REVERSAL_CONFIRM_DIR[typ]
    return event_dir(typ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--horizon", type=int, default=20)
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    stats = {}
    pool = []
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
        last = {}
        n = len(df)
        close = df["close"].values
        for e in evs:
            typ = e.get("type", "?")
            if only and typ not in only:
                continue
            i = int(e["idx"])
            if i < 0 or i + args.horizon >= n:
                continue
            prev = last.get(typ, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[typ] = i
            d = _dir(typ)
            rec = stats.setdefault(typ, {"n": 0, "hits": 0, "rets": [],
                                         "conf": []})
            rec["n"] += 1
            rec["conf"].append(float(e.get("conf", 50)))
            r = float(close[i + args.horizon] / close[i] - 1)
            rec["rets"].append(r)
            if d != 0:
                pool.append(r)
                if (r < 0 if d < 0 else r > 0):
                    rec["hits"] += 1
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s, 事件样本 "
          f"{sum(s['n'] for s in stats.values())}")

    p_up = np.mean([r > 0 for r in pool]) if pool else 0.0
    print(f"池基准 (方向事件 20根上涨占比): {p_up * 100:.1f}%  (n={len(pool)})")

    print("\n== 各事件 20 根方向命中率 (相对池基准) ==")
    rows = []
    small = []
    for typ, rec in stats.items():
        d = _dir(typ)
        if d == 0:
            continue
        wr = rec["hits"] / rec["n"] if rec["n"] else 0.0
        item = (typ, rec["n"], wr, np.mean(rec["rets"]), np.mean(rec["conf"]))
        (rows if rec["n"] >= 20 else small).append(item)
    for typ, n, wr, mean, conf in sorted(rows, key=lambda x: -x[2]):
        lift = (wr - p_up) * 100
        print(f"  {typ:<8s} n={n:<6d} 命中={wr * 100:5.1f}% 相对基准={lift:+5.1f}pt "
              f"均值={mean * 100:+6.2f}% 置信={conf:.0f}")
    if small:
        print("  — 低样本方向事件 (n<20): " +
              ", ".join(f"{t}(n={n}, 命中={wr * 100:.0f}%)" for t, n, wr, _, _ in small))
    neut = [(t, s["n"]) for t, s in stats.items() if _dir(t) == 0 and s["n"] >= 20]
    if neut:
        print("\n== 中性事件大样本 (未计命中) ==")
        for t, n in sorted(neut, key=lambda x: -x[1]):
            print(f"  {t:<8s} n={n}")


if __name__ == "__main__":
    main()