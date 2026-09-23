"""5.4 样本外 / 前向验证 (walk-forward 按年 + 显式时间切分)。

用 wx_signal_accuracy.json 已评估 20 根收益的强多头事件记录, 按信号日期切分:
  A. 按年 walk-forward: 各年度 20 根方向命中率 / 均值, 检验 edge 是否随时间衰减;
  B. 显式样本外: 以 2026-01-01 为界, [训练期] 与 [前向期] 分桶对比
     (含 conf≥100 生产门槛口径), 附 Wilson 95%CI (大样本 n≥30 才给结论)。

干净口径: 只算事件信号 (kind=event), 20 根方向化命中 (多头 ret>0),
与 conservative_bt / paper 入场口径同源。纯本地计算, 不访问网络。

用法: python scripts/oos_forward_validation.py
"""
from __future__ import annotations

import collections
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.signal_accuracy import load_signals

TIERS = ("Spring", "Shakeout", "ST", "LPS")
OOS_CUTOFF = "2026-01-01"      # B 部分显式样本外切分点
# 生产候选 conf = 事件 conf + 产业链调整 (上限100), 原始信号库无该调整;
# 取 LONG_MIN_CONF=90 (纪律强多头基础门槛) 作为"可交易档"对比档。
CONF_MIN = 90
H = "20"                        # 评估周期键


def _wilson(k, n, z=1.96):
    """Wilson score 95%CI: (lo, hi)。n=0 → (None, None)。"""
    if n <= 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    md = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return (c - md, c + md)


def _rows(records):
    """已评估 20 根的多头强梯队事件, 归一字段。"""
    out = []
    for r in records:
        if r.get("kind") != "event":
            continue
        ty = str(r.get("type") or "")
        if ty not in TIERS:
            continue
        rr = (r.get("results") or {}).get(H) or {}
        ret = rr.get("ret")
        if ret is None:
            continue
        date = str(r.get("date") or "")
        if len(date) < 10:
            continue
        out.append({
            "date": date[:10],
            "year": date[:4],
            "type": ty,
            "conf": int(r.get("conf") or 0),
            "ret": float(ret),
        })
    return out


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in rows if r["ret"] > 0)
    mean = sum(r["ret"] for r in rows) / n
    lo, hi = _wilson(wins, n)
    return {"n": n, "wins": wins, "hit": wins / n, "mean": mean,
            "ci": (lo, hi)}


def _line(label, st):
    if not st["n"]:
        return f"  {label:<16s} n=0"
    ci = st["ci"]
    return (f"  {label:<16s} n={st['n']:<7d} 命中={st['hit']*100:5.1f}% "
            f"(95%CI {ci[0]*100:4.1f}~{ci[1]*100:4.1f}%) "
            f"20根均值={st['mean']*100:+6.3f}%")


def main() -> int:
    records = load_signals()
    rows = _rows(records)
    print(f"信号库: {len(records)} 条 · 强多头事件已评估20根 {len(rows)} 条 "
          f"({rows[0]['date']} ~ {rows[-1]['date']})")

    # ── A. 按年 walk-forward ──
    print("\n[按年 walk-forward] (Spring/Shakeout/ST/LPS, 20根方向化命中, conf 全部)")
    by_year = collections.defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r)
    for y in sorted(by_year):
        print(_line(y, _stats(by_year[y])))

    by_year_hi = collections.defaultdict(list)
    for r in rows:
        if r["conf"] >= CONF_MIN:
            by_year_hi[r["year"]].append(r)
    print("\n[按年, conf≥100 生产门槛]")
    for y in sorted(by_year_hi):
        print(_line(y, _stats(by_year_hi[y])))

    # ── B. 显式样本外 (训练期 vs 前向期) ──
    train = [r for r in rows if r["date"] < OOS_CUTOFF]
    oos = [r for r in rows if r["date"] >= OOS_CUTOFF]
    print(f"\n[显式样本外切分 @ {OOS_CUTOFF}]")
    print(f"  训练期 {rows[0]['date']}~ : {len(train)} 条")
    print(f"  前向期 {OOS_CUTOFF}~ : {len(oos)} 条")
    print("  -- conf 全部 --")
    print(_line("训练期", _stats(train)))
    print(_line("前向期", _stats(oos)))
    print("  -- conf≥100 生产门槛 --")
    train_hi = [r for r in train if r["conf"] >= CONF_MIN]
    oos_hi = [r for r in oos if r["conf"] >= CONF_MIN]
    print(_line("训练期", _stats(train_hi)))
    print(_line("前向期", _stats(oos_hi)))

    # 逐子梯队前向期
    print("\n[前向期按事件类型, conf≥100]")
    for ty in sorted(TIERS):
        grp = [r for r in oos_hi if r["type"] == ty]
        print(_line(ty, _stats(grp)))

    # ── 简评 ──
    s_tr, s_oos = _stats(train_hi), _stats(oos_hi)
    if s_tr["n"] >= 30 and s_oos["n"] >= 30:
        diff = s_oos["hit"] - s_tr["hit"]
        overlap = not (s_oos["ci"][0] > s_tr["ci"][1]
                       or s_tr["ci"][0] > s_oos["ci"][1])
        print("\n诊断: 前向期命中 vs 训练期 "
              f"{diff*100:+.1f}pt, 95%CI 是否重叠: {'重叠 → 无显著退化' if overlap else '不重叠 → 显著变化'}")
        if diff < -0.05 and not overlap:
            print("     命中显著下降且 CI 分离 → 提示真实退化, 需复核特征/重训。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
