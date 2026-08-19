# -*- coding: utf-8 -*-
"""威科夫九大买卖检验 (Nine Tests)。

借鉴 WyckoffPro 的 NineBuyingTests/NineSellingTests, 以九条规则检验吸筹
(买入) / 派发 (卖出) 阶段的成熟度。全部通过时对应操作的最佳时机成熟;
   部分通过时给出"还缺什么"。纯规则, 数据全部取自本项目已有的 df/events/tr/pnf。
"""

from ._shared import recent_events as _recent, vol_wave as _vol_wave


def _support_level(df, pivots, tr):
    """支撑位: 优先 TR 下轨, 否则近 60 日枢轴低点 (低于现价最近的)。"""
    if tr and tr.get("bottom"):
        return float(tr["bottom"])
    last = float(df["close"].iloc[-1])
    lows = [p["price"] for p in pivots if p["type"] == "low"
            and p["idx"] >= len(df) - 60 and p["price"] < last]
    return max(lows) if lows else None


def _decline_objective(df, events, pivots, tr):
    """已完成下跌的目标低点: 优先最近 SC 低点, 否则 TR 下轨, 否则近 60 日枢轴低点。

    威科夫 T1"下跌目标已达到"问的是已走完的下跌是否到位 (吸筹成熟度), 而非
    当前区间向下的破位投影 — 破位投影只会在下跌继续时被触及, 用它当目标会让
    区间内个股的 T1 恒为失败, 语义错误。
    """
    scs = [e for e in events if e["type"] == "SC"]
    if scs:
        return float(scs[-1]["price"])
    if tr and tr.get("bottom"):
        return float(tr["bottom"])
    last = float(df["close"].iloc[-1])
    lows = [p["price"] for p in pivots if p["type"] == "low"
            and p["idx"] >= len(df) - 60 and p["price"] < last]
    return max(lows) if lows else None


def _rally_objective(df, events, pivots, tr):
    """已完成上涨的目标高点: 优先最近 BC 高点, 否则 TR 上轨, 否则近 60 日枢轴高点。"""
    bcs = [e for e in events if e["type"] == "BC"]
    if bcs:
        return float(bcs[-1]["price"])
    if tr and tr.get("top"):
        return float(tr["top"])
    last = float(df["close"].iloc[-1])
    highs = [p["price"] for p in pivots if p["type"] == "high"
             and p["idx"] >= len(df) - 60 and p["price"] > last]
    return min(highs) if highs else None


def _resistance_level(df, pivots, tr):
    if tr and tr.get("top"):
        return float(tr["top"])
    last = float(df["close"].iloc[-1])
    highs = [p["price"] for p in pivots if p["type"] == "high"
             and p["idx"] >= len(df) - 60 and p["price"] > last]
    return min(highs) if highs else None


def _test_counts(df, pivots, level, is_support):
    """统计近 60 日对 level 的测试次数与最近一次测试的量。"""
    if not level:
        return 0, 0.0
    n = len(df)
    start = n - 60
    count = 0
    last_vol = 0.0
    for p in pivots:
        if p["idx"] < start:
            continue
        if is_support and p["type"] == "low" and abs(p["price"] / level - 1) < 0.03:
            count += 1
            last_vol = float(df["volume"].iloc[p["idx"]])
        elif not is_support and p["type"] == "high" and abs(p["price"] / level - 1) < 0.03:
            count += 1
            last_vol = float(df["volume"].iloc[p["idx"]])
    return count, last_vol


def _spring_confirmed(events, df):
    """最近 Spring 是否成功 (后 8 根收盘未跌破其低点)。"""
    springs = [e for e in events if e["type"] == "Spring"]
    if not springs:
        return False
    sp = springs[-1]
    after = df["close"].values[sp["idx"] + 1:sp["idx"] + 9]
    if after.size == 0:
        return False
    return after.min() >= sp["price"]


def _rs_txt(rs):
    """相对强度 dict ({window: %}) → 展示文本。"""
    if not rs:
        return "无大盘参照"
    items = sorted(rs.items())
    return " ".join(f"{w}日RS {v:+.1f}%" for w, v in items)


def nine_tests(df, events, pivots=None, phase=None, structure=None,
               tr=None, pnf_t=None, rs=None):
    """九大检验, 返回:
      buy        买入侧 [{name, passed, detail, req}]
      buy_passed 买入侧通过数
      sell       卖出侧 [{name, passed, detail, req}]
      sell_passed 卖出侧通过数
      side       'buy'/'sell'/'' 当前相关侧 (由阶段/结构决定)
    """
    pivots = pivots or []
    last_close = float(df["close"].iloc[-1])
    recent = _recent(events, df, span=160)
    types = [e["type"] for e in recent]
    up_vol, dn_vol = _vol_wave(df)
    lo_ma = df["vol_ma20"].iloc[-1] if "vol_ma20" in df.columns else 0.0

    def _mk(tests):
        return [{"name": n, "passed": bool(p), "detail": d, "req": r}
                for n, p, d, r in tests]

    # ── 买入侧 (吸筹成熟度) ──
    sup = _support_level(df, pivots, tr)
    sup_tests, last_test_vol = _test_counts(df, pivots, sup, True)
    dn_obj = _decline_objective(df, events, pivots, tr)
    has_seq = all(t in types for t in ("SC", "AR", "ST"))
    spring_ok = _spring_confirmed(events, df)
    buy = _mk([
        ("T1 下跌目标已达到",
         dn_obj is None or last_close <= dn_obj * 1.05,
         f"已完成下跌低点 {dn_obj:.2f} vs 现价 {last_close:.2f}" if dn_obj
         else "无下跌参照 (SC/区间下沿缺失, 跳过)",
         "现价 ≤ 已完成下跌低点 (SC低点/区间下沿) ×1.05"),
        ("T2 PS→SC→AR→ST序列完成", has_seq,
         "有" if has_seq else "SC/AR/ST 序列不完整",
         "近160日 SC→AR→ST 事件齐全"),
        ("T3 看涨量价 (反弹放量/回调缩量)", up_vol > dn_vol,
         f"涨波量 {up_vol:.0f} vs 跌波量 {dn_vol:.0f}",
         "反弹波量 > 回调波量 (涨波放量/回调缩量)"),
        ("T4 支撑位确立 (≥2次测试)", sup_tests >= 2,
         f"支撑 {sup:.2f} 测试 {sup_tests} 次" if sup else "无明确支撑",
         "支撑位 (TR下沿/枢轴低点) 近60日测试 ≥ 2 次"),
        ("T5 供应枯竭 (低量回踩)", lo_ma > 0 and 0 < last_test_vol < lo_ma,
         f"最近测试量 {last_test_vol:.0f} vs 量均 {lo_ma:.0f}",
         "最近一次回踩量 >0 且 < 量均20"),
        ("T6 个股强于大盘",
         bool(rs) and any(v > 1.0 for v in rs.values()),
         _rs_txt(rs),
         "任一窗口 RS > +1.0%"),
        ("T7 Spring/震仓已确认", spring_ok,
         "Spring 已确认" if spring_ok else "Spring 未现或已失败",
         "Spring 后 8 根收盘未跌破其低点"),
        ("T8 趋势线突破或SOS出现", bool({"SOS", "JOC"} & set(types)),
         "有 SOS/JOC" if {"SOS", "JOC"} & set(types) else "无强势信号",
         "出现 SOS 或 JOC 强势信号"),
        ("T9 因果充分 (TR≥30日)",
         (tr or {}).get("bottom_tests", 0) * 10 >= 30 or
         (len(df) >= 80 and last_close > 0),
         "结构已具备时间积累",
         "TR 积累 ≥ 30 日 或 行情 ≥ 80 日"),
    ])

    # ── 卖出侧 (派发成熟度) ──
    res = _resistance_level(df, pivots, tr)
    res_tests, last_rally_vol = _test_counts(df, pivots, res, False)
    rally_obj = _rally_objective(df, events, pivots, tr)
    has_dist_seq = all(t in types for t in ("BC", "AR", "UTAD"))
    sell = _mk([
        ("T1 上涨目标已达到",
         rally_obj is None or last_close >= rally_obj * 0.95,
         f"已完成上涨高点 {rally_obj:.2f} vs 现价 {last_close:.2f}" if rally_obj
         else "无上涨参照 (BC/区间上沿缺失, 跳过)",
         "现价 ≥ 已完成上涨高点 (BC高点/区间上沿) ×0.95"),
        ("T2 BC→AR→UTAD序列完成", has_dist_seq,
         "有" if has_dist_seq else "BC/AR/UTAD 序列不完整",
         "近160日 BC→AR→UTAD 事件齐全"),
        ("T3 看跌量价 (下跌放量/反弹缩量)", dn_vol > up_vol,
         f"跌波量 {dn_vol:.0f} vs 涨波量 {up_vol:.0f}",
         "下跌波量 > 反弹波量 (跌波放量/反弹缩量)"),
        ("T4 阻力位确立 (≥2次测试)", res_tests >= 2,
         f"阻力 {res:.2f} 测试 {res_tests} 次" if res else "无明确阻力",
         "阻力位 (TR上沿/枢轴高点) 近60日测试 ≥ 2 次"),
        ("T5 需求枯竭 (低量反弹)", lo_ma > 0 and 0 < last_rally_vol < lo_ma,
         f"最近反弹量 {last_rally_vol:.0f} vs 量均 {lo_ma:.0f}",
         "最近一次反弹量 >0 且 < 量均20"),
        ("T6 个股弱于大盘",
         bool(rs) and any(v < 1.0 for v in rs.values()),
         _rs_txt(rs),
         "任一窗口 RS < +1.0%"),
        ("T7 UTAD/UT已确认", "UTAD" in types,
         "已现 UTAD" if "UTAD" in types else "无 UTAD",
         "出现 UTAD 事件"),
        ("T8 趋势线跌破或弱势确认",
         bool("UTAD" in types and sup and last_close <= sup),
         "UTAD 后跌破支撑" if ("UTAD" in types and sup and last_close <= sup) else "未见破位",
         "UTAD 后现价跌破支撑位"),
        ("T9 派发原因充分 (TR≥30日)",
         bool(tr) or len(df) >= 80,
         "结构已具备时间积累",
         "存在 TR 区间 或 行情 ≥ 80 日"),
    ])

    # 相关侧: 由阶段/结构决定优先展示哪一侧
    side = ""
    if phase:
        if any(k in phase for k in ("Accumulation", "吸筹", "底部", "上升")):
            side = "buy"
        elif any(k in phase for k in ("Distribution", "派发", "顶部", "下跌")):
            side = "sell"
    return {
        "buy": buy, "buy_passed": sum(1 for t in buy if t["passed"]),
        "sell": sell, "sell_passed": sum(1 for t in sell if t["passed"]),
        "side": side,
    }


def nt_lines(nt, phase=None):
    """九大检验 → 结论区文本行。"""
    if not nt:
        return ["  (无数据)"]
    side = phase and nt.get("side") or nt.get("side") or "buy"
    key = "buy" if side == "buy" else "sell"
    label = "买入(吸筹成熟度)" if key == "buy" else "卖出(派发成熟度)"
    passed = nt[f"{key}_passed"]
    total = len(nt[key])
    lines = [f"  九大{label}检验: 通过 {passed}/{total}"]
    for t in nt[key]:
        mark = "✓" if t["passed"] else "✗"
        lines.append(f"    {mark} {t['name']} — {t['detail']}")
        lines.append(f"      检验要求: {t.get('req', '')}")
    return lines
