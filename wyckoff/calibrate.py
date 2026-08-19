# -*- coding: utf-8 -*-
"""准确度校准: 从 accuracy 评估导出中诊断失效规则并输出调整建议。

自动化闭环的最后一步 (评估 → 汇总 → 诊断 → 建议):
  1. 读取 wx_accuracy_export.json (accuracy.py --export 生成) 的 stats.confusion /
     horizons 分层命中率;
  2. 对每个 阶段×tone 与实际方向 做一致性诊断: 若某阶段预设 tone 与未来 N 根
     实际方向长期相悖 (胜率显著低于 50%), 判定该规则失效;
  3. 输出 {verdict, diagnosis, action} 建议: 下调/上调 tone、修正判定、增删信号权重,
     同时给出对应的代码/配置定位 (供人工或 AI 落库)。

用法:
  python -m wyckoff.calibrate            # 诊断 ~/.wyckoff/wx_accuracy_export.json
  python -m wyckoff.calibrate --min-n 10 --alpha 0.60
      # min-n: 最小样本数才下结论; alpha: 命中率失效阈值 (低于则判失效)
"""
import json
import os
import sys

from .paths import DATA_DIR, FEEDBACK_FILE

DEFAULT_EXPORT = os.path.join(DATA_DIR, "wx_accuracy_export.json")

# 规则 → 对应实现位置 (供建议定位)
_RULE_LOC = {
    "底部整固": "wyckoff/phases.py judge_phase (低位收敛筑底)",
    "上升趋势": "wyckoff/phases.py judge_phase (高低点同步上移)",
    "区间整理": "wyckoff/phases.py judge_phase (方向不一致)",
    "顶部构筑": "wyckoff/phases.py judge_phase (高位派发)",
    "下跌趋势": "wyckoff/phases.py judge_phase (高低点同步下移)",
    "trade_bull": "wyckoff/analysis.py build_trade_plan (多头方向)",
    "trade_bear": "wyckoff/analysis.py build_trade_plan (空头方向)",
    "pnf_up": "wyckoff/pnf.py pnf_targets (上方目标计数)",
    "pnf_down": "wyckoff/pnf.py pnf_targets (下方目标计数)",
    "up_target": "wyckoff/pnf.py / waves.py calc_targets (上方目标价)",
    "down_target": "wyckoff/pnf.py / waves.py calc_targets (下方目标价)",
    "Spring": "wyckoff/backtest.py _BUY_PTS (弹簧买点权重)",
    "ST": "wyckoff/backtest.py _BUY_PTS (二次测试买点权重)",
    "SC": "wyckoff/backtest.py _BUY_PTS (卖出高潮买点权重)",
    "UTAD": "wyckoff/backtest.py _SELL_PTS (上冲派发卖点权重)",
    "BC": "wyckoff/backtest.py _SELL_PTS (买入高潮卖点权重)",
}


def _win(pred_tone, ret, bench):
    """按预测方向判定单条命中 (与 accuracy.accuracy_stats 同口径)。"""
    if pred_tone == "bullish":
        return ret > 0, ret > bench if bench is not None else None
    if pred_tone == "bearish":
        return ret < 0, ret < bench if bench is not None else None
    return None, None


# 阶段带 label → 方向期望 (供人工标注的诊断)
_FEEDBACK_TONE = {
    "accumulation": "bullish", "markup": "bullish",
    "distribution": "bearish", "markdown": "bearish",
}


def diagnose_feedback(feedback, min_n=2):
    """从 wx_feedback.json (用户对阶段带标 正确/错误) 汇总误判模式。

    反馈记录含 features (吸筹: lo1/lo2/low_defense; 派发: hi1/hi2/high_cap),
    用于定位最可能失效的阈值: 吸筹误判看 low_defense, 派发误判看 high_cap。
    返回 {verdict, issues:[...]}。
    """
    issues = []
    acc_lo, dis_hi = [], []
    n_wrong = n_correct = 0
    for r in feedback:
        v = r.get("verdict")
        if v == "wrong":
            n_wrong += 1
        elif v == "correct":
            n_correct += 1
        if v != "wrong":
            continue
        label = r.get("label")
        feat = r.get("features") or {}
        if label == "accumulation" and "low_defense" in feat:
            acc_lo.append(feat["low_defense"])
        elif label == "distribution" and "high_cap" in feat:
            dis_hi.append(feat["high_cap"])
    if n_wrong:
        issues.append({
            "kind": "feedback_meta",
            "n_wrong": n_wrong, "n_correct": n_correct,
            "msg": f"人工标注 {n_wrong} 条错误 / {n_correct} 条正确",
        })
    if acc_lo:
        mean = sum(acc_lo) / len(acc_lo)
        issues.append({
            "kind": "feedback_accumulation",
            "n": len(acc_lo), "low_defense_mean": round(mean, 4),
            "msg": f"吸筹带被标错误 {len(acc_lo)} 条, low_defense 均值 {mean:.4f} "
                   f"(<1 表示后段低点跌破前段低点) — 若普遍 <0.97 说明低点防守阈值偏松",
            "loc": "wyckoff/phases.py _validate_phase (低点防守 0.97)",
        })
    if dis_hi:
        mean = sum(dis_hi) / len(dis_hi)
        issues.append({
            "kind": "feedback_distribution",
            "n": len(dis_hi), "high_cap_mean": round(mean, 4),
            "msg": f"派发带被标错误 {len(dis_hi)} 条, high_cap 均值 {mean:.4f} "
                   f"(<1 表示后段高点低于前段高点) — 若普遍接近 1 说明高点封顶判定偏松",
            "loc": "wyckoff/phases.py _validate_phase (高点封顶 1.03)",
        })
    verdict = "需要校准" if n_wrong else "无错误标注"
    return {"verdict": verdict, "issues": issues}


def diagnose(records, min_n=10, alpha=0.60):
    """从 accuracy 记录诊断失效规则。返回 {verdict, issues:[...]}。"""
    issues = []
    # ── 阶段×tone 一致性 (混淆矩阵) ──
    by_phase = {}
    for r in records:
        res = (r.get("results") or {})
        for h, out in res.items():
            if not out or out.get("ret") is None:
                continue
            base = r.get("phase", "")
            tone = r.get("phase_tone", "")
            key = (base, tone)
            s = by_phase.setdefault(key, {"n": 0, "hit": 0, "ex": [], "means": []})
            win, ex = _win(tone, out["ret"], out.get("bench"))
            if win is not None:
                s["n"] += 1
                s["hit"] += int(win)
                if ex is not None:
                    s["ex"].append(ex)
            s["means"].append(out["ret"])
    for (base, tone), s in by_phase.items():
        if s["n"] < min_n:
            continue
        wr = s["hit"] / s["n"]
        ex_mean = sum(s["ex"]) / len(s["ex"]) if s["ex"] else None
        mean = sum(s["means"]) / len(s["means"])
        expect_dir = "涨" if tone == "bullish" else "跌" if tone == "bearish" else "—"
        if tone in ("bullish", "bearish") and wr < alpha:
            issues.append({
                "kind": "phase_mislabel",
                "phase": base, "tone": tone, "n": s["n"],
                "win_rate": round(wr, 3),
                "mean": round(mean, 5), "ex_mean": ex_mean,
                "expect": expect_dir,
                "msg": (f"阶段[{base}]标{tone}(预期{expect_dir})但未来命中率仅{wr:.0%} "
                        f"(n={s['n']}, 均值{mean:+.2%}, 超额{ex_mean:+.2%}%) — "
                        f"判定可能系统性{('偏多' if tone=='bullish' else '偏空')}"),
                "loc": _RULE_LOC.get(base, "judge_phase"),
                "action": f"核查 {_RULE_LOC.get(base, 'judge_phase')} 的{base}判定条件; "
                          "若持续反向考虑降级该阶段 tone 或收紧触发条件",
            })
        elif s["n"] >= min_n:
            issues.append({
                "kind": "phase_ok",
                "phase": base, "tone": tone, "n": s["n"],
                "win_rate": round(wr, 3), "mean": round(mean, 5),
                "ex_mean": ex_mean,
                "msg": f"阶段[{base}]标{tone}命中率{wr:.0%} (n={s['n']}) — 正常",
            })

    # ── 交易计划方向 (多头/空头) ──
    for tkey, label in (("trade_bull", "多头交易计划"), ("trade_bear", "空头交易计划")):
        n = hit = 0
        exs = []
        for r in records:
            res = (r.get("results") or {})
            for h, out in res.items():
                if not out or out.get("ret") is None:
                    continue
                t = r.get("trade_tone")
                if t != ("bullish" if "bull" in tkey else "bearish"):
                    continue
                n += 1
                ret = out["ret"]
                if ("bull" in tkey and ret > 0) or ("bear" in tkey and ret < 0):
                    hit += 1
                if out.get("bench") is not None:
                    exs.append(ret - out["bench"])
        if n < min_n:
            continue
        wr = hit / n
        ex_mean = sum(exs) / len(exs) if exs else None
        issues.append({
            "kind": "trade_mislabel" if wr < alpha else "trade_ok",
            "key": tkey, "n": n, "win_rate": round(wr, 3),
            "ex_mean": ex_mean,
            "msg": f"{label}命中率{wr:.0%} (n={n}, 超额{ex_mean:+.2%}%)"
                   + (" — 正常" if wr >= alpha else " — 建议复核 build_trade_plan 方向/止损"),
            "loc": _RULE_LOC.get(tkey),
            "action": None if wr >= alpha else "复核 build_trade_plan 方向判定与止损/目标设置",
        })

    # ── P&F 方向 ──
    for dkey, label, expect in (("pnf_up", "P&F 上涨", ">0"), ("pnf_down", "P&F 下跌", "<0")):
        n = hit = 0
        exs = []
        for r in records:
            res = (r.get("results") or {})
            for h, out in res.items():
                if not out or out.get("ret") is None:
                    continue
                if r.get("pnf_dir") != ("up" if "up" in dkey else "down"):
                    continue
                n += 1
                ret = out["ret"]
                if ("up" in dkey and ret > 0) or ("down" in dkey and ret < 0):
                    hit += 1
                if out.get("bench") is not None:
                    exs.append(ret - out["bench"])
        if n < min_n:
            continue
        wr = hit / n
        ex_mean = sum(exs) / len(exs) if exs else None
        issues.append({
            "kind": "pnf_mislabel" if wr < alpha else "pnf_ok",
            "key": dkey, "n": n, "win_rate": round(wr, 3), "ex_mean": ex_mean,
            "msg": f"{label}方向命中率{wr:.0%} (n={n}, 期望{expect}, 超额{ex_mean:+.2f}%)"
                   + (" — 正常" if wr >= alpha else " — 建议复核 pnf 目标计数方向"),
            "loc": _RULE_LOC.get(dkey),
            "action": None if wr >= alpha else "复核 pnf_targets 方向判定/计数规则",
        })

    # ── 目标价命中 ──
    for tkey, label in (("up_target", "上方目标价"), ("down_target", "下方目标价")):
        n = hit = 0
        for r in records:
            res = (r.get("results") or {})
            for h, out in res.items():
                if not out or out.get("up_hit") is None:
                    continue
                if tkey == "up_target":
                    if r.get("up_target"):
                        n += 1
                        hit += int(out["up_hit"])
                else:
                    if r.get("down_target"):
                        n += 1
                        hit += int(out["down_hit"])
        if n < min_n:
            continue
        hr = hit / n
        issues.append({
            "kind": "target_miss" if hr < 0.5 else "target_ok",
            "key": tkey, "n": n, "hit_rate": round(hr, 3),
            "msg": f"{label}命中率{hr:.0%} (n={n})"
                   + (" — 正常" if hr >= 0.5 else " — 目标偏乐观/偏保守, 建议校准 pnf 箱体尺寸或目标系数"),
            "loc": _RULE_LOC.get(tkey),
            "action": None if hr >= 0.5 else "校准 pnf box 尺寸或 calc_targets 目标系数",
        })

    probs = [i for i in issues if i["kind"] in
             ("phase_mislabel", "trade_mislabel", "pnf_mislabel", "target_miss")]
    verdict = "需要校准" if probs else "规则正常"
    return {"verdict": verdict, "issues": issues,
            "warnings": probs}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = DEFAULT_EXPORT
    min_n, alpha = 10, 0.60
    for a in argv:
        if a.startswith("--min-n="):
            min_n = max(3, int(a.split("=", 1)[1]))
        elif a.startswith("--alpha="):
            alpha = float(a.split("=", 1)[1])
        elif not a.startswith("--"):
            path = a
    if not os.path.exists(path):
        print(f"未找到导出文件 {path}")
        print("请先运行: python -m wyckoff.accuracy --export")
        return 1
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records") or []
    if not records:
        print("导出文件中无记录 (尚无评估数据)")
        return 1
    result = diagnose(records, min_n=min_n, alpha=alpha)
    print("=" * 70)
    print(f"校准诊断 (样本≥{min_n}, 失效阈值 命中率<{alpha:.0%})")
    print(f"共 {len(records)} 条记录 · 结论: {result['verdict']}")
    print("=" * 70)
    for i in result["issues"]:
        mark = "!" if i["kind"] in ("phase_mislabel", "trade_mislabel",
                                    "pnf_mislabel", "target_miss") else " "
        print(f"[{mark}] {i['msg']}")
        if i.get("action"):
            print(f"      → 建议: {i['action']}")
        if i.get("loc"):
            print(f"      定位: {i['loc']}")
    # ── 人工阶段带反馈 (wx_feedback.json) 诊断, 不依赖行情评估也可用 ──
    fb = []
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                fbdata = json.load(f)
            fb = fbdata if isinstance(fbdata, list) else []
        except Exception:
            fb = []
    if fb:
        fbr = diagnose_feedback(fb, min_n=min_n)
        print("\n" + "=" * 70)
        print(f"阶段带人工反馈 (wx_feedback.json, 共{len(fb)}条) · 结论: {fbr['verdict']}")
        print("=" * 70)
        for i in fbr["issues"]:
            print(f"[!] {i['msg']}")
            if i.get("loc"):
                print(f"      定位: {i['loc']}")
    if result["warnings"]:
        print("\n存在失效规则, 校准后再重新评估一轮以确认改善。")
        return 1
    print("\n全部规则命中率达标, 无需调整。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
