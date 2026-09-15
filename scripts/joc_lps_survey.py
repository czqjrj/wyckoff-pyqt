"""JOC/LPS 链路调查: 验证 JOC 方向 + LPS/BU/SOS 命中率 + 检测产出量。

待办背景 (docs/event_env_gate_progress.md):
  - LPS 78.9% 最强第二梯队但样本仅 76;
  - JOC/BU 分支 600 只产出 0 (疑死代码), 且 JOC 方向未验证 (可能像 SOS 反向);
  - SOS 已降中性 (8f05b51), JOC 仍标多头但列弱组。

本脚本统计四类事件 (JOC/LPS/BU/SOS) 的:
  - 20 根方向命中率 (JOC/LPS/BU 按 event_dir, SOS 中性不计方向);
  - 检测产出量 (验死代码);
  - 若 JOC 产出过少, 并行验证"JOC 若标多是否正确"。

用法:
  python scripts/joc_lps_survey.py --limit 6000
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
    args = ap.parse_args()

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    TYPES = ("JOC", "LPS", "BU", "SOS")
    stats = {t: {"n": 0, "hits": 0, "rets": [], "conf": []} for t in TYPES}
    overall_d = {"n": 0, "rets": []}
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
            typ = e.get("type", "?")
            if typ not in TYPES:
                continue
            i = int(e["idx"])
            if i < 0 or i + HORIZON >= n:
                continue
            prev = last.get(typ, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[typ] = i
            d = _dir(typ)
            rec = stats[typ]
            rec["n"] += 1
            rec["conf"].append(float(e.get("conf", 50)))
            r = float(close[i + HORIZON] / close[i] - 1)
            rec["rets"].append(r)
            if d != 0:
                overall_d["rets"].append(r)
                if (r < 0 if d < 0 else r > 0):
                    rec["hits"] += 1
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    if overall_d["rets"]:
        p_up = np.mean([r > 0 for r in overall_d["rets"]])
        print(f"池基准 (方向事件 20根上涨占比): {p_up * 100:.1f}%  (n={len(overall_d['rets'])})")

    print("\n== JOC/LPS/BU/SOS 20根命中率 ==")
    for typ in TYPES:
        rec = stats[typ]
        d = _dir(typ)
        if not rec["n"]:
            print(f"  {typ:<5s} n=0  (dir={d}, 检测未产出)")
            continue
        if d == 0:
            print(f"  {typ:<5s} n={rec['n']:<6d} dir=0(中性) 均值={np.mean(rec['rets']) * 100:+.2f}%")
            continue
        wr = rec["hits"] / rec["n"] * 100
        p_up = np.mean([r > 0 for r in overall_d["rets"]]) if overall_d["rets"] else 0.5
        hit_up = rec["hits"] / rec["n"]
        rel = (hit_up - p_up) * 100
        print(f"  {typ:<5s} n={rec['n']:<6d} 命中={wr:5.1f}% 相对={rel:+5.1f}pt "
              f"conf均值={np.mean(rec['conf']):5.1f} 均值={np.mean(rec['rets']) * 100:+6.2f}%")

    # JOC/LPS 只要有者 d=1 (看多), 用绝对上涨占比复核 (看多正确性独立于池基准)
    print("\n== 绝对口径复核 (看多类型 20根上涨占比) ==")
    base_up = np.mean([r > 0 for r in overall_d["rets"]]) if overall_d["rets"] else 0.5
    print(f"  池基准20根上涨: {base_up * 100:.1f}%")
    for typ in ("JOC", "LPS", "BU"):
        rec = stats[typ]
        if not rec["n"]:
            continue
        up = np.mean([r > 0 for r in rec["rets"]])
        print(f"  {typ:<5s} 上涨占比={up * 100:5.1f}%  增量={ (up - base_up) * 100:+5.1f}pt")


if __name__ == "__main__":
    main()