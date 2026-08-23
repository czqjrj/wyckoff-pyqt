"""PNF (点数图) 三档目标准确率评估 (可 import 库模块, UI / Cron / CLI 都走这里)。

暴露给外部的主要 API:
  - run_eval(print_stdout, export_json, n_seeds, real_symbols) -> dict
  - load_latest_report() -> dict
  - save_report(result) -> str   (等价于内部 _dump_report)
  - PNF_ACC_DIR / PNF_ACC_LATEST
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from wyckoff.datasource import fetch_kline
from wyckoff.indicators import add_indicators
from wyckoff.paths import DATA_DIR
from wyckoff.pnf import build_pnf, pnf_history_targets, pnf_volume

PNF_ACC_DIR = os.path.join(DATA_DIR, "pnf_accuracy")
PNF_ACC_LATEST = os.path.join(PNF_ACC_DIR, "pnf_latest.json")
os.makedirs(PNF_ACC_DIR, exist_ok=True)


TIERS = ("保守", "中", "激进")


# ══════════════════════════════════════════════════════════════════
#  合成行情生成 + 段收集
# ══════════════════════════════════════════════════════════════════
def _gen_trend_and_range(seed: int, n_bars: int = 1600) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = []
    p = 20.0 + rng.uniform(0, 10)
    segments = [
        ("u", 200, 0.08, 0.6),
        ("r", 220, 0.0,  0.35),
        ("d", 200, -0.09, 0.6),
        ("r", 220, 0.0,  0.3),
        ("u", 260, 0.06, 0.55),
        ("r", 200, 0.0,  0.32),
        ("d", 180, -0.07, 0.5),
        ("r", 120, 0.0,  0.28),
    ]
    for _tag, n, drift, vol in segments:
        for _ in range(n):
            shock = rng.normal(0, vol)
            p = max(1.0, p + drift + shock)
            close.append(p)
    while len(close) < n_bars:
        p = max(1.0, p + rng.normal(0, 0.35))
        close.append(p)
    close = np.array(close[:n_bars])
    spread = np.maximum(close * 0.006, 0.03)
    return pd.DataFrame({
        "day": pd.date_range("2022-01-01", periods=len(close)),
        "open": close - rng.uniform(0, 1, len(close)) * spread,
        "close": close,
        "high": close + rng.uniform(0.3, 1, len(close)) * spread,
        "low": close - rng.uniform(0.3, 1, len(close)) * spread,
        "volume": rng.uniform(2e5, 5e6, len(close)),
    })


def _collect_segments(df: pd.DataFrame) -> list:
    cols, box = build_pnf(df, box_mode="pct", atr_factor=0.5)
    vol = pnf_volume(df, cols, box)
    hist = pnf_history_targets(cols, box, max_items=20, min_gap=4)
    for h in hist:
        h["_box"] = box
    return hist


def _real_stock_segments(symbols: list, scale: int = 240, datalen: int = 400) -> list:
    from wyckoff.utils import normalize_symbol
    out = []
    for s in symbols:
        try:
            df_raw = fetch_kline(normalize_symbol(s), datalen=datalen, scale=scale)
            if df_raw is None or len(df_raw) < 60:
                continue
            df = add_indicators(df_raw)
            out.extend(_collect_segments(df))
        except Exception:
            continue
    return out


# ══════════════════════════════════════════════════════════════════
#  统计工具
# ══════════════════════════════════════════════════════════════════
def _row(counter, direction_tag, tier_tag):
    hit_k = f"{direction_tag}hit_{tier_tag}"
    tgt_k = f"{direction_tag}目标_{tier_tag}"
    prob_k = f"{direction_tag}概率_{tier_tag}"
    sp_k = f"{direction_tag}空间_{tier_tag}%"
    n_total = counter["n"]
    n_valid = sum(1 for h in counter["hist"] if h.get(tgt_k) is not None)
    n_hit = sum(1 for h in counter["hist"] if h.get(hit_k))
    hit_rate = n_hit / n_valid * 100 if n_valid else 0.0
    probs = [h[prob_k] for h in counter["hist"] if isinstance(h.get(prob_k), (int, float))]
    avg_prob = float(np.mean(probs)) * 100 if probs else None
    sps = [h[sp_k] for h in counter["hist"] if isinstance(h.get(sp_k), (int, float))]
    avg_sp = float(np.mean(sps)) if sps else None
    calib = (avg_prob - hit_rate) if avg_prob is not None else None
    return {
        "n_total": n_total, "n_valid": n_valid, "n_hit": n_hit,
        "hit_rate%": round(hit_rate, 1),
        "avg_prob%": round(avg_prob, 1) if avg_prob is not None else None,
        "calib_pp": round(calib, 1) if calib is not None else None,
        "avg_space%": round(avg_sp, 1) if avg_sp is not None else None,
    }


# ══════════════════════════════════════════════════════════════════
#  主评估函数 (UI / Cron / CLI 共同入口)
# ══════════════════════════════════════════════════════════════════
def run_eval(print_stdout: bool = True, export_json: bool = True,
             n_seeds: int = 30, real_symbols: list = None) -> dict:
    result = {"ts": datetime.now().isoformat(timespec="seconds"),
              "total_segments": 0, "synthetic_n": 0, "real_n": 0,
              "near": {}, "tiers": [], "calibration": []}

    def _p(msg=""):
        if print_stdout:
            print(msg, flush=True)

    _p("=" * 78)
    _p("PNF 三档目标测算准确率评估")
    _p("=" * 78)
    all_hist = []
    _p(f"\n[1/3] 生成 {n_seeds} 组合成行情...")
    for seed in range(n_seeds):
        df = _gen_trend_and_range(seed=seed)
        segs = _collect_segments(df)
        all_hist.extend(segs)
    result["synthetic_n"] = len(all_hist)
    _p(f"  合成行情产出历史段: {len(all_hist)}")

    _p("\n[2/3] 尝试拉取真实股票数据...")
    if real_symbols is None:
        real_symbols = [
            "000001.SZ", "600519.SH", "000858.SZ", "601318.SH",
            "002594.SZ", "300750.SZ", "600036.SH", "601899.SH",
        ]
    real_segs = _real_stock_segments(real_symbols)
    result["real_n"] = len(real_segs)
    _p(f"  真实股票产出历史段: {len(real_segs)}")
    all_hist.extend(real_segs)
    result["total_segments"] = len(all_hist)

    if not all_hist:
        _p("  (无有效历史段, 退出)")
        if export_json:
            save_report(result)
        return result

    _p(f"\n[3/3] 统计分析 (总样本 {len(all_hist)} 段)...\n")

    ups_near = [h for h in all_hist if h.get("direction") == "up" and h.get("up_target") is not None]
    dns_near = [h for h in all_hist if h.get("direction") == "down" and h.get("down_target") is not None]
    up_near_ok = sum(1 for h in ups_near if h.get("up_hit"))
    dn_near_ok = sum(1 for h in dns_near if h.get("down_hit"))
    tot_near_n = len(ups_near) + len(dns_near)
    tot_near_ok = up_near_ok + dn_near_ok

    def _rate(a, b):
        return round(a / b * 100, 1) if b > 0 else 0.0
    result["near"] = {
        "up":   {"n": len(ups_near), "hit": up_near_ok, "rate%": _rate(up_near_ok, len(ups_near))},
        "dn":   {"n": len(dns_near), "hit": dn_near_ok, "rate%": _rate(dn_near_ok, len(dns_near))},
        "total":{"n": tot_near_n, "hit": tot_near_ok, "rate%": _rate(tot_near_ok, tot_near_n)},
    }
    _p("── 近端基准口径 (±4%封顶, 现有图标注口径) ──")
    _p(f"  上涨: {up_near_ok:>3}/{len(ups_near):>3}  {_rate(up_near_ok, len(ups_near)):.1f}%")
    _p(f"  下跌: {dn_near_ok:>3}/{len(dns_near):>3}  {_rate(dn_near_ok, len(dns_near)):.1f}%")
    _p(f"  综合: {tot_near_ok:>3}/{tot_near_n:>3}  {_rate(tot_near_ok, tot_near_n):.1f}%")
    _p()

    buckets = [
        ("全部", lambda h: True),
        ("方向=上", lambda h: h.get("direction") == "up"),
        ("方向=下", lambda h: h.get("direction") == "down"),
        ("区间=吸筹", lambda h: h.get("zone") == "吸筹"),
        ("区间=派发", lambda h: h.get("zone") == "派发"),
        ("吸筹+向上", lambda h: h.get("zone") == "吸筹" and h.get("direction") == "up"),
        ("派发+向下", lambda h: h.get("zone") == "派发" and h.get("direction") == "down"),
    ]

    header = f"{'分桶':<12}{'档位':<6}{'样本':>6}{'到达':>6}{'到达率':>8}{'均概率':>9}{'校准差':>8}{'均空间':>8}"
    sep = "-" * len(header)
    _p(header)
    _p(sep)

    for b_label, filt in buckets:
        hs = [h for h in all_hist if filt(h)]
        if not hs:
            continue
        counter = {"n": len(hs), "hist": hs}
        if b_label == "方向=上":
            dir_pairs = [("上方", "↑")]
        elif b_label == "方向=下":
            dir_pairs = [("下方", "↓")]
        else:
            dir_pairs = [("上方", "↑"), ("下方", "↓")]
        for dir_tag, dir_mark in dir_pairs:
            for tier in TIERS:
                r = _row(counter, dir_tag, tier)
                if r["n_valid"] == 0:
                    continue
                result["tiers"].append({
                    "bucket": b_label, "dir": dir_tag, "tier": tier,
                    "n_valid": r["n_valid"], "n_hit": r["n_hit"],
                    "hit_rate%": r["hit_rate%"], "avg_prob%": r["avg_prob%"],
                    "calib_pp": r["calib_pp"], "avg_space%": r["avg_space%"],
                })
                hr_str = f"{r['hit_rate%']:.1f}%"
                prob_str = f"{r['avg_prob%']:.1f}%" if r['avg_prob%'] is not None else "  --"
                calib_str = f"{r['calib_pp']:+.1f}pt" if r['calib_pp'] is not None else "    --"
                space_str = f"{r['avg_space%']:.1f}%" if r['avg_space%'] is not None else "   --"
                label = f"{b_label}{dir_mark}"
                _p(
                    f"{label:<12}{tier:<6}"
                    f"{r['n_valid']:>6}{r['n_hit']:>6}"
                    f"{hr_str:>8}"
                    f"{prob_str:>9}"
                    f"{calib_str:>8}"
                    f"{space_str:>8}"
                )
        _p()

    _p("── 概率校准表: 模型估算概率 vs 实际到达率 (仅突破同向目标) ──")
    calib_buckets = defaultdict(lambda: {"n": 0, "hit": 0, "prob_sum": 0.0})
    for h in all_hist:
        d = h.get("direction")
        if d not in ("up", "down"):
            continue
        dir_tag = "上方" if d == "up" else "下方"
        for tier in TIERS:
            p = h.get(f"{dir_tag}概率_{tier}")
            hit = h.get(f"{dir_tag}hit_{tier}")
            if not isinstance(p, (int, float)) or not isinstance(hit, bool):
                continue
            if p < 0.5:
                b = "<50%"
            elif p < 0.6:
                b = "50~60%"
            elif p < 0.7:
                b = "60~70%"
            elif p < 0.8:
                b = "70~80%"
            else:
                b = "≥80%"
            cb = calib_buckets[b + f"({tier})"]
            cb["n"] += 1
            cb["hit"] += int(hit)
            cb["prob_sum"] += float(p)
    _p(f"  {'预测概率档':<18}{'样本':>6}{'到达率':>9}{'均预测概率':>12}{'偏差':>8}")
    for k in sorted(calib_buckets.keys()):
        cb = calib_buckets[k]
        if cb["n"] == 0:
            continue
        avg_p = cb["prob_sum"] / cb["n"] * 100
        hr = cb["hit"] / cb["n"] * 100
        bias = avg_p - hr
        result["calibration"].append({
            "bucket": k, "n": cb["n"], "n_hit": cb["hit"],
            "hit_rate%": round(hr, 1), "avg_prob%": round(avg_p, 1),
            "bias_pp": round(bias, 1),
        })
        _p(
            f"  {k:<18}{cb['n']:>6}"
            f"{hr:>7.1f}%"
            f"{avg_p:>11.1f}%"
            f"{bias:>+7.1f}pt"
        )

    _p()
    _p("=" * 78)
    _p("评估完成。")
    _p("  校准差>0 = 模型高估 (说70%实际只到50%); <0 = 低估")
    _p("  建议关注: 保守档到达率应最高 (≥65%), 激进档最低 (30~50%), 概率偏差<±10pt")
    _p("=" * 78)

    if export_json:
        path = save_report(result)
        if print_stdout:
            print(f"[json] 报告已写入: {path}")
    return result


def save_report(result: dict) -> str:
    """写入 latest.json 并按日期归档 (等价于 _dump_report, 对外开放)。"""
    ts = result.get("ts") or datetime.now().isoformat(timespec="seconds")
    os.makedirs(PNF_ACC_DIR, exist_ok=True)
    with open(PNF_ACC_LATEST, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    day = ts[:10].replace("-", "")
    archive = os.path.join(PNF_ACC_DIR, f"pnf_acc_{day}.json")
    with open(archive, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return PNF_ACC_LATEST


def load_latest_report() -> dict:
    """UI / 校准中心读取最近一次评估结果。"""
    if not os.path.exists(PNF_ACC_LATEST):
        return {}
    try:
        with open(PNF_ACC_LATEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════
#  定时任务 (CLI 用)
# ══════════════════════════════════════════════════════════════════
def _sched_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --pnf-acc-eval'
    # 库模块路径: .../wyckoff/pnf_accuracy.py -> 项目根 = 其上两级
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(proj, "scripts", "eval_pnf_tier_accuracy.py")
    return f'cd "{proj}" && "{sys.executable}" "{script}" --run --quiet-json'


def install_cron(hour=None, minute=10):
    import subprocess
    try:
        cur = subprocess.check_output(["crontab", "-l"], stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError:
        cur = ""
    lines = [l for l in cur.splitlines()
             if "eval_pnf_tier_accuracy" not in l and "pnf-acc-eval" not in l]
    if hour is not None:
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        log = os.path.join(PNF_ACC_DIR, "cron.log")
        lines.append(
            f'TZ=Asia/Shanghai PATH="{os.path.dirname(sys.executable)}:$PATH" '
            f'{minute} {hour} * * * {_sched_command()} >> "{log}" 2>&1'
        )
    new = "\n".join(lines).strip() + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    return hour is not None


def install_task(hour="02:10", remove=False):
    import subprocess
    if os.name != "nt":
        print("install_task 仅支持 Windows; Linux 请用 --install-cron")
        return
    if remove:
        subprocess.run(["schtasks", "/Delete", "/TN", "WyckoffPnfAccuracy", "/F"])
        return
    bat = os.path.join(PNF_ACC_DIR, "pnf_accuracy_daily.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f"@echo off\n{_sched_command()}\n")
    subprocess.run(["schtasks", "/Create", "/TN", "WyckoffPnfAccuracy",
                    "/SC", "DAILY", "/ST", hour, "/TR", bat, "/F"], check=True)
