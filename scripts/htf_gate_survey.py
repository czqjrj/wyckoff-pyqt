"""5.2 多周期共振门 · 历史实证 survey (回测对照)。

用 wx_signal_accuracy.json 的强多头事件记录 (Spring/Shakeout/ST/LPS,
已评估出 20 根收益) 结合本地 SQLite K线缓存, 重构每个信号日"当周"的周/月线
方向 (只用信号日之前的已完成周, 无前视), 分组对比:

    门禁将拦下的记录 (htf=-1, 周/月线偏空)  vs  放行的记录 (htf∈{0,+1})

在方向化 20 根命中率 / 均值收益 上的差异。若偏空组系统性劣化 → 支持
ENTRY_HTF_GATE 作为入场硬门禁; 同时输出它会拦下的样本占比 (验收目标: n 下降
<30%), 以及 fail-open / fail-close 两档的保留比例。

用法: python scripts/htf_gate_survey.py
依赖: 仅本地缓存 (sqldb.kline_load) + wx_signal_accuracy.json, 不访问网络。
"""
from __future__ import annotations

import sys
import time

import numpy as np

from wyckoff import sqldb
from wyckoff.config import STRONG_TIER_TYPES, event_dir
from wyckoff.multitime import (
    BEAR_PHASES,
    BULL_PHASES,
    _current_phase,
    monthly_resample,
    weekly_resample,
)
from wyckoff.signal_accuracy import load_signals

# 只评估可交易口径里命中能力最强的标称多头强梯队 (与 ENTRY_TIERS 一致)
LONG_STRONG = frozenset(t for t in STRONG_TIER_TYPES if event_dir(t) > 0)

_BEAR = "bear"
_OK = "ok"
_UNKNOWN = "unknown"


def _phase_of(phase_str):
    """阶段文本 → bear/ok/unknown (基点取 split(' ')[0], 语义同 _htf_direction)。"""
    base = (phase_str or "").split(" ")[0]
    if base in BEAR_PHASES:
        return _BEAR
    if base in BULL_PHASES:
        return _OK
    return _UNKNOWN


def _prior_phase(series_fn, df, date, order):
    """信号日之前的已完成周期方向 (无前视): series_fn 把日线聚合到周/月线。

    返回 (bear/ok/unknown)。规则: 取第一个"期末 >= 信号日"的周期 bar k, 用
    其之前的已完成周期 wdf.iloc[:k] 判方向; k 过小 (至少 order+1 根) → unknown。
    """
    try:
        wdf = series_fn(df)
        if wdf is None or len(wdf) == 0:
            return _UNKNOWN
        ends = wdf["day"].values.astype("datetime64[ns]")
        k = int(np.searchsorted(ends, np.datetime64(str(date)[:10]), side="left"))
        if k < order + 1:
            return _UNKNOWN
        seg = wdf.iloc[:k].copy()
        from wyckoff.indicators import find_pivots
        ph = _current_phase(seg, find_pivots(seg))
        return _phase_of(ph)
    except Exception:
        return _UNKNOWN


def _load_cached_df(rec):
    """本地缓存读该标的日线 (原始代码优先, 回退规范化 symbol), 过期时间放超大。"""
    for key in (str(rec.get("code") or ""), str(rec.get("symbol") or "")):
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
    t0 = time.time()
    records = load_signals()
    evs = [r for r in records
           if r.get("kind") == "event" and r.get("type") in LONG_STRONG]
    evs20 = [r for r in evs if (r.get("results") or {}).get("20")]
    print(f"信号库: 记录 {len(records)} · 强多头事件 {len(evs)} · "
          f"已评估20根 {len(evs20)}")

    from wyckoff.indicators import add_indicators as _ai  # noqa: F401 (保留接口)
    per_symbol = {}
    for r in evs20:
        per_symbol.setdefault(str(r.get("symbol")), []).append(r)
    print(f"涉及标的: {len(per_symbol)}")

    order = 3
    buckets = {"weekly": {_BEAR: {"n": 0, "wins": 0, "rets": []},
                          _OK: {"n": 0, "wins": 0, "rets": []},
                          _UNKNOWN: {"n": 0, "wins": 0, "rets": []}},
               "htf": {_BEAR: {"n": 0, "wins": 0, "rets": []},
                       _OK: {"n": 0, "wins": 0, "rets": []},
                       _UNKNOWN: {"n": 0, "wins": 0, "rets": []}}}
    skipped_nocache = matched = 0

    for sym, recs in per_symbol.items():
        df = _load_cached_df(recs[0])
        if df is None:
            skipped_nocache += len(recs)
            continue
        for rec in recs:
            res = (rec.get("results") or {}).get("20") or {}
            ret20 = res.get("ret_c")
            if ret20 is None:
                ret20 = res.get("ret")
            if ret20 is None:
                continue
            date = str(rec.get("date", ""))
            w_ph = _prior_phase(weekly_resample, df, date, order)
            m_ph = _prior_phase(monthly_resample, df, date, 1)
            # htf 汇总结论与 multitime.htf_direction 同语义: 任一偏空 →
            # -1; 任一偏多在无偏空时 → +1; 否则 0/unavailable。
            if _BEAR in (w_ph, m_ph):
                htf = _BEAR
            elif _OK in (w_ph, m_ph):
                htf = _OK
            elif w_ph == _UNKNOWN and m_ph == _UNKNOWN:
                htf = _UNKNOWN
            else:
                htf = _OK
            d = event_dir(rec.get("type", ""))
            hit = ret20 > 0 if d >= 0 else ret20 < 0
            for key, ph in (("weekly", w_ph), ("htf", htf)):
                b = buckets[key][ph]
                b["n"] += 1
                if hit:
                    b["wins"] += 1
                b["rets"].append(ret20)
            matched += 1

    print(f"可评估: {matched} 条 (命中缓存) · 缺缓存跳过 {skipped_nocache} 条")

    def _row(name, b):
        n = b["n"]
        if not n:
            return f"  {name:<8s} n=0"
        win = b["wins"] / n
        mean = sum(b["rets"]) / n
        return (f"  {name:<8s} n={n:<5d} 命中率={win*100:5.1f}%  "
                f"20根均值={mean*100:+6.3f}%")

    print("\n── 按当周周线方向分组 (仅强多头事件, 20根确认可交易口径) ──")
    for k in ("weekly", "htf"):
        label = "周线方向" if k == "weekly" else "多周期门 (周/月线合并)"
        print(f"\n[{label}]")
        wb = buckets[k]
        print(_row("偏空(bear)", wb[_BEAR]))
        print(_row("非偏空(ok)", wb[_OK]))
        print(_row("不可判", wb[_UNKNOWN]))

    h = buckets["htf"]
    bear = h[_BEAR]
    tot = matched
    if tot:
        cut_failopen = bear["n"] / tot
        cut_failclose = (bear["n"] + h[_UNKNOWN]["n"]) / tot
        print("\n── 门禁影响 ──")
        print(f"  总样本          = {tot}")
        print(f"  fail-open 拦下  = {bear['n']} ({cut_failopen*100:.1f}%)  保留 {tot-bear['n']}")
        print(f"  fail-close 拦下 = {bear['n']+h[_UNKNOWN]['n']}"
              f" ({cut_failclose*100:.1f}%)  保留 {tot-bear['n']-h[_UNKNOWN]['n']}")
        if bear["n"] >= 20 and bear["n"] / tot < 0.30:
            print("\n结论: 偏空组样本 < 30% 且方向劣化时, 支持接入 ENTRY_HTF_GATE。")
    print(f"\n耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    limit = 0
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        limit = int(sys.argv[1])
    raise SystemExit(main(limit))
