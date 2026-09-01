#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合搜索: 在挖掘数据上枚举条件组合, 评估胜率/盈亏比/累计, 并按时间前半/后半做稳定性检查。"""
import json, sys
import numpy as np

DATA = json.load(open(r"C:\Users\seewo\AppData\Local\Temp\opencode\mining_data.json", encoding="utf-8"))
for r in DATA:
    r["ret"] = float(r["ret"])

NET = [r for r in DATA if r.get("event_type") in ("Spring", "Shakeout")]


def stats(recs):
    if not recs:
        return None
    rets = np.array([r["ret"] for r in recs])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = abs(wins.sum() / losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    return {
        "n": len(recs),
        "wr": (rets > 0).mean() * 100,
        "avg": rets.mean() * 100,
        "pf": pf,
        "cum": float(np.prod(1 + rets) - 1) * 100,
    }


def base_stats(recs, label):
    s = stats(recs)
    print(f"=== {label}: {s}")


# 排除 ST 后, 在各单条件上分层, 找 strong 标签
def cond(name, rec):
    if name == "rsi6_50_70":
        return 50 <= rec.get("rsi6") < 70
    if name == "rsi6_ge50":
        return rec.get("rsi6") >= 50
    if name == "pos_lt07":
        return rec["pos"] < 0.7
    if name == "pos_lt05":
        return rec["pos"] < 0.5
    if name == "pos_lt04":
        return rec["pos"] < 0.4
    if name == "boll_mid":
        return 0.3 <= rec.get("boll_pct") <= 0.7
    if name == "feat_vr_ge15":
        return rec.get("feat_vr") is not None and rec.get("feat_vr") >= 1.5
    if name == "feat_vr_ge25":
        return rec.get("feat_vr") is not None and rec.get("feat_vr") >= 2.5
    if name == "feat_rw_ge2":
        return rec.get("feat_rw") is not None and rec.get("feat_rw") >= 2
    if name == "reson_ge1":
        return (rec.get("feat_reson") or 0) >= 1
    if name == "vol_z_ge2":
        return rec.get("vol_z20") is not None and rec.get("vol_z20") >= 2
    if name == "vol_z_lt1":
        return rec.get("vol_z20") is not None and rec.get("vol_z20") < 1
    if name == "ma20gt_ma50":
        return rec.get("ma20_above_ma50") is True
    if name == "below_ma20":
        return rec.get("close_above_ma20") is False
    if name == "atr_2_5":
        return rec.get("atr_pct") is not None and 2 <= rec.get("atr_pct") < 5
    if name == "confirmed_true":
        return rec.get("confirmed") is True
    return True

CAND_COND = [
    "rsi6_50_70", "rsi6_ge50", "pos_lt07", "pos_lt05", "pos_lt04", "boll_mid",
    "feat_vr_ge15", "feat_vr_ge25", "feat_rw_ge2", "reson_ge1", "vol_z_ge2",
    "vol_z_lt1", "ma20gt_ma50", "below_ma20", "atr_2_5", "confirmed_true",
]

COND_CN = {
    "rsi6_50_70": "RSI6∈[50,70)", "rsi6_ge50": "RSI6>=50", "pos_lt07": "pos<0.7",
    "pos_lt05": "pos<0.5", "pos_lt04": "pos<0.4", "boll_mid": "boll 0.3-0.7",
    "feat_vr_ge15": "事件vr>=1.5", "feat_vr_ge25": "事件vr>=2.5", "feat_rw_ge2": "事件rw>=2",
    "reson_ge1": "共振>=1", "vol_z_ge2": "量Z>=2", "vol_z_lt1": "量Z<1",
    "ma20gt_ma50": "MA20>MA50", "below_ma20": "MA20下方", "atr_2_5": "ATR 2-5%",
    "confirmed_true": "事件已确认",
}


def stability(recs):
    if len(recs) < 8:
        return None
    recs = sorted(recs, key=lambda r: (r["stock"], r["event_idx"]))
    half = len(recs) // 2
    s1 = stats(recs[:half])
    s2 = stats(recs[half:])
    return s1, s2


print("==== 基线引用 ====")
base_stats(DATA, "全部5事件(基线)")
base_stats(NET, "仅Spring/Shakeout(排除ST)")

print("\n==== 单条件叠加 (在 排除ST 基线上), n>=8 ====")
results = []
for c in CAND_COND:
    sub = [r for r in NET if cond(c, r)]
    s = stats(sub)
    if s and s["n"] >= 8:
        results.append((c, s))

# 排序: 胜率降序, n的权重
def sort_key(item):
    c, s = item
    return (s["pf"] if s["cum"] > 0 else -s["pf"], s["wr"], s["n"])

for c, s in sorted(results, key=sort_key, reverse=True):
    st = stability([r for r in NET if cond(c, r)])
    sttxt = ""
    if st:
        sttxt = f" 前半wr={st[0]['wr']:.0f}%(n{st[0]['n']}) 后半wr={st[1]['wr']:.0f}%(n{st[1]['n']})"
    print(f"  +{COND_CN[c]:<14} n={s['n']:>3} 胜率={s['wr']:4.1f}% 均={s['avg']:+5.2f}% PF={s['pf']:5.2f} 累计={s['cum']:+7.1f}%{sttxt}")

print("\n==== 两条件组合 (排除ST + A + B), n>=6, 按PF*累计排 ====")
two_results = []
for i in range(len(CAND_COND)):
    for j in range(i + 1, len(CAND_COND)):
        a, b = CAND_COND[i], CAND_COND[j]
        sub = [r for r in NET if cond(a, r) and cond(b, r)]
        s = stats(sub)
        if s and s["n"] >= 6 and s["cum"] > 0:
            two_results.append((a, b, s))
for a, b, s in sorted(two_results, key=lambda t: (t[2]["pf"] * max(t[2]["cum"], 0), t[2]["wr"], t[2]["n"]), reverse=True):
    st = stability([r for r in NET if cond(a, r) and cond(b, r)])
    sttxt = ""
    if st:
        sttxt = f"  前后wr={st[0]['wr']:.0f}/{st[1]['wr']:.0f}%(n{st[0]['n']}/{st[1]['n']})"
    print(f"  {COND_CN[a]}+{COND_CN[b]:<12} n={s['n']:>3} 胜率={s['wr']:4.1f}% 均={s['avg']:+5.2f}% PF={s['pf']:5.2f} 累计={s['cum']:+7.1f}%{sttxt}")

print("\n==== 候选组合明细(含单笔) ====")
for key in [
    ("排除ST", "Spring/Shakeout"),
    ("排除ST+共振", "Spring/Shakeout + 共振>=1"),
    ("排除ST+RSI50-70", "Spring/Shakeout + RSI6∈[50,70)"),
    ("排除ST+boll中轨", "Spring/Shakeout + boll 0.3-0.7"),
    ("排除ST+低位", "Spring/Shakeout + pos<0.7"),
]:
    pass