"""价值吸筹入场/事件门槛调查: 释放全量价值候选, 按事件类型/conf 分层统计未来收益。

加载股票缓存 (scripts/paper_priority_bt.py --stocks-cache 产出), 对每只股票只在
"近20根内出现 LONG_EVENT" 的 bar 上跑 va_candidate (与回放同一判据, pbt._va_cache
全进程复用), 收集全部价值候选。对每条候选计算:
  - 未来收益: 持有 5/10/20 根 (用当日 open 买入, 未来 close 卖出, 不含成本)
  - 固定规则模拟: 止损3%/止盈15%/持≤20根/成本0.004 (贴近账户回放规则)

输出分层表: 事件类型 / conf 桶 / 类型×conf / 确认式前置(事件后首收上MA10 距事件根数)。
用法:
  python scripts/va_event_survey.py --stocks-cache /tmp/pp_pool80.pkl
"""

import argparse
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/scripts")

from scripts import paper_replay_bt as pbt
from wyckoff import paper

TAKE = 0.15
STOP = 0.03
COST = 0.004
HOLD = 20


def rule_ret(closes, opens, buy_i):
    """固定止盈/止损/持有规则的单笔收益 (收盘判定, 无盘中高/低, 略保守且与回放近似)."""
    if buy_i + 1 >= len(closes):
        return 0.0
    entry = opens[buy_i]
    if entry <= 0:
        return 0.0
    for k in range(1, HOLD + 1):
        p = buy_i + k
        if p >= len(closes):
            break
        c = closes[p]
        if c <= entry * (1 - STOP):
            return c / entry - 1 - COST
        if c >= entry * (1 + TAKE):
            return c / entry - 1 - COST
    c = closes[min(buy_i + HOLD, len(closes) - 1)]
    return c / entry - 1 - COST


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks-cache", required=True)
    ap.add_argument("--save", default="", help="候选+收益落盘路径 (pickle), 供时间稳定性复查")
    args = ap.parse_args()

    with open(args.stocks_cache, "rb") as f:
        stocks = pickle.load(f)
    print(f"加载股票: {len(stocks)} 只", flush=True)
    va_m = paper._strategy_manager()
    by_code = {r["code"]: r for r in stocks}

    recs = []
    for k, rec in enumerate(stocks):
        idxs = set()
        for e in rec["all_evs"] or []:
            if e.get("type") not in paper.LONG_EVENT_TYPES:
                continue
            ei = int(e.get("idx") or 0)
            for jj in range(ei, min(ei + 21, len(rec["df"]))):
                idxs.add(jj)
        for j in sorted(idxs):
            va = pbt.va_candidate(rec, j, va_m)
            if va is None:
                continue
            ei = int(va.get("idx") or 0)
            ci = pbt.va_confirm_idx(rec, ei)
            recs.append(
                {
                    "code": rec["code"],
                    "type": va["type"],
                    "conf": int(va.get("conf") or 0),
                    "j": j,
                    "confirm_gap": (ci - ei) if ci is not None else None,
                }
            )
        if (k + 1) % 10 == 0:
            print(f"  扫描 {k + 1}/{len(stocks)} 只 ... 候选累计 {len(recs)}", flush=True)

    print(f"价值候选总样本: {len(recs)}")
    if not recs:
        return
    # 去重 (同一 (code,j) 只留一条)
    seen = set()
    uniq = []
    for r in recs:
        key = (r["code"], r["j"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    recs = uniq
    print(f"去重后样本: {len(recs)}")

    def _enrich(items):
        rows = []
        for it in items:
            rec = by_code[it["code"]]
            o = float(rec["open"][it["j"]])
            cl = rec["close"]
            futs = {}
            for h in (5, 10, 20):
                p = it["j"] + h
                futs[h] = (cl[p] / o - 1) if p < len(cl) else None
            rows.append({
                "code": it["code"],
                "type": it["type"],
                "conf": it["conf"],
                "j": it["j"],
                "frac": it["j"] / max(1, len(cl) - 1),
                "confirm_gap": it["confirm_gap"],
                "futs": futs,
                "rule": rule_ret(cl, rec["open"], it["j"]),
            })
        return rows

    rows = _enrich(recs)
    if args.save:
        with open(args.save, "wb") as f:
            pickle.dump(rows, f)
        print(f"候选+收益已落盘: {len(rows)} 条 -> {args.save}")

    def _stats(items, label):
        n = len(items)
        if n == 0:
            return
        futs = {h: [r["futs"][h] for r in items if r["futs"][h] is not None] for h in (5, 10, 20)}
        rets = [r["rule"] for r in items]

        def m(x):
            return sum(x) / len(x) if x else float("nan")

        def wr(x):
            return 100.0 * sum(1 for v in x if v > 0) / len(x) if x else float("nan")

        print(
            f"{label:40s} n={n:<4d} 命中5={wr(futs[5]):5.1f}% "
            f"命中10={wr(futs[10]):5.1f}% 命中20={wr(futs[20]):5.1f}% "
            f"| 规则模拟 胜率={wr(rets):5.1f}% 平均={100 * m(rets):+6.2f}%"
        )

    def _time_stats(items, label):
        _stats([it for it in items if it["frac"] < 0.5], f"{label} [时间前半]")
        _stats([it for it in items if it["frac"] >= 0.5], f"{label} [时间后半]")

    print("\n===== 时间稳定性 (每股全程前/后半) =====")
    _time_stats(rows, "全部候选")
    for t in sorted({r["type"] for r in rows}):
        _time_stats([r for r in rows if r["type"] == t], f"类型={t}")
    _time_stats([r for r in rows if r["conf"] >= 100], "conf>=100")
    _time_stats([r for r in rows if r["conf"] < 100], "conf<100")
    _time_stats(
        [r for r in rows if r["confirm_gap"] is not None and r["confirm_gap"] <= 2],
        "确认距事件<=2根",
    )

    print("\n===== 按事件类型 =====")
    for t in sorted({r["type"] for r in rows}):
        _stats([r for r in rows if r["type"] == t], f"类型={t}")

    print("\n===== 按 conf 桶 =====")
    for lo, hi, name in [
        (150, 10**9, "conf>=150"),
        (130, 149, "conf 130-149"),
        (110, 129, "conf 110-129"),
        (100, 109, "conf 100-109"),
        (0, 99, "conf<100"),
    ]:
        _stats([r for r in rows if lo <= r["conf"] <= hi], f"conf {name}")

    print("\n===== 类型 X conf 桶 (n>=8) =====")
    for t in sorted({r["type"] for r in rows}):
        for lo, hi, name in [
            (130, 10**9, "conf>=130"),
            (100, 129, "conf 100-129"),
            (0, 99, "conf<100"),
        ]:
            grp = [r for r in rows if r["type"] == t and lo <= r["conf"] <= hi]
            if len(grp) >= 8:
                _stats(grp, f"{t} x {name}")

    print("\n===== 确认式前置 =====")
    _stats([r for r in rows if r["confirm_gap"] is not None], "有确认(事件后收上MA10)")
    _stats([r for r in rows if r["confirm_gap"] is None], "无确认(始终未收上MA10)")
    for gap in (0, 2, 5):
        _stats(
            [r for r in rows if r["confirm_gap"] is not None and r["confirm_gap"] <= gap],
            f"确认距事件<= {gap} 根",
        )


if __name__ == "__main__":
    main()
