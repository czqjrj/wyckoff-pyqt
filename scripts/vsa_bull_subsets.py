"""VSA 看多标签复查: DEM/ETR/SPR/SV 按量能/幅宽/收盘位置/趋势多维度分桶。

待办背景 (docs/event_env_gate_progress.md):
  DEM/ETR/SPR/SV 全量 trend 分桶两环境均贴近随机 (scripts/vsa_by_trend.py),
  需按"阶段/量能分位再挖子集"。本脚本把看多标签按独立特征 (vr/rw/cpos/trend)
  分桶, 找是否存在高命中子集 (命中显著高于池基准)。

口径与 vsa_by_trend.py 一致:
  - 每信号未来 20 根方向化命中: 看多 → ret>0 记命中;
  - 同标的同标签 15 根冷却窗去重。

用法:
  python scripts/vsa_bull_subsets.py --limit 6000 [--only DEM,ETR,SPR,SV]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.indicators import add_indicators
from wyckoff import sqldb
from wyckoff.vsa import vsa_classify

HORIZON = 20
COOLDOWN = 15

# 目标看多标签
DEFAULT_ONLY = ("DEM", "ETR", "SPR", "SV")


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
    from wyckoff.fusion import VSA_BULL, VSA_BEAR
    return 1 if lb in VSA_BULL else (-1 if lb in VSA_BEAR else 0)


def _bk(name, conds):
    return {name: {c: {"n": 0, "hits": 0, "rets": []} for c in conds}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--only", type=str, default=",".join(DEFAULT_ONLY))
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}

    syms = cached_klines()[:args.limit]
    print(f"缓存标的 {len(syms)} 只 (scale=240)", flush=True)

    dims = {
        "vr": {"<1.5": {}, "1.5~2.2": {}, ">=2.2": {}},
        "rw": {"<1.2": {}, "1.2~1.8": {}, ">=1.8": {}},
        "cpos": {"低位<=0.35": {}, "中间": {}, "高位>=0.7": {}},
        "trend": {"空头(0)": {}, "多头(1)": {}},
    }
    overall = {"n": 0, "hits": 0, "rets": []}
    pool = {"n": 0, "rets": []}  # 全部看多标签基线
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
        close = df["close"].values
        n = len(df)
        last = {}
        for s in sigs:
            lb = s.get("label", "?")
            if only and lb not in only:
                continue
            if _vsa_dir(lb) != 1:  # 只看多标签
                continue
            i = int(s["idx"])
            if i < 0 or i + HORIZON >= n:
                continue
            prev = last.get(lb, -10 ** 9)
            if i - prev < COOLDOWN:
                continue
            last[lb] = i
            r = float(close[i + HORIZON] / close[i] - 1)
            hit = r > 0
            overall["n"] += 1
            overall["hits"] += int(hit)
            overall["rets"].append(r)
            pool["n"] += 1
            pool["rets"].append(r)
            f = s.get("features") or {}
            vr = f.get("vr", 0.0) or 0.0
            rw = f.get("rw", 1.0) or 1.0
            cpos = f.get("cpos", 0.5) or 0.5
            trend = int(f.get("trend", 0) or 0)
            vr_b = "<1.5" if vr < 1.5 else ("1.5~2.2" if vr < 2.2 else ">=2.2")
            rw_b = "<1.2" if rw < 1.2 else ("1.2~1.8" if rw < 1.8 else ">=1.8")
            cpos_b = "低位<=0.35" if cpos <= 0.35 else ("高位>=0.7" if cpos >= 0.7 else "中间")
            tr_b = "空头(0)" if trend == 0 else "多头(1)"
            for dim, conds in dims.items():
                buck = conds[vr_b if dim == "vr" else rw_b if dim == "rw"
                            else cpos_b if dim == "cpos" else tr_b]
                lbrec = buck.setdefault(lb, {"n": 0, "hits": 0, "rets": []})
                lbrec["n"] += 1
                lbrec["hits"] += int(hit)
                lbrec["rets"].append(r)
        done += 1
        if (k + 1) % 1000 == 0:
            print(f"  {k + 1}/{len(syms)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"完成 {done} 只, 用时 {time.time() - t0:.0f}s")

    o = overall
    ohr = o["hits"] / o["n"] * 100 if o["n"] else 0.0
    p_up = np.mean([r > 0 for r in pool["rets"]]) if pool["n"] else 0.0
    print(f"\n== 总览: n={o['n']} 命中={ohr:.1f}% 池基准20根上涨={p_up * 100:.1f}% ==")

    for dim, conds in dims.items():
        print(f"\n-- {dim} --")
        for cond, buck in conds.items():
            tot = sum(b["n"] for b in buck.values())
            if not tot:
                continue
            all_hit = sum(b["hits"] for b in buck.values())
            all_ret = [r for b in buck.values() for r in b["rets"]]
            hr = all_hit / tot * 100
            print(f"  [{cond:<12s}] n={tot:<6d} 命中={hr:5.1f}% "
                  f"相对={hr - p_up * 100:+5.1f}pt")
            rows = []
            for lb, b in buck.items():
                if not b["n"]:
                    continue
                rows.append((lb, b["n"], b["hits"] / b["n"], np.mean(b["rets"])))
            for lb, nn, wr, mean in sorted(rows, key=lambda x: -x[2]):
                print(f"      {lb:<5s} n={nn:<5d} 命中={wr * 100:5.1f}% "
                      f"相对={ (wr - p_up) * 100:+5.1f}pt 均值={mean * 100:+6.2f}%")


if __name__ == "__main__":
    main()