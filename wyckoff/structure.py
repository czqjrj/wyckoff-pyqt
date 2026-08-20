# -*- coding: utf-8 -*-
"""吸筹/派发结构进度判断。

威科夫本质是"阶段因果链"而非"事件拼图": 结构进度不能只看出现了哪些高等级
事件, 而必须校验因果顺序 —— Spring 必须有 SC/ST 铺垫, LPS 必须出现在 SOS/JOC
之后, UTAD 必须先有买盘高潮 BC。这里用"前置事件 + 回溯窗口"把每个事件的
推进资格固化下来, 防止把孤立/越序事件推断成结构进度 (孤立 Spring 命中率低,
无吸筹背景的 SOS 实测命中率仅 31%)。
"""
from .config import ACC_PHASES, DIST_PHASES, W_PIVOT_LONG

_ACC_TYPES = ("PSY", "SC", "ST", "Spring", "SOS", "LPS", "BU", "JOC")
_DIST_TYPES = ("BC", "AR", "UT", "UTAD", "LPSY", "SOW")

# ── 事件推进前置约束: type -> (前置事件类型, 回溯窗口根数) ──
# 空元组表示无前置 (阶段起点事件)。事件不满足前置时不推进结构进度。
# 依据威科夫因果链:
#   吸筹: PSY→SC→AR→ST→Spring→(SOS→LPS|BU)→JOC
#   派发: BC→AR→UT→UTAD; 高位弱势信号也需 BC 背景 (否则只是趋势内回撤)
_ACC_PREREQ = {
    "PSY": ((), 0),
    "SC": ((), 0),
    "AR": (("SC",), 40),
    "ST": (("SC",), 60),
    "Spring": (("SC", "ST"), 60),
    "SOS": (("SC", "Spring", "ST"), 60),
    "LPS": (("SOS", "JOC"), 40),
    "BU": (("JOC",), 40),
    # JOC 是基底完成的突破 (Phase E 起点), 因果上须先有强势信号 SOS 铺垫;
    # 不要求紧贴 SC (基底构筑可能远超 60 根后才有 JOC)
    "JOC": (("SOS",), 60),
}
_DIST_PREREQ = {
    "BC": ((), 0),
    "AR": (("BC",), 40),
    "UT": (("BC",), 60),
    "UTAD": (("BC", "UT"), 60),
    "LPSY": (("UTAD", "BC"), 60),
    "SOS": (("BC",), 60),
    "LPS": (("BC",), 60),
    # 派发对应的 JOC 是破位下穿, 因果上须先有出货确认 (UTAD)
    "JOC": (("UTAD",), 60),
    # SOW 弱势信号是派发 Phase D→E 的破位确认, 前置须有 UTAD 出货铺垫
    "SOW": (("UTAD", "LPSY"), 60),
}

# 阶段 marker (推进后所在阶段), 与 config 的 ACC/DIST_PHASES 对齐
_ACC_MARKER = {"PSY": 0, "SC": 0, "AR": 0, "ST": 1, "Spring": 2,
               "SOS": 3, "LPS": 3, "BU": 3, "JOC": 4}
_DIST_MARKER = {"BC": 0, "AR": 0, "UT": 1, "UTAD": 2, "LPSY": 3, "SOS": 3,
                "LPS": 3, "JOC": 4, "SOW": 3}


def _prereqs_for(kind: str):
    """按结构方向返回前置约束表与 marker 映射。"""
    if kind == "acc":
        return _ACC_PREREQ, _ACC_MARKER, ACC_PHASES, "吸筹 (Accumulation)"
    return _DIST_PREREQ, _DIST_MARKER, DIST_PHASES, "派发 (Distribution)"


def _kind_by_phase(phase):
    """按判段面板决定结构类型: 底部/吸筹/上升 → 吸筹; 顶部/派发/下跌 → 派发。
    返回结构方向 key ("acc"/"dist") 或 None。"""
    if not phase:
        return None
    if any(k in phase for k in ("Accumulation", "吸筹", "底部", "上升")):
        return "acc"
    if any(k in phase for k in ("Distribution", "派发", "顶部", "下跌")):
        return "dist"
    return None


def _kind_by_events(recent, start, span):
    """区间整理/待定时用事件打分: 越近期权重越高 (避免早期反弹事件盖过当前派发)。"""
    acc_w = dist_w = 0.0
    for e in recent:
        w = 0.3 + 0.7 * (e["idx"] - start) / span
        t = e["type"]
        if t in _ACC_TYPES:
            acc_w += w
        elif t in _DIST_TYPES:
            dist_w += w
    return "dist" if dist_w > acc_w else "acc"


def _has_prereq(events, e, prereq, window, conf_thr=40):
    """某事件在 e 之前 window 根内是否出现任一前置类型事件 (且置信度达标)。

    events 需已按 idx 升序。因果校验核心: 后验事件只有在前置铺垫存在时才
    算"结构推进", 孤立事件 (如无 SC/ST 的 Spring) 不构成威科夫阶段证据。
    """
    if not prereq:
        return True
    i = e["idx"]
    for o in events:
        if o["idx"] >= i:
            break
        if i - o["idx"] > window:
            continue
        if o.get("type") in prereq and o.get("conf", 100) >= conf_thr:
            return True
    return False


def _progress(events, kind):
    """按因果链推进结构阶段: 事件逐个出现, 只有满足前置约束的高等级事件
    才推进 cur (越序/孤立事件被拦截, 不参与阶段判断)。

    返回 (cur, blocked) — blocked 为被前置约束拦下的 (事件类型, 所需前置) 摘要。
    """
    prereq, marker, _, _ = _prereqs_for(kind)
    order = sorted(events, key=lambda x: x["idx"])
    cur = 0
    blocked = []
    for e in order:
        st = marker.get(e.get("type", ""), -1)
        if st <= cur or e.get("conf", 100) < 40:
            continue
        need, win = prereq.get(e["type"], ((), 0))
        if not _has_prereq(order, e, need, win):
            blocked.append((e["type"], need))
            continue
        cur = st
    return cur, blocked


def structure_progress(events: list, df, phase: str = None):
    """基于事件时间线判断吸筹/派发结构进度, 返回 (阶段字母, 阶段描述, 详细进度文本)。

    吸筹/派发类型优先以判段面板 (judge_phase) 为准, 与"阶段"卡片口径一致;
    面板为区间整理/待定时回退到按事件打分 (时间加权, 近期事件权重更高)。

    与旧版 (事件序号取最大) 的区别: 结构进度要求因果前置 —— Spring 需先有
    SC/ST, LPS/BU 需先有 SOS/JOC, UTAD 需先有 BC。无前置的高等级孤立事件
    会被拦截并计入进度文本 (blocked), 让"为什么阶段没推进"可解释。
    """
    recent = [e for e in events if e["idx"] >= len(df) - W_PIVOT_LONG]
    kind = _kind_by_phase(phase)
    if kind is None:
        n = len(df)
        start = n - 200
        kind = _kind_by_events(recent, start, max(1, n - start))

    cur, blocked = _progress(recent, kind)
    _, _, phases, kind_txt = _prereqs_for(kind)
    letter, name, note = phases[cur]

    detail = f"{kind_txt}\n当前进度: Phase {letter} — {name}\n含义: {note}"
    if blocked:
        descs = ", ".join(f"{t}(需前置 {'/'.join(p) if p else '—'})" for t, p in blocked)
        detail += f"\n注意: {descs} 因缺少前置铺垫未推进(孤立/越序事件)"
    return letter, name, detail