"""反面证据积分追踪 (Counter-Evidence) 与紧急反转提示。

借鉴 WyckoffPro CounterEvidenceTracker 的思想: 对当前"吸筹/派发"结构假设
维护一个反面证据积分 (0~100)。反向事件出现越多、越严重, 加分越多; 正向
确认信号则消减积分。积分越过黄色/橙色警戒线提示怀疑, 越过红色线 (71) 触发
"紧急反转"提示 —— 当前假设可能站不住, 需打问号。

    与 WyckoffPro 的差异: 本模块纯规则、无状态持久化、无外部依赖, 每次分析
    即时计算, 事件类型适配本项目检测器 (SC/BC/AR/ST/Spring/UTAD/SOS/JOC/LPS/BU/PSY)。
"""
from ._shared import spring_state as _spring_state

ALERT_NONE = "NONE"
ALERT_YELLOW = "YELLOW"    # 31-50
ALERT_ORANGE = "ORANGE"    # 51-70
ALERT_RED = "RED"          # >=71

YELLOW_TH = 31
ORANGE_TH = 51
RED_TH = 71

# 吸筹假设下的反面(派发倾向)事件加分表
ACC_COUNTER = {
    "UTAD": (25, "上冲派发 → 区间内出货迹象"),
    "BC": (15, "买入高潮 → 高位放量滞涨"),
    "SPRING_FAIL": (25, "Spring 后跌破其低点 → 弹簧失败"),
    "SC_BREAK": (35, "SC 低点被有效跌破 → 底不成底"),
    "VOL_REVERSE": (15, "下跌波量 > 上涨波量 → 量价逆转"),
    "NO_DEMAND": (10, "无强势需求信号 (无 SOS/JOC) → 反弹乏力"),
}
# 吸筹假设下的正向(吸筹确认)消减表
ACC_POSITIVE = {
    "SOS": 20, "JOC": 20, "ST": 15, "SPRING_OK": 25,
}

# 派发假设下的反面(吸筹倾向)事件加分表
DIS_COUNTER = {
    "Spring": (25, "派发区弹簧 → 需求进场迹象"),
    "Shakeout": (25, "放量假破位收复 → 震仓, 派发假设动摇"),
    "SOS": (20, "派发区 SOS → 强势信号不弱"),
    "JOC": (30, "派发区 JOC 突破 → 假突破风险"),
    "BC_BREAK": (30, "价格收盘突破 BC 高点 → 派发假设动摇"),
    "VOL_STRONG": (15, "上涨波量 > 下跌波量 → 需求占优"),
}
# 派发假设下的正向(派发确认)消减表
DIS_POSITIVE = {
    "UTAD": 20, "AR": 10, "UT": 8, "LPSY": 12, "SOW": 20,
}


def _recent(events, df, span=None):
    from ._shared import recent_events
    return recent_events(events, df, span=span)


def _vol_wave(df, window=40):
    from ._shared import vol_wave
    return vol_wave(df, window=window)


def _hypothesis(phase, structure):
    """判定当前结构假设: '吸筹' / '派发' / ''。优先判段面板, 结构进度兜底。"""
    if phase:
        if any(k in phase for k in ("Accumulation", "吸筹", "底部")):
            return "吸筹"
        if any(k in phase for k in ("Distribution", "派发", "顶部")):
            return "派发"
    if structure and len(structure) >= 3:
        first = structure[2].splitlines()[0] if structure[2] else ""
        if "吸筹" in first:
            return "吸筹"
        if "派发" in first:
            return "派发"
    return ""


def _spring_ok(events, df):
    """最近一个 Spring 的**当前**有效性。返回 (有效, 已失败)。

    统一口径见 _shared.spring_state: 不只看事件后 8 根, 还检查此后直至当前
    bar 有没有跌破 Spring 低点。旧实现 8 根之后永久返回"成功", 会让已经
    破位的 Spring 继续抵消反面证据。
    """
    st = _spring_state(events, df, types=("Spring",))
    return bool(st["active"]), bool(st["failed"])


def counter_evidence(df, events, phase=None, structure=None) -> dict:
    """反面证据积分计算, 返回:
      hypothesis    当前假设 (吸筹/派发)
      score         反面积分 (0~100)
      alert_level   NONE/YELLOW/ORANGE/RED
      reversal      是否触发紧急反转 (score >= 71)
      reversal_reason  反转原因文案
      events        [{"name", "delta", "desc"}, ...] 本次命中的事件明细
    """
    base = {"hypothesis": "", "score": 0.0, "alert_level": ALERT_NONE,
            "reversal": False, "reversal_reason": "", "events": [],
            "deductions": []}
    hypothesis = _hypothesis(phase, structure)
    if not hypothesis:
        base["hypothesis"] = "未定"
        return base

    recent = _recent(events, df)
    types = [e["type"] for e in recent]
    close = df["close"].values
    last_close = float(df["close"].iloc[-1])
    up_vol, dn_vol = _vol_wave(df)
    spring_ok, spring_fail = _spring_ok(recent, df)

    fired = []  # (name, delta, desc)
    # 正向消减项: 计入 deductions 而非直接吞掉, 否则"明细有 +25 但积分 0"
    # 会让读者以为加分丢失 (实际是被正向证据抵消到底)。ce_lines 会一并展示。
    deductions = []  # (name, delta, desc), delta 为负值

    if hypothesis == "吸筹":
        counter = ACC_COUNTER
        for t, (delta, desc) in counter.items():
            if t in ("UTAD", "BC") and t in types:
                fired.append((t, delta, desc))
        if spring_fail:
            fired.append(("SPRING_FAIL", counter["SPRING_FAIL"][0], counter["SPRING_FAIL"][1]))
        scs = [e for e in recent if e["type"] == "SC"]
        if scs and close[-1] < scs[-1]["price"]:
            fired.append(("SC_BREAK", counter["SC_BREAK"][0], counter["SC_BREAK"][1]))
        if up_vol > 0 and dn_vol > up_vol * 1.15:
            fired.append(("VOL_REVERSE", counter["VOL_REVERSE"][0], counter["VOL_REVERSE"][1]))
        if not any(t in types for t in ("SOS", "JOC", "LPS", "BU")):
            fired.append(("NO_DEMAND", counter["NO_DEMAND"][0], counter["NO_DEMAND"][1]))
        score = sum(d for _, d, _ in fired)
        # 正向消减
        if "SOS" in types:
            deductions.append(("SOS", -ACC_POSITIVE["SOS"], "强势信号 → 吸筹确认"))
        if "JOC" in types:
            deductions.append(("JOC", -ACC_POSITIVE["JOC"], "放量突破 → 吸筹确认"))
        if "ST" in types:
            deductions.append(("ST", -ACC_POSITIVE["ST"], "二次测试守住 → 支撑确认"))
        if spring_ok:
            deductions.append(("SPRING_OK", -ACC_POSITIVE["SPRING_OK"],
                               "Spring 当前仍有效 (未跌破低点) → 吸筹确认"))
        score += sum(d for _, d, _ in deductions)
        reversal_reason = _acc_reversal_reason(fired)
    else:  # 派发
        counter = DIS_COUNTER
        for t, (delta, desc) in counter.items():
            if t in ("Spring", "Shakeout", "SOS", "JOC") and t in types:
                fired.append((t, delta, desc))
        bcs = [e for e in recent if e["type"] == "BC"]
        if bcs and last_close > bcs[-1]["price"]:
            fired.append(("BC_BREAK", counter["BC_BREAK"][0], counter["BC_BREAK"][1]))
        if dn_vol > 0 and up_vol > dn_vol * 1.15:
            fired.append(("VOL_STRONG", counter["VOL_STRONG"][0], counter["VOL_STRONG"][1]))
        score = sum(d for _, d, _ in fired)
        for t, desc in (("UTAD", "上冲派发成立 → 派发确认"),
                        ("AR", "自动反弹 → 派发结构成立"),
                        ("UT", "上冲失败 → 派发确认"),
                        ("LPSY", "反弹无力 → 派发确认"),
                        ("SOW", "破位下跌 → 派发确认")):
            if t in types:
                deductions.append((t, -DIS_POSITIVE[t], desc))
        score += sum(d for _, d, _ in deductions)
        reversal_reason = _dis_reversal_reason(fired)

    score = float(min(100, max(0, score)))
    alert_level = _alert_level(score)
    # 排序去重: 按加分从高到低
    fired = sorted(fired, key=lambda x: -x[1])
    ev_list = [{"name": n, "delta": d, "desc": desc} for n, d, desc in fired]
    deductions = sorted(deductions, key=lambda x: x[1])  # 消减力度大的在前
    ded_list = [{"name": n, "delta": d, "desc": desc} for n, d, desc in deductions]

    base.update({
        "hypothesis": hypothesis,
        "score": round(score, 1),
        "alert_level": alert_level,
        "reversal": score >= RED_TH,
        "reversal_reason": reversal_reason if score >= RED_TH else "",
        "events": ev_list,
        "deductions": ded_list,
    })
    return base


def _acc_reversal_reason(fired):
    names = {n for n, _, _ in fired}
    if "SC_BREAK" in names:
        return "SC 低点被有效跌破 → 原判'吸筹'可能是下跌中继, 假设被推翻"
    if "SPRING_FAIL" in names and ("VOL_REVERSE" in names or "UTAD" in names):
        return "Spring 失败且量价转弱 → 吸筹区实为派发区, 原假设被推翻"
    if "UTAD" in names:
        return "反复上冲派发 + 无需求反弹 → 可能是派发而非吸筹, 原假设存疑"
    return "反面证据显著累积 → 原吸筹假设存疑, 警惕方向反转"


def _dis_reversal_reason(fired):
    names = {n for n, _, _ in fired}
    if "BC_BREAK" in names:
        return "价格突破 BC 高点 → 原判'派发'可能是再吸筹, 假设被推翻"
    if "JOC" in names and "Spring" in names:
        return "派发区出现 Spring + JOC 突破 → 更可能是吸筹后上攻, 原假设被推翻"
    if "SOS" in names:
        return "派发区持续出现 SOS 强势信号 → 派发假设存疑, 警惕反转向上"
    return "反面证据显著累积 → 原派发假设存疑, 警惕方向反转"


def _alert_level(score):
    if score >= RED_TH:
        return ALERT_RED
    if score >= ORANGE_TH:
        return ALERT_ORANGE
    if score >= YELLOW_TH:
        return ALERT_YELLOW
    return ALERT_NONE


def ce_lines(ce, phase=None):
    """反面积分 → 结论区文本行。"""
    if not ce or not ce.get("hypothesis"):
        return ["  (方向未定, 不计算反面证据)"]
    lvl_cn = {"NONE": "正常", "YELLOW": "黄灯", "ORANGE": "橙灯", "RED": "红灯"}
    hyp = ce["hypothesis"]
    lines = [f"  假设: {hyp}结构   反面积分: {ce['score']:.0f}/100 "
             f"({lvl_cn.get(ce['alert_level'], '?')})"]
    if not ce["events"] and not ce.get("deductions"):
        lines.append("  暂无反面事件, 假设未被质疑")
        return lines
    if ce["events"]:
        lines.append("  反面事件明细:")
        for ev in ce["events"]:
            lines.append(f"    {ev['name']} {ev['delta']:+d}  {ev['desc']}")
    else:
        lines.append("  暂无反面事件 (假设未被质疑)")
    if ce.get("deductions"):
        lines.append("  正向消减 (抵消反面积分):")
        for ev in ce["deductions"]:
            lines.append(f"    {ev['name']} {ev['delta']:+d}  {ev['desc']}")
        if ce["score"] <= 0 < sum(e["delta"] for e in ce["events"]):
            lines.append("    (消减 >= 加分, 积分归零)")
    if ce["reversal"]:
        lines.append(f"  🚨 紧急反转提示: {ce['reversal_reason']}")
    return lines
