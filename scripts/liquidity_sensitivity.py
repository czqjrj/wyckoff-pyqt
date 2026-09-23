"""5.5 极端流动性敏感性 · 涨跌停约束对 20 根方向化收益的影响 (按标的去重缓存版)。

动机: 保守/组合回测全部用收盘价成交 (ret20), 未计"涨停买不进 / 跌停卖不出"。
实盘 paper 引擎 `market_rules.limit_blocked` 已有该约束 (封板顺延), 但回测没有。
本脚本用本地 SQLite K线缓存重建每条强多头事件记录信号日之后逐 bar 涨跌停
旗标 (与 indicators.limit_up/limit_dn 同口径, 每标的只载入一次缓存):

  - 入场受阻率: 信号日 (date 对应 bar) 是否涨停封板 → 无法按买入价成交;
  - 出场受阻率: 20 根窗口内含跌停封板 bar 的比例 → ret20 可能无法兑现;
  - 窗口内任意封板占比 (涨停或跌停)。
  - 受阻碍段的收益偏移: 排除受阻记录后命中率/均值如何变化 (上界估计)。

纯本地计算 (sqldb 缓存 + 指标), 不访问网络。
用法: python scripts/liquidity_sensitivity.py [max_symbols]
"""
from __future__ import annotations

import collections
import os
import sys

os.environ.setdefault("WYCKOFF_CACHE_DB", "wyckoff_cache.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wyckoff.signal_accuracy import load_signals

TIERS = ("Spring", "Shakeout", "ST", "LPS")
H = "20"
LIMIT_BARS = int(H)


def _locate_idx(df, date_str):
    """在 df 中定位 date_str 对应的 bar 索引。"""
    if df is None or len(df) == 0:
        return None
    s = df["day"].astype(str)
    idx = np.where(s.values == str(date_str)[:10])[0]
    if len(idx):
        return int(idx[-1])
    idx = np.where(s.str.startswith(str(date_str)[:10]).values)[0]
    if len(idx):
        return int(idx[-1])
    return None


def _load_cached_df(code) :
    """本地缓存读该标的日线 (过期时间放超大)。返回 df 或 None。"""
    from wyckoff import sqldb
    for key in (str(code), ):
        key = key.strip()
        if not key:
            continue
        try:
            hit = sqldb.kline_load(key, 240, 10 ** 12)
        except Exception:
            hit = None
        if hit is not None:
            return hit[0]
    return None


def main(max_symbols: int = 0) -> int:
    records = load_signals()
    evs = [r for r in records
           if r.get("kind") == "event" and r.get("type") in TIERS
           and (r.get("results") or {}).get(H) is not None]
    print(f"信号库: {len(records)} · 强多头事件已评估{H}根 {len(evs)}",
          flush=True)

    # 按标的聚合, 只载一次缓存; 截断标的数供快速测试
    by_sym = collections.defaultdict(list)
    for r in evs:
        key = str(r.get("symbol") or r.get("code") or "")
        if key:
            by_sym[key].append(r)
    max_symbols = max_symbols or len(by_sym)
    syms = list(by_sym)[:max_symbols]
    print(f"涉及标的 {len(by_sym)} · 本次扫描 {len(syms)}", flush=True)

    agg = {"n": 0, "win": 0, "rets": [],
           "entry_blocked": 0, "exit_locked": 0, "any_lock": 0,
           "clean_rets": [], "locked_any_rets": [], "entry_blocked_rets": []}
    nocache = 0
    for k, sym in enumerate(syms, 1):
        df = _load_cached_df(sym)
        if df is None or len(df) == 0 or "close" not in df.columns:
            nocache += len(by_sym[sym])
            continue
        if "limit_up" not in df.columns:
            from wyckoff.indicators import add_indicators
            _df = add_indicators(df, symbol=sym)
            if _df is not None and len(_df) == len(df):
                df = _df
        up = (np.asarray(df["limit_up"], dtype=bool)
              if "limit_up" in df.columns else np.zeros(len(df), bool))
        dn = (np.asarray(df["limit_dn"], dtype=bool)
              if "limit_dn" in df.columns else np.zeros(len(df), bool))
        for r in by_sym[sym]:
            i = _locate_idx(df, r.get("date", ""))
            if i is None:
                nocache += 1
                continue
            end = min(len(df), i + LIMIT_BARS)
            if end <= i:
                nocache += 1
                continue
            u = up[i:end]
            d = dn[i:end]
            ret = (r.get("results") or {}).get(H, {}).get("ret")
            if ret is None:
                continue
            agg["n"] += 1
            agg["rets"].append(ret)
            if ret > 0:
                agg["win"] += 1
            entry_blocked = bool(u[0])
            exit_lock_win = bool(np.any(d))
            any_lock = bool(np.any(u) or np.any(d))
            agg["entry_blocked"] += int(entry_blocked)
            agg["exit_locked"] += int(exit_lock_win)
            agg["any_lock"] += int(any_lock)
            if entry_blocked:
                agg["entry_blocked_rets"].append(ret)
            if not (entry_blocked or exit_lock_win):
                agg["clean_rets"].append(ret)
            if not any_lock:
                agg["locked_any_rets"].append(ret)
        if k % 250 == 0:
            print(f"  已处理 {k}/{len(syms)} 标的 ...", flush=True)

    n = agg["n"]
    if not n:
        print("无缓存命中, 退出。")
        return 1

    def _stat(name, rs):
        if not rs:
            return f"  {name:<26s} n=0"
        win = sum(1 for x in rs if x > 0) / len(rs)
        mean = sum(rs) / len(rs)
        return (f"  {name:<26s} n={len(rs):<7d} 命中={win*100:5.1f}% "
                f"20根均值={mean*100:+6.3f}%")

    print(f"可评估: {n} 条 · 缺缓存 {nocache} 条")
    print("\n[涨跌停触达率]")
    print(f"  入场涨停封板 (买不进)      : {agg['entry_blocked']:6d}  "
          f"{agg['entry_blocked']/n*100:5.1f}%")
    print(f"  窗口内跌停封板 (卖不出风险) : {agg['exit_locked']:6d}  "
          f"{agg['exit_locked']/n*100:5.1f}%")
    print(f"  窗口内任一涨/跌停 (受限)   : {agg['any_lock']:6d}  "
          f"{agg['any_lock']/n*100:5.1f}%")

    print("\n[排除受阻记录后的收益偏移]")
    print(_stat("全部 (收盘口径)", agg["rets"]))
    print(_stat("仅排除入场受阻", agg["clean_rets"]))
    print(_stat("排除窗口内任一封板", agg["locked_any_rets"]))

    print("\n[入场涨停受阻组的收益特征]")
    print(_stat("入场涨停受阻组", agg["entry_blocked_rets"]))

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    limit = 0
    if len(sys.argv) > 1 and sys.argv[1].strip().isdigit():
        limit = int(sys.argv[1])
    raise SystemExit(main(limit))
