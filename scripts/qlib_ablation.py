#!/usr/bin/env python3
"""Qlib 消融对比: 交易计划方向准确率 (QLib 模型概率 ON vs OFF)。

对自选股池最近 datalen 根日线, 每 step 根抽样一次 bar 作为分析日,
在同一时刻用两种模式生成 build_trade_plan 方向:
  ON : qlib_prob = (Alpha158 因子截至该 bar 的模型 prob_buy, auc 置信度)
  OFF: qlib_prob = 中性 {0.5, 0.5, 0.0} (等价于未接入 Qlib 时的默认)

评估: 分析日收盘 → 未来 5/10/20 根收益方向命中 (多头 ret>0 命中, 空头 ret<0 命中,
观望不计)。Alpha158 为滚动因子 (MA/ROC/STD 等只用截至当日数据), 且预计算整段
历史后按日期取行, 保证无未来信息泄漏。

用法:
  python scripts/qlib_ablation.py
  python scripts/qlib_ablation.py --code sh600519 --datalen 500 --step 5
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.datasource import fetch_kline
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.events import detect_all
from wyckoff.phases import judge_phase
from wyckoff.waves import calc_targets
from wyckoff.pnf import build_pnf, pnf_targets
from wyckoff.market import find_trading_range
from wyckoff.structure import structure_progress
from wyckoff.vsa import vsa_classify
from wyckoff.analysis import build_trade_plan, _plan_from_lines

_HORIZONS = (5, 10, 20)
NEUTRAL = {"prob_buy": 0.5, "prob_sell": 0.5, "confidence": 0.0}


def _load_model_and_features(code):
    """返回 (model_obj, features_df, proba阵列, names)。features 按交易日索引。"""
    from wyckoff import qlib_adapter
    if not qlib_adapter._is_qlib_available():
        return None, None, None, None
    model_obj = qlib_adapter._load_qlib_model()
    if model_obj is None:
        return None, None, None, None
    feat_df, names = qlib_adapter.fetch_alpha158_features(
        code, "2022-01-01", "2026-09-10")
    if feat_df is None or feat_df.empty:
        return None, None, None, None
    fnames = model_obj["feature_names"]
    if any(f.startswith("wy_") for f in fnames):
        dfeat, _ = qlib_adapter.compute_domain_features(
            code, "2022-01-01", "2026-09-10")
        if dfeat is not None and len(dfeat):
            feat_df = feat_df.join(dfeat, how="inner")
    prob_buy = qlib_adapter.qlib_probability_series(feat_df, model_obj)
    if prob_buy is None:
        return None, None, None, None
    return model_obj, feat_df, prob_buy, None


def _prob_at(feat_df, prob_buy, day, veto_lo=0.40, veto_hi=0.60):
    """取某交易日当天模型概率 (无前视)。"""
    if feat_df is None or day is None:
        return NEUTRAL
    day = pd.Timestamp(day).normalize()
    if day in feat_df.index:
        p = float(prob_buy[feat_df.index.get_loc(day)])
        p = min(max(p, 0.01), 0.99)
        return {"prob_buy": p, "prob_sell": 1.0 - p, "confidence": 0.55,
                "veto_lo": veto_lo, "veto_hi": veto_hi}
    return NEUTRAL


def _direction(plan_lines):
    p = _plan_from_lines(plan_lines or [])
    d = (p.get("direction") or "").strip()
    if "多头" in d:
        return 1
    if "空头" in d:
        return -1
    return 0


def _hits(df, j, mode, qp):
    try:
        piv = find_pivots(df.iloc[: j + 1])
        evs = detect_all(df.iloc[: j + 1], piv) or []
        phase, _ = judge_phase(df.iloc[: j + 1], piv, evs)
        targets = calc_targets(df.iloc[: j + 1], piv, evs)
        _, box = build_pnf(df.iloc[: j + 1])
        pnf_t = pnf_targets(df.iloc[: j + 1], _, box)
        tr = find_trading_range(df.iloc[: j + 1], piv)
        struct = structure_progress(evs, df.iloc[: j + 1], phase=phase)
        close = float(df["close"].iloc[j])
        plan = build_trade_plan(df.iloc[: j + 1], piv, evs, phase, struct,
                                targets, pnf_t, tr, close, qlib_prob=qp)
    except Exception:
        return None
    dirn = _direction(plan)
    if dirn == 0:
        return None
    out = {"mode": mode, "dir": dirn, "j": j}
    for h in _HORIZONS:
        if j + h < len(df) and close > 0:
            ret = float(df["close"].iloc[j + h] / close - 1)
            out[f"ret{h}"] = ret
    return out


def evaluate(code, datalen, step, veto_lo=0.40, veto_hi=0.60):
    df = add_indicators(fetch_kline(code, datalen=datalen, scale=240), symbol=code)
    if df is None or len(df) < 140:
        return None
    model_obj, feat_df, prob_buy, _ = _load_model_and_features(code)
    rows = []
    n = len(df)
    for j in range(int(len(df) * 0.5), n - max(_HORIZONS), step):
        day = df["day"].iloc[j]
        qp = _prob_at(feat_df, prob_buy, day, veto_lo, veto_hi) if model_obj is not None else NEUTRAL
        on = _hits(df, j, "ON", qp)
        off = _hits(df, j, "OFF", NEUTRAL)
        if on is not None:
            rows.append(on)
        if off is not None:
            rows.append(off)
    if not rows:
        return None
    rec = pd.DataFrame(rows)
    lines = [f"\n== {code} (bars {int(len(df)*0.5)}~{n-21}, step {step}) =="]
    for mode in ("ON", "OFF"):
        sub = rec[rec["mode"] == mode]
        parts = [f"{mode} n={len(sub)}"]
        for h in _HORIZONS:
            col = f"ret{h}"
            v = sub[sub[col].notna()][col]
            if len(v):
                hit = (v > 0).sum()
                parts.append(f"{h}根命中={hit}/{len(v)} ({hit / len(v):.1%})")
        lines.append("  " + "  ".join(parts))
    print("\n".join(lines))
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--code", default=None, help="单只股票代码 (默认自选股池)")
    ap.add_argument("--datalen", type=int, default=700)
    ap.add_argument("--step", type=int, default=8)
    ap.add_argument("--veto-lo", type=float, default=0.40, help="Qlib 否决阈值下限")
    ap.add_argument("--veto-hi", type=float, default=0.60, help="Qlib 否决阈值上限")
    args = ap.parse_args()

    if args.code:
        codes = [args.code]
    else:
        from wyckoff.qlib_update import load_watch_pool
        codes = load_watch_pool()
        codes = [c for c in codes if c.lower().startswith(("sh", "sz"))
                 and not c.lower().startswith("sh000") and not c.lower().startswith("sz399")]

    agg = {"ON": {h: [] for h in _HORIZONS}, "OFF": {h: [] for h in _HORIZONS}}
    for c in codes:
        rec = evaluate(c, args.datalen, args.step, args.veto_lo, args.veto_hi)
        if rec is None:
            continue
        for mode in ("ON", "OFF"):
            sub = rec[rec["mode"] == mode]
            for h in _HORIZONS:
                v = sub[sub[f"ret{h}"].notna()][f"ret{h}"]
                agg[mode][h] += list(v)

    print("\n=== 汇总 (方向命中率, 多头 ret>0 / 空头 ret<0) ===")
    print(f"{'模式':6s} {'5根':>10s} {'10根':>10s} {'20根':>10s}")
    for mode in ("ON", "OFF"):
        cells = []
        for h in _HORIZONS:
            v = agg[mode][h]
            if v:
                hit = sum(1 for x in v if x > 0)
                cells.append(f"{hit}/{len(v)}={hit/len(v):.1%}")
            else:
                cells.append("--")
        print(f"{mode:6s} {cells[0]:>10s} {cells[1]:>10s} {cells[2]:>10s}")


if __name__ == "__main__":
    main()