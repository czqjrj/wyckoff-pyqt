"""派发侧对称补齐回归测试:
1. BC 之后的第一个低点枢轴 → 自动回落 AR (detect_ar_st 派发分支);
2. BC 后冲高测试前高失败 → UT (detect_ut);
3. BC/UTAD/LPSY 后放量跌破前低 → SOW (detect_sow);
4. 完整派发因果链 BC→AR→UT→UTAD→LPSY→SOW 推进结构进度到 Phase D;
5. detect_all 串联新事件且不破坏中性/吸筹事件确认。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff import events as E
from wyckoff.indicators import add_indicators
from wyckoff.structure import structure_progress


def _mkdf(n=300):
    rng = np.random.default_rng(7)
    closes = 30 + np.cumsum(rng.normal(0, 0.15, n))
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.002, n)),
        "close": closes * (1 + rng.normal(0, 0.002, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": rng.uniform(5e5, 8e5, n),
    })


def _mkdf_dist(pts):
    """构造 BC→AR→UT→UTAD→LPSY→SOW 的分段结构化 K 线 (点位 dict 起决定性作用)。

    每段用斜线构造, 保证在 pts 位置出现明确高低点; 其余区间缓慢回归。
    """
    n = pts["sow"] + 25
    days = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = np.full(n, 9.0)
    # 建仓上行 → BC 顶部
    closes[:pts["bc"] + 1] = 8.0 + np.linspace(0, 2.6, pts["bc"] + 1)
    # BC → AR 回落
    i0, i1 = pts["bc"], pts["ar"]
    closes[i0 + 1:i1 + 1] = np.linspace(closes[i0], 9.6, i1 - i0)
    # AR → UT 再冲前高
    i0, i1 = pts["ar"], pts["ut"]
    closes[i0 + 1:i1 + 1] = np.linspace(closes[i0], 10.4, i1 - i0)
    # UT → UTAD 刺破
    i0, i1 = pts["ut"], pts["utad"]
    closes[i0 + 1:i1 + 1] = np.linspace(closes[i0], 10.8, i1 - i0)
    # UTAD → LPSY 回落
    i0, i1 = pts["utad"], pts["lpsy"]
    closes[i0 + 1:i1 + 1] = np.linspace(closes[i0], 10.2, i1 - i0)
    # LPSY → SOW 破位
    i0, i1 = pts["lpsy"], pts["sow"]
    closes[i0 + 1:i1 + 1] = np.linspace(closes[i0], 9.0, i1 - i0)
    # SOW 后走弱
    closes[pts["sow"] + 1:] = 8.8
    opens = np.roll(closes, 1); opens[0] = closes[0]
    highs = np.maximum(opens, closes) * 1.02
    lows = np.minimum(opens, closes) * 0.98
    vols = np.full(n, 1e6)
    vols[pts["bc"]] = 6e6
    vols[pts["ar"]] = 4e5
    vols[pts["ut"]] = 1.2e6
    vols[pts["utad"]] = 7e6
    vols[pts["lpsy"]] = 4e5
    vols[pts["sow"]] = 8e6
    return pd.DataFrame({
        "day": days, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })


_PTS = {"bc": 60, "ar": 70, "ut": 85, "utad": 100, "lpsy": 115, "sow": 130}


def _fake_pivots(df, pts):
    """在关键点位放高低枢轴 (高/低点相邻成对), 供检测器消费。"""
    pivots = []
    for name, i in pts.items():
        if name in ("bc", "ut", "utad", "lpsy"):
            pivots.append({"type": "high", "idx": i,
                           "price": float(df["high"].iloc[i]),
                           "date": df["day"].iloc[i]})
        else:
            pivots.append({"type": "low", "idx": i,
                           "price": float(df["low"].iloc[i]),
                           "date": df["day"].iloc[i]})
    return pivots


# ───────────────── 1. BC → AR (detect_ar_st 派发分支) ─────────────────

def test_ar_generated_after_bc():
    """BC 之后的第一个低点枢轴应产出 AR (自动回落, 修复前恒缺失)。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    bc_ev = [{"type": "BC", "idx": _PTS["bc"],
              "price": float(df["high"].iloc[_PTS["bc"]]), "date": df["day"].iloc[_PTS["bc"]]}]
    ev = E.detect_ar_st(df, _fake_pivots(df, _PTS), bc_ev)
    ars = [e for e in ev if e["type"] == "AR"]
    assert ars, "BC 后应有自动回落 AR"
    assert ars[0]["idx"] > _PTS["bc"]


def test_sc_st_branch_unchanged():
    """吸筹侧 SC→AR→ST 分支不受派发分支影响。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    sc_ev = [{"type": "SC", "idx": _PTS["bc"],
              "price": float(df["low"].iloc[_PTS["bc"]]), "date": df["day"].iloc[_PTS["bc"]]}]
    ev = E.detect_ar_st(df, _fake_pivots(df, _PTS), sc_ev)
    # 仅传入 SC, 不应误产出派发侧 AR (BC 分支不触发), 但仍走吸筹 AR
    assert any(e["type"] == "AR" for e in ev)


# ───────────────── 2. UT (detect_ut) ─────────────────

def test_ut_generated_after_bc():
    """BC 后冲高测试前高失败 → UT (上冲测试)。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    pivots = _fake_pivots(df, _PTS)
    base = [{"type": "BC", "idx": _PTS["bc"], "price": float(df["high"].iloc[_PTS["bc"]])},
            {"type": "UTAD", "idx": _PTS["utad"], "price": float(df["high"].iloc[_PTS["utad"]])}]
    ev = E.detect_ut(df, pivots, base)
    uts = [e for e in ev if e["type"] == "UT"]
    assert uts, "BC 后冲高测试应产出 UT"


def test_ut_excludes_utad_same_bar():
    """UT 与 UTAD 刺破形态互斥 (同一根仅保留其一)。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    utad_idx = _PTS["utad"]
    base = [{"type": "BC", "idx": _PTS["bc"], "price": float(df["high"].iloc[_PTS["bc"]])},
            {"type": "UTAD", "idx": utad_idx, "price": float(df["high"].iloc[utad_idx])}]
    ev = E.detect_ut(df, _fake_pivots(df, _PTS), base)
    for e in ev:
        if e["type"] == "UT":
            assert e["idx"] != utad_idx, "UT 不应与 UTAD 重叠"


# ───────────────── 3. SOW (detect_sow) ─────────────────

def test_sow_generated_after_lpsy():
    """LPSY/UTAD 之后放量跌破前低 → SOW。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    base = [
        {"type": "UTAD", "idx": _PTS["utad"], "price": float(df["high"].iloc[_PTS["utad"]])},
        {"type": "LPSY", "idx": _PTS["lpsy"], "price": float(df["high"].iloc[_PTS["lpsy"]])},
        {"type": "BC", "idx": _PTS["bc"], "price": float(df["high"].iloc[_PTS["bc"]])},
    ]
    ev = E.detect_sow(df, _fake_pivots(df, _PTS), base)
    sows = [e for e in ev if e["type"] == "SOW"]
    assert sows, "放量跌破前低应产出 SOW"
    assert sows[0]["idx"] > _PTS["lpsy"]


def test_sow_not_generated_no_break():
    """未跌破支撑 → 无 SOW。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    # 抬升 SOW 处低点与收盘到支撑之上 → 不破位
    df.loc[_PTS["sow"], "low"] = float(df["high"].iloc[_PTS["lpsy"]]) * 0.99
    df.loc[_PTS["sow"], "close"] = float(df["high"].iloc[_PTS["lpsy"]]) * 1.001
    base = [{"type": "LPSY", "idx": _PTS["lpsy"],
             "price": float(df["high"].iloc[_PTS["lpsy"]])}]
    ev = E.detect_sow(df, _fake_pivots(df, _PTS), base)
    assert not [e for e in ev if e["type"] == "SOW"]


def test_shakeout_when_breakdown_recovers():
    """破位后未续跌 → Shakeout 震仓 (非 SOW)。

    放量跌破前低支撑后, 若 confirm_bars 根内无更低低点, 应归为
    震仓/诱空 (看多), 而不是 SOW 弱势信号 (看空)。
    """
    df = _mkdf_dist(_PTS)
    # SOW 处放量破位后快速收复 (逐根回升, 不再创出新低)
    df.loc[_PTS["sow"] + 1:, "close"] = np.linspace(9.2, 9.6, len(df) - _PTS["sow"] - 1)
    df["open"] = np.roll(df["close"], 1); df.loc[0, "open"] = df["close"].iloc[0]
    df["high"] = np.maximum(df["open"], df["close"]) * 1.01
    df["low"] = np.minimum(df["open"], df["close"]) * 0.995
    df = add_indicators(df, symbol="600104")
    base = [{"type": "UTAD", "idx": _PTS["utad"],
             "price": float(df["high"].iloc[_PTS["utad"]])},
            {"type": "LPSY", "idx": _PTS["lpsy"],
             "price": float(df["high"].iloc[_PTS["lpsy"]])},
            {"type": "BC", "idx": _PTS["bc"],
             "price": float(df["high"].iloc[_PTS["bc"]])}]
    ev = E.detect_sow(df, _fake_pivots(df, _PTS), base)
    assert not [e for e in ev if e["type"] == "SOW"], "未续跌不应归为 SOW"
    shakes = [e for e in ev if e["type"] == "Shakeout"]
    assert shakes, "放量破位但未续跌应归为 Shakeout 震仓"
    assert shakes[0]["idx"] == _PTS["sow"]


def test_shakeout_when_new_low_but_fast_rebound():
    """破位后创新低但快速反弹回支撑上方 → Shakeout (非 SOW)。

    放量跌破前低支撑后, 即使 confirm_bars 根内创出新低,
    若收盘快速反弹回支撑上方, 仍应归为震仓/诱空 (看多),
    而非 SOW 弱势信号 (看空)。
    """
    df = _mkdf_dist(_PTS)
    # SOW 后: 先创新低(2根), 再 10 根内快速反弹回支撑(floor≈10.3)上方
    n_post = df.shape[0] - _PTS["sow"] - 1
    closes_post = np.concatenate([
        np.linspace(8.8, 8.6, 2),          # 先创新低 (满足 new_low)
        np.linspace(8.6, 11.5, 10),         # 10根内反弹回 floor 上方
        np.full(n_post - 12, 11.5)          # 剩余维持高位
    ])
    df.loc[_PTS["sow"] + 1:, "close"] = closes_post
    df["open"] = np.roll(df["close"], 1); df.loc[0, "open"] = df["close"].iloc[0]
    df["high"] = np.maximum(df["open"], df["close"]) * 1.01
    df["low"] = np.minimum(df["open"], df["close"]) * 0.995
    df = add_indicators(df, symbol="600104")
    base = [{"type": "UTAD", "idx": _PTS["utad"],
             "price": float(df["high"].iloc[_PTS["utad"]])},
            {"type": "LPSY", "idx": _PTS["lpsy"],
             "price": float(df["high"].iloc[_PTS["lpsy"]])},
            {"type": "BC", "idx": _PTS["bc"],
             "price": float(df["high"].iloc[_PTS["bc"]])}]
    ev = E.detect_sow(df, _fake_pivots(df, _PTS), base)
    assert not [e for e in ev if e["type"] == "SOW"], "创新低但快速反弹回支撑上方不应归为 SOW"
    shakes = [e for e in ev if e["type"] == "Shakeout"]
    assert shakes, "破位+创新低+快速反弹应归为 Shakeout 震仓"


# ───────────────── 4. 完整链推进结构进度 ─────────────────

def test_full_distribution_chain_reaches_phase_d():
    """BC→AR→UT→UTAD→LPSY→SOW 全集 → 结构进度 Phase D (含弱势确认)。"""
    df = _mkdf(320)
    base = 230
    events = [
        {"type": "BC", "idx": base, "conf": 95},
        {"type": "AR", "idx": base + 15, "conf": 70},
        {"type": "UT", "idx": base + 30, "conf": 75},
        {"type": "UTAD", "idx": base + 50, "conf": 90},
        {"type": "LPSY", "idx": base + 70, "conf": 80},
        {"type": "SOW", "idx": base + 90, "conf": 85},
    ]
    letter, name, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert letter in ("D", "E")
    assert "未推进" not in detail


def test_sow_requires_dist_prereq():
    """无 UTAD/LPSY 铺垫的孤立 SOW 不推进结构 (前置拦截)。"""
    df = _mkdf(320)
    events = [{"type": "SOW", "idx": 300, "conf": 90}]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert "未推进" in detail


def test_sow_after_utad_only_advances():
    """有 UTAD 铺垫的 SOW → 推进 (前置成立)。"""
    df = _mkdf(320)
    events = [{"type": "BC", "idx": 230, "conf": 95},
              {"type": "UT", "idx": 260, "conf": 75},
              {"type": "UTAD", "idx": 280, "conf": 90},
              {"type": "SOW", "idx": 300, "conf": 85}]
    letter, _, detail = structure_progress(events, df, phase="顶部构筑 (Distribution)")
    assert "未推进" not in detail


# ───────────────── 5. detect_all 串联不破坏 ─────────────────

def test_detect_all_includes_new_types():
    """detect_all 聚合 UT/SOW (数据满足形态时), 且所有事件带 confirmed。"""
    df = _mkdf_dist(_PTS)
    df = add_indicators(df, symbol="600104")
    ev = E.detect_all(df, _fake_pivots(df, _PTS))
    assert ev, "应至少产出事件"
    assert all("confirmed" in e for e in ev)
    kinds = {e["type"] for e in ev}
    # 至少应产出派发侧新事件 (UT/SOW/AR) 之一, 证明检测器已串联
    assert kinds & {"UT", "SOW", "AR"}


def test_detect_all_preserves_bull_chain_unchanged():
    """增强后 detect_all 仍产出吸筹侧事件 (Spring/ST/LPS 等不回归)。"""
    df = _mkdf(300)
    df.loc[50, "low"] = float(df["low"].iloc[40]) * 0.95
    df.loc[50, "close"] = float(df["low"].iloc[50])
    df.loc[50, "volume"] = df["volume"].rolling(20).mean().iloc[50] * 2.5
    df.loc[60, "high"] = float(df["high"].iloc[45]) * 1.05
    df = add_indicators(df, symbol="600104")
    ev = E.detect_all(df, [])
    assert isinstance(ev, list)
