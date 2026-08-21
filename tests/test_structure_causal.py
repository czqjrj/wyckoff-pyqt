# -*- coding: utf-8 -*-
"""结构进度因果链校验: 孤立/越序事件不推进阶段, 前置存在时才推进。

威科夫本质 = 阶段因果链: Spring 需先有 SC/ST, LPS 需先有 SOS/JOC,
UTAD 需先有 BC。旧版"事件序号取最大"会把无铺垫的孤立高等级事件
直接推升阶段, 违背因果定律。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from wyckoff.structure import structure_progress, _has_prereq


def _df(n=240):
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 100.0,
    })


def _ev(type_, idx, conf=80):
    return {"type": type_, "idx": idx, "conf": conf, "date": pd.Timestamp("2024-01-01")}


def test_isolated_spring_does_not_advance():
    """无 SC/ST 铺垫的孤立 Spring 不应推进到 Phase C (因果: 弹簧必须先有吸筹铺垫)。"""
    df = _df()
    events = [_ev("Spring", 60)]
    letter, _, detail = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "A"
    assert "未推进" in detail


def test_spring_after_sc_advances():
    """SC 后出现的 Spring 有吸筹铺垫 → 推进到 Phase C。"""
    df = _df()
    events = [_ev("SC", 45, conf=90), _ev("Spring", 60)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "C"


def test_shakeout_requires_sc_st_prereq():
    """震仓 Shakeout 与 Spring 同构: 无 SC/ST 铺垫不推进, 有铺垫推进。"""
    df = _df()
    isolated = structure_progress([_ev("Shakeout", 60)], df,
                                  phase="底部整固 (Accumulation)")
    assert isolated[0] == "A" and "未推进" in isolated[2]
    chained = structure_progress([_ev("SC", 45, conf=90), _ev("Shakeout", 60)],
                                 df, phase="底部整固 (Accumulation)")
    assert chained[0] == "C"


def test_spring_beyond_window_blocked():
    """SC 距今超过 60 根窗口, 前置失效 → Spring 不推进。"""
    df = _df()
    events = [_ev("SC", 45, conf=90), _ev("Spring", 120)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter != "C"


def test_lps_requires_sos_joc():
    """无 SOS/JOC 的孤立 LPS 不推进到 Phase D。"""
    df = _df()
    events = [_ev("SC", 45, conf=90), _ev("LPS", 90)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter != "D"


def test_lps_after_sos_advances():
    """SOS 之后的 LPS 是标准买点结构 → Phase D。"""
    df = _df()
    events = [_ev("SC", 45, conf=90), _ev("Spring", 60),
              _ev("SOS", 90), _ev("LPS", 100)]
    letter, _, detail = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "D"
    assert "未推进" not in detail


def test_full_accumulation_chain_reaches_e():
    """完整因果链 SC→ST→Spring→SOS→LPS→JOC → Phase E (JOC 前置 SOS 而非 SC)。"""
    df = _df()
    events = [_ev("SC", 45, conf=90), _ev("ST", 55, conf=85),
              _ev("Spring", 65, conf=90), _ev("SOS", 80),
              _ev("LPS", 90), _ev("JOC", 115, conf=95)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "E"


def test_utad_requires_bc():
    """无 BC 的孤立 UTAD 在派发结构中被前置拦截。"""
    df = _df(300)
    events = [_ev("UTAD", 120, conf=90)]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter != "C"
    assert "未推进" in detail


def test_utad_after_bc_advances():
    """BC 之后出现的 UTAD → 派发 Phase C (出货关键确认)。"""
    df = _df(300)
    events = [_ev("BC", 110, conf=95), _ev("AR", 125, conf=70),
              _ev("UT", 140, conf=75), _ev("UTAD", 160)]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter == "C"
    assert "未推进" not in detail


def test_dedup_types_preserved():
    """对冲: 不识别的事件类型不应推进阶段 (marker 无该项)。"""
    df = _df()
    events = [_ev("SC", 20, conf=90), _ev("UNKNOWN_SIG", 40, conf=99)]
    letter, _, _ = structure_progress(events, df, phase="底部整固 (Accumulation)")
    assert letter == "A"


def test_has_prereq_window_bounds():
    """前置回溯窗口边界: 窗口内命中为 True, 超出为 False。"""
    events = [_ev("SC", 10, conf=90)]
    assert _has_prereq(events, _ev("Spring", 40), ("SC", "ST"), 40)
    assert not _has_prereq(events, _ev("Spring", 60), ("SC", "ST"), 40)


def test_prereq_low_conf_not_counted():
    """置信度低于阈值的前置事件不算有效铺垫。"""
    events = [_ev("SC", 10, conf=30)]
    assert not _has_prereq(events, _ev("Spring", 40), ("SC", "ST"), 40)


def test_lpsy_requires_utad_bc():
    """无 UTAD/BC 的孤立 LPSY 在派发结构中被前置拦截 (LPSY 是派发 Phase D 末端)。"""
    df = _df(300)
    events = [_ev("LPSY", 120, conf=85)]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter != "D"
    assert "未推进" in detail


def test_lpsy_after_utad_advances_to_phase_d():
    """完整派发因果链 BC→AR→UT→UTAD→LPSY 推进到 Phase D (最后供应点)。"""
    df = _df(300)
    events = [_ev("BC", 110, conf=95), _ev("AR", 125, conf=70),
              _ev("UT", 140, conf=75), _ev("UTAD", 160),
              _ev("LPSY", 180, conf=80)]
    letter, name, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter == "D"
    assert "未推进" not in detail
    assert "Phase D" in detail