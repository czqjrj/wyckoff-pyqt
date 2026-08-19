# -*- coding: utf-8 -*-
"""吸筹/派发结构进度判断。"""
import pandas as pd

from .config import ACC_PHASES, DIST_PHASES, W_PIVOT_LONG

_ACC_TYPES = ("PSY", "SC", "ST", "Spring", "SOS", "LPS", "BU", "JOC")
_DIST_TYPES = ("BC", "AR", "UT", "UTAD")


def _kind_by_phase(phase):
    """按判段面板决定结构类型: 底部/吸筹/上升 → 吸筹; 顶部/派发/下跌 → 派发。
    返回 (kind 文案, phases 表, marker 映射) 或 None。"""
    if not phase:
        return None
    if any(k in phase for k in ("Accumulation", "吸筹", "底部", "上升")):
        return ("吸筹 (Accumulation)", ACC_PHASES,
                {"PSY": 0, "SC": 0, "AR": 0, "ST": 1, "Spring": 2,
                 "SOS": 3, "LPS": 3, "BU": 3, "JOC": 4})
    if any(k in phase for k in ("Distribution", "派发", "顶部", "下跌")):
        return ("派发 (Distribution)", DIST_PHASES,
                {"BC": 0, "AR": 0, "UT": 1, "UTAD": 2, "SOS": 3,
                 "LPS": 3, "JOC": 4})
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
    if dist_w > acc_w:
        return ("派发 (Distribution)", DIST_PHASES,
                {"BC": 0, "AR": 0, "UT": 1, "UTAD": 2, "SOS": 3,
                 "LPS": 3, "JOC": 4})
    return ("吸筹 (Accumulation)", ACC_PHASES,
            {"PSY": 0, "SC": 0, "AR": 0, "ST": 1, "Spring": 2,
             "SOS": 3, "LPS": 3, "BU": 3, "JOC": 4})


def structure_progress(events: list, df: pd.DataFrame, phase: str = None):
    """基于事件时间线判断吸筹/派发结构进度, 返回 (阶段字母, 阶段描述, 详细进度文本)。

    吸筹/派发类型优先以判段面板 (judge_phase) 为准, 与"阶段"卡片口径一致;
    面板为区间整理/待定时回退到按事件打分 (时间加权, 近期事件权重更高)。
    """
    recent = [e for e in events if e["idx"] >= len(df) - W_PIVOT_LONG]
    kind = _kind_by_phase(phase)
    if kind is None:
        n = len(df)
        start = n - 200
        kind = _kind_by_events(recent, start, max(1, n - start))
    kind_txt, phases, marker = kind

    cur = 0
    for e in sorted(recent, key=lambda x: x["idx"]):
        st = marker.get(e["type"], -1)
        if st > cur and e.get("conf", 100) >= 40:
            cur = st
    letter, name, note = phases[cur]
    return letter, name, f"{kind_txt}\n当前进度: Phase {letter} — {name}\n含义: {note}"
