# -*- coding: utf-8 -*-
"""信号准确性验证层: 检验"置信度打分是否真的预判收益 / 信号是否真优于随机"。

在 signal_accuracy 追踪数据 (signal_accuracy.json) 之上做四类统计验证,
全部只读、不写盘:

1. Rank IC (置信度自检): 事件置信度 conf 与未来收益的 Spearman 秩相关 +
   置信度分档胜率曲线。IC≈0 说明打分系统不预判收益, 需要重新校准;
2. Bootstrap 置信区间: 每类信号胜率的 95% CI, 小样本 (n<20) 自动标注不足,
   避免把 n=9 的 33% 当可靠结论;
3. 随机入场显著性 (置换检验): 每类信号收益 vs 全池随机入场收益的零分布,
   算 p 值 —— 信号优于随机才算"有信息量";
4. 样本外胜率 win_rate_of_oos: 只用 before_ts 之前已出现的信号记录计算,
   消除"用未来数据校准历史信号权重"的前瞻偏差。

依赖: numpy / pandas (项目已有)。不依赖 scipy (秩相关自行实现)。
"""
import time

import numpy as np
import pandas as pd

from .signal_accuracy import load_signals, _sig_date
from .config import event_dir, vsa_dir

# 置信度分档边界 (与 accuracy_center 展示口径一致)
CONF_BANDS = (("≥80", 80), ("60-79", 60), ("40-59", 40), ("<40", 0))


def _rankdata(a):
    """平均秩 (处理并列), 替代 scipy.stats.rankdata。"""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return ranks


def _spearman(x, y):
    """Spearman 秩相关系数 (无 scipy 依赖)。"""
    rx = _rankdata(x)
    ry = _rankdata(y)
    xm = rx - rx.mean()
    ym = ry - ry.mean()
    denom = np.sqrt((xm ** 2).sum() * (ym ** 2).sum())
    if denom <= 1e-12:
        return 0.0
    return float((xm * ym).sum() / denom)


def _hit(kind, type_, v):
    """方向化命中: 标称多头/中性 ret>0 命中; 标称空头 ret<0 命中 (跌才对)。"""
    if kind == "event":
        d = event_dir(type_)
    else:
        d = vsa_dir(type_)
    return bool(v < 0) if d < 0 else bool(v >= 0)


# ───────────────────────── 1. Rank IC 置信度自检 ─────────────────────────

def _conf_ret_pairs(records, kind="event", horizon=20):
    """取 (conf, ret, hit) 对: 仅事件信号有置信度, VSA 统一 0 无区分度。
    hit 为方向化命中 (空头信号下跌才对), 分档胜率用它而非原始上涨占比。"""
    pairs = []
    for r in records:
        if r.get("kind") != kind:
            continue
        conf = r.get("conf")
        if conf is None:
            continue
        res = (r.get("results") or {}).get(str(horizon))
        if not res or res.get("ret") is None:
            continue
        ret = float(res["ret"])
        pairs.append((float(conf), ret, _hit(kind, r.get("type", ""), ret)))
    return pairs


def rank_ic(records, kind="event", horizon=20, min_n=30):
    """置信度 conf 与未来收益的 Spearman 秩相关 + 分档胜率曲线。

    返回 dict (样本 < min_n 时 spearman/insufficient 标记, 分档仍给出):
      {"horizon", "n", "spearman", "insufficient",
       "by_band": {band: {"n", "win", "mean"}}}
    """
    pairs = _conf_ret_pairs(records, kind=kind, horizon=horizon)
    out = {"horizon": horizon, "n": len(pairs), "spearman": None,
           "insufficient": len(pairs) < min_n, "by_band": {}}
    if not pairs:
        return out
    cf = np.array([p[0] for p in pairs])
    rt = np.array([p[1] for p in pairs])
    ht = np.array([1.0 if p[2] else 0.0 for p in pairs])
    if len(pairs) >= 2:
        out["spearman"] = _spearman(cf, rt)
    for band, lo in CONF_BANDS:
        sel = cf >= lo
        if band == "<40":
            sel = cf < 40
        if not sel.any():
            continue
        h_band = ht[sel]
        r_band = rt[sel]
        out["by_band"][band] = {
            "n": int(sel.sum()),
            "win": float(h_band.mean() * 100),
            "mean": float(r_band.mean() * 100),
        }
    return out


# ───────────────────────── 2. Bootstrap 置信区间 ─────────────────────────

def bootstrap_winrate_ci(rets, n_boot=1000, seed=42, alpha=0.05, direction=0):
    """对收益序列重采样估计(方向化)胜率 CI。

    direction<0 (标称空头) → 以 ret<0 为命中; 其余 ret>0 为命中。
    返回 {"n", "win", "ci_lo", "ci_hi"} 或 None (样本 < 3)。
    """
    arr = np.asarray(rets, dtype=float)
    if arr.size < 3:
        return None
    def _is_hit(v):
        return bool(v < 0) if direction < 0 else bool(v > 0)
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for k in range(n_boot):
        s = arr[rng.integers(0, arr.size, arr.size)]
        boot[k] = float(np.mean([_is_hit(v) for v in s]))
    lo, hi = np.percentile(boot, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"n": int(arr.size), "win": float(np.mean([_is_hit(v) for v in arr]) * 100),
            "ci_lo": float(lo * 100), "ci_hi": float(hi * 100)}


def winrate_ci_table(records, kind="event", horizon=20, min_n=3, n_boot=1000):
    """每类信号的胜率 95% CI 表。

    返回 {"horizon", "types": {type: {"n", "win", "ci_lo", "ci_hi",
    "insufficient": n<20}}}。样本 < min_n 的类型不入表。
    """
    out = {"horizon": horizon, "types": {}}
    by_type = {}
    for r in records:
        if r.get("kind") != kind:
            continue
        res = (r.get("results") or {}).get(str(horizon))
        if not res or res.get("ret") is None:
            continue
        by_type.setdefault(r.get("type", "?"), []).append(float(res["ret"]))
    for t, rets in by_type.items():
        if len(rets) < min_n:
            continue
        ci = bootstrap_winrate_ci(rets, n_boot=n_boot,
                                  direction=event_dir(t) if kind == "event" else vsa_dir(t))
        if not ci:
            continue
        ci["insufficient"] = len(rets) < 20
        out["types"][t] = ci
    return out


# ─────────────────────── 3. 随机入场显著性 (置换检验) ────────────────────

def significance_table(records, kind="event", horizon=20, min_n=8,
                       n_perm=2000, seed=7):
    """每类信号 vs 全池随机入场 的置换检验。

    零假设: 该类型信号的收益均值 不优于 从全池随机抽同等数量的均值。
    返回:
      {"horizon", "kind", "pool": {"n", "win", "mean"},
       "types": {type: {"n", "win", "mean", "p", "sig_5"}}}
    样本 < min_n 的类型不入表; 全池 < 20 条返回 None (无法检验)。
    """
    pool = []
    by_type = {}
    for r in records:
        if r.get("kind") != kind:
            continue
        res = (r.get("results") or {}).get(str(horizon))
        if not res or res.get("ret") is None:
            continue
        v = float(res["ret"])
        pool.append(v)
        by_type.setdefault(r.get("type", "?"), []).append(v)
    pool = np.asarray(pool, dtype=float)
    if pool.size < 20:
        return None
    rng = np.random.default_rng(seed)
    out = {"horizon": horizon, "kind": kind,
           "pool": {"n": int(pool.size), "win": float((pool > 0).mean() * 100),
                    "mean": float(pool.mean() * 100)},
           "types": {}}
    for t, rets in by_type.items():
        if len(rets) < min_n:
            continue
        arr = np.asarray(rets, dtype=float)
        n = arr.size
        obs = float(arr.mean())
        sims = np.empty(n_perm)
        for k in range(n_perm):
            s = pool[rng.integers(0, pool.size, n)]
            sims[k] = float(s.mean())
        p = float((sims >= obs).mean())
        out["types"][t] = {
            "n": int(n), "win": float(np.mean(
                [_hit(kind, t, v) for v in arr]) * 100),
            "mean": obs * 100, "p": round(p, 4), "sig_5": bool(p < 0.05),
        }
    return out


# ───────────────────────── 4. 样本外胜率 (OOS) ──────────────────────────

def win_rate_of_oos(records, kind, type_, before_ts, horizon=20,
                    baseline=0.5, min_n=10):
    """样本外胜率 (方向化): 只用 before_ts 之前已出现的该类型信号记录。

    before_ts: datetime/Timestamp 阈值 (信号日期严格早于它才计入)。
    消除前瞻: 评价 t 时刻的信号时, 只用 t 之前已能看到的样本。
    方向化: 该类型为标称空头 (event_dir/vsa_dir<0) → 以 ret<0 为命中。
    样本 < min_n → 回退 baseline (与 win_rate_of 同约定)。
    """
    before = pd.Timestamp(before_ts)
    rets = []
    for r in records:
        if r.get("kind") != kind or r.get("type") != type_:
            continue
        d = _sig_date(r)
        if d is None:
            continue
        try:
            if pd.Timestamp(d) >= before:
                continue
        except Exception:
            continue
        res = (r.get("results") or {}).get(str(horizon))
        if not res or res.get("ret") is None:
            continue
        rets.append(float(res["ret"]))
    if len(rets) < min_n:
        return baseline
    return sum(1 for v in rets if _hit(kind, type_, v)) / len(rets)


def oos_record_loader():
    """惰性 + 短缓存 的 records 加载 (融合层高频调用时避免重复读盘)。"""
    _c = {"ts": 0.0, "records": None}
    _TTL = 60.0

    def load():
        now = time.time()
        if _c["records"] is not None and now - _c["ts"] < _TTL:
            return _c["records"]
        recs = load_signals()
        _c["records"] = recs
        _c["ts"] = now
        return recs

    return load


# ───────────────────────── 汇总格式化 (展示用) ──────────────────────────

def validation_lines(records, horizon=20):
    """把四类验证结果格式化为文本行 (供准确度中心/CLI/周报)。"""
    lines = []
    # 1. Rank IC
    ic = rank_ic(records, kind="event", horizon=horizon)
    if ic["n"]:
        if ic["insufficient"] or ic["spearman"] is None:
            lines.append(f"置信度IC: 样本 {ic['n']} 不足 (需≥30), 暂不评估打分有效性")
        else:
            grade = ("有效" if abs(ic["spearman"]) >= 0.15 else
                     "较弱" if abs(ic["spearman"]) >= 0.05 else "无效")
            lines.append(f"置信度IC: Spearman={ic['spearman']:+.3f} (n={ic['n']}, {grade})")
        bands = "  ".join(f"{b}: {s['win']:.0f}%({s['n']})"
                          for b, s in ic["by_band"].items())
        if bands:
            lines.append(f"  按置信度分档胜率: {bands}")
        if ic["by_band"]:
            hi = ic["by_band"].get("≥80")
            lo = ic["by_band"].get("<40")
            if hi and lo and hi["n"] >= 10 and lo["n"] >= 10:
                delta = hi["win"] - lo["win"]
                lines.append(f"  高置信(≥80) vs 低置信(<40) 胜率差 {delta:+.1f}% "
                             f"→ {'打分有区分度' if delta > 0 else '打分反了/无区分'}")
    # 2. Bootstrap CI
    ci = winrate_ci_table(records, kind="event", horizon=horizon)
    if ci["types"]:
        rows = []
        for t, s in ci["types"].items():
            tag = "样本不足" if s["insufficient"] else f"{s['ci_lo']:.0f}-{s['ci_hi']:.0f}%"
            rows.append(f"{t}: {s['win']:.0f}%[{tag}]")
        lines.append(f"事件胜率CI(95%): " + "  ".join(sorted(rows)))
    # 3. 随机入场显著性
    sig = significance_table(records, kind="event", horizon=horizon)
    if sig:
        lines.append(f"随机入场基准: 全池{sig['pool']['n']}条 均值{sig['pool']['mean']:+.1f}% "
                     f"胜率{sig['pool']['win']:.0f}%")
        marks = []
        for t, s in sorted(sig["types"].items(), key=lambda kv: -kv[1]["mean"]):
            mark = "✓优于随机" if s["sig_5"] else "≈随机"
            marks.append(f"{t}: {s['mean']:+.1f}% (p={s['p']:.2f}, {mark})")
        if marks:
            lines.append("  类型均值 vs 随机: " + "  ".join(marks))
    return lines


# ───────────────────────── 5. 规则化 AI 解读 ─────────────────────────

def validation_verdict(records, horizon=20, min_conf=80):
    """把四类验证结果"翻译"成通俗结论 (规则化 AI 解读, 无需外部大模型)。

    返回 str (多行): 面向普通投资者的准确性结论 + 可依赖的信号类型清单 +
    需要怀疑/样本不足的类型。无记录时返回提示文本。
    """
    ic = rank_ic(records, kind="event", horizon=horizon)
    ci = winrate_ci_table(records, kind="event", horizon=horizon)
    sig = significance_table(records, kind="event", horizon=horizon)
    if ic["n"] == 0:
        return "暂无信号记录, 无法评估信号准确性。完成几次分析后自动积累样本。"
    parts = []
    # 1. 打分体系是否有区分度
    if ic["insufficient"] or ic["spearman"] is None:
        parts.append(f"样本 {ic['n']} 个, 置信度打分还无法判定有效性 (建议至少 30 个)。")
    elif abs(ic["spearman"]) >= 0.15:
        parts.append(f"置信度打分有区分度 (IC={ic['spearman']:+.2f}): "
                     f"置信度越高的信号, 实际收益整体越高, 打分体系可以信赖。")
    elif abs(ic["spearman"]) >= 0.05:
        parts.append(f"置信度打分区分度较弱 (IC={ic['spearman']:+.2f}): "
                     f"高低置信信号差异不明显, 打分仅供参考。")
    else:
        parts.append(f"置信度打分与收益几乎无关 (IC={ic['spearman']:+.2f}): "
                     f"当前的置信度数字不预判收益, 建议重新校准打分规则。")
    # 2. 分档胜率是否有方向一致性
    hi = ic["by_band"].get("≥80")
    lo = ic["by_band"].get("<40")
    if hi and lo and hi["n"] >= 10 and lo["n"] >= 10:
        delta = hi["win"] - lo["win"]
        if delta > 0:
            parts.append(f"高置信(≥{min_conf})信号胜率 {hi['win']:.0f}%, "
                         f"显著高于低置信 {lo['win']:.0f}% (差 {delta:+.0f}%)。")
        elif delta < 0:
            parts.append(f"警告: 高置信信号胜率 {hi['win']:.0f}% 反而低于 "
                         f"低置信 {lo['win']:.0f}%, 打分方向反了, 谨慎使用。")
    # 3. 优于随机的类型 (置换检验)
    good, weak, thin = [], [], []
    if sig:
        for t, s in sig["types"].items():
            if s["sig_5"] and s["mean"] > 0:
                good.append(t)
            elif not s["sig_5"]:
                weak.append(t)
        for t, s in ci["types"].items():
            if s["insufficient"]:
                thin.append(t)
    if good:
        parts.append("可依赖的信号类型: " + "、".join(good) +
                     " (统计显著优于随机入场)。")
    if weak:
        parts.append("暂未证明优于随机的类型: " + "、".join(weak) +
                     ", 按此类信号交易时谨慎。")
    if thin:
        parts.append("样本不足 (<20) 需继续积累的类型: " + "、".join(thin) + "。")
    return "\n".join(parts)


def validation_ai_interpret(records, settings=None, horizon=20):
    """把验证统计摘要交给大模型生成通俗解读 (可选层)。

    未配置 API Key / 未启用 / 失败 → 回退规则化 validation_verdict (离线可用)。
    返回 {"ai": str|None, "rule": str}。
    """
    rule = validation_verdict(records, horizon=horizon)
    if not records or not settings:
        return {"ai": None, "rule": rule}
    try:
        from .interpret import interpret_prompt
        stats = "\n".join(validation_lines(records, horizon=horizon))
        prompt = _ACCURACY_AI_PROMPT.format(stats=stats, rule=rule)
        ai = interpret_prompt(prompt, settings, min_len=100,
                              max_tokens=1200, temperature=0.3)
        return {"ai": ai, "rule": rule}
    except Exception:
        return {"ai": None, "rule": rule}


_ACCURACY_AI_PROMPT = """你是一名严谨的量化研究员, 专门复核信号分析工具的自检报告。
请基于下面这份"信号准确性验证"统计摘要, 给普通投资者写一段通俗解读, 指出:
1) 这个工具的置信度打分体系可不可信、有多大参考价值;
2) 哪些信号类型值得跟随, 哪些不值得/样本不足;
3) 当前数据积累到多少样本才足以得出可靠结论。
要求: 口语化、有具体数字依据、客观中立 (不夸大), 300 字以内, 直接输出正文,
不要标题/序号/markdown。若统计说样本不足或打分无效, 要直说"现在还不能信赖", 不要粉饰。

# 统计摘要
{stats}

# 规则化结论 (可参考)
{rule}

# 市场约束
该工具面向 A股单边做多市场 (只能做多、不能做空)。凡涉及可执行动作的表述,
偏空信号一律落在 减仓/离场/回避/不追高 等动作上, 严禁给出 做空/放空/开空仓/
空头回补 等做空指令。
"""
