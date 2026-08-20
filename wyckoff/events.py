# -*- coding: utf-8 -*-
"""威科夫事件检测与置信度打分。"""
import numpy as np
import pandas as pd

from .config import EVENT_COLORS, event_dir

# 置信度实证校准: 将 bar 级结构分(量/幅/位置/趋势/共振)与同类型信号的
# 历史实证方向可靠性混合。实测结构分存在系统性偏差——强动量类信号
# (BC/JOC/SOS) 结构分偏高但命中率贴近随机, 反转/极点类信号 (Spring/ST)
# 结构分偏低但胜率显著高于随机。混合后高置信档恢复正收益区分度。
USE_EMPIRICAL_CONF = True
EMPIRICAL_CONF_BLEND = 0.55  # 实证可靠性权重 (结构分占 0.45)


def _empirical_reliability(type_, d=0):
    """某类型信号的方向可靠性(0~1):
    直接取 win_rate_of 的方向化命中占比 (多头涨记中、空头跌记中 —— 信号本身
    已含方向, 不再对空头做 1-win 反转, 否则会把"下跌命中"再倒过来);
    中性/样本不足 → 0.5 (回退纯结构分)。
    样本不足由 win_rate_of 的 min_n=10 基线回退保证。
    惰性导入避免 events↔signal_accuracy 循环引用 (与 fusion 同模式)。"""
    if not USE_EMPIRICAL_CONF or d == 0:
        return 0.5
    try:
        from .signal_accuracy import win_rate_of
        return win_rate_of("event", type_, horizon=20, baseline=0.5)
    except Exception:
        return 0.5


def _apply_empirical_calibration(events):
    """把事件 conf 向该类型实证方向可靠性混合, 修复结构分方向偏差。"""
    for e in events:
        if not USE_EMPIRICAL_CONF:
            return
        conf = e.get("conf")
        if not isinstance(conf, (int, float)):
            continue
        if conf <= 5:
            continue  # 涨停/一字板等硬性低置信档保持不动
        rel = _empirical_reliability(e.get("type", ""), event_dir(e.get("type", "")))
        e["conf"] = int(round(min(100, max(0,
            conf * (1.0 - EMPIRICAL_CONF_BLEND) + rel * 100 * EMPIRICAL_CONF_BLEND))))
    return events

def _dedup(events, span: int = 8):
    """同类型事件在 span 天内只保留第一个"""
    out = []
    for e in sorted(events, key=lambda x: x["idx"]):
        if not out or e["type"] != out[-1]["type"] or e["idx"] - out[-1]["idx"] > span:
            out.append(e)
    return out


def detect_climaxes(df: pd.DataFrame):
    """SC 卖出高潮 / BC 买入高潮: 放量 + 极端价位 + 长影线"""
    close = df["close"]
    cvals = close.values
    lo40 = close.rolling(41, min_periods=1).min().values
    hi40 = close.rolling(41, min_periods=1).max().values
    span = np.where(hi40 - lo40 > 1e-9, hi40 - lo40, 1e-9)
    rng = np.where(df["range"].values > 1e-9, df["range"].values, 1e-9)
    lo_zone = (cvals - lo40) / span < 0.15
    hi_zone = (hi40 - cvals) / span < 0.15
    vol_ok = df["vol_ratio_20"].values >= 1.6
    vol_hi = df["vol_ratio_20"].values >= 2.0
    sc_cond = vol_ok & lo_zone & (df["lower_wick"].values / rng > 0.30)
    hi_zone_narrow = (hi40 - cvals) / span < 0.10
    bc_cond = vol_hi & hi_zone_narrow & (df["upper_wick"].values / rng > 0.30)
    days = df["day"].values
    lows = df["low"].values
    highs = df["high"].values
    vr20 = df["vol_ratio_20"].values
    events = []
    for i in np.where(sc_cond | bc_cond)[0]:
        if i < 20:
            continue
        i = int(i)
        if sc_cond[i]:
            events.append(dict(type="SC", idx=i, date=pd.Timestamp(days[i]), price=float(lows[i]),
                               desc=f"卖出高潮 vol={vr20[i]:.1f}x",
                               color=EVENT_COLORS["SC"]))
        if bc_cond[i]:
            events.append(dict(type="BC", idx=i, date=pd.Timestamp(days[i]), price=float(highs[i]),
                               desc=f"买入高潮 vol={vr20[i]:.1f}x",
                               color=EVENT_COLORS["BC"]))
    return _dedup(events)


def detect_pivot_events(df: pd.DataFrame, pivots, climax_events=None):
    """基于枢轴点的 Spring / UTAD / SOS (需吸筹背景)"""
    if climax_events is None:
        climax_events = []
    events = []
    n = len(df)
    lows = [p for p in pivots if p["type"] == "low"]
    highs = [p for p in pivots if p["type"] == "high"]
    closes = df["close"].values
    volume = df["volume"].values
    vol_ma20 = df["vol_ma20"].values

    # Spring: 刺破前低后收回
    for k in range(1, len(lows)):
        prev, cur = lows[k - 1], lows[k]
        if cur["price"] < prev["price"] * 0.98:
            a, b = cur["idx"], min(cur["idx"] + 20, n)
            if b > a and (closes[a:b] > prev["price"]).any():
                events.append(dict(type="Spring", idx=cur["idx"], date=cur["date"],
                                   price=cur["price"], desc=f"刺破{prev['price']:.2f}后收回",
                                   color=EVENT_COLORS["Spring"]))
    # UTAD: 冲高突破前高后回落
    for k in range(1, len(highs)):
        prev, cur = highs[k - 1], highs[k]
        if cur["price"] > prev["price"] * 1.02:
            a, b = cur["idx"], min(cur["idx"] + 20, n)
            if b > a and (closes[a:b] < prev["price"]).any():
                events.append(dict(type="UTAD", idx=cur["idx"], date=cur["date"],
                                   price=cur["price"], desc=f"冲高{cur['price']:.2f}后回落",
                                   color=EVENT_COLORS["UTAD"]))
    # SOS: 放量上破前枢轴高点 + 吸筹背景 (实测: 无吸筹背景的纯趋势SOS命中率仅31%,
    # 有 SC/Spring/ST 背景时 46%, 故不再用"均线转多"作为唯一依据)
    for k in range(1, len(highs)):
        prev, cur = highs[k - 1], highs[k]
        if not (cur["price"] > prev["price"] and cur["idx"] - prev["idx"] <= 30):
            continue
        a, b = prev["idx"], cur["idx"] + 1
        vr = volume[a:b].mean() / max(vol_ma20[cur["idx"]], 1e-9)
        if vr <= 1.3:
            continue
        i = int(cur["idx"])
        accum_sources = climax_events + events
        has_accum = any(e["type"] in ("SC", "Spring", "ST") and e["idx"] < i and i - e["idx"] <= 60
                        for e in accum_sources)
        if not has_accum:
            continue
        events.append(dict(type="SOS", idx=i, date=cur["date"],
                            price=cur["price"], desc=f"量比{vr:.1f}", color=EVENT_COLORS["SOS"]))
    return _dedup(events)


def detect_ar_st(df: pd.DataFrame, pivots, climax):
    """AR 自动反弹/自动回落 / ST 二次测试: SC 后的第一波反弹与缩量回踩。

    派发侧对称 (与 SC→AR 的吸筹侧对偶): BC 之后的第一个低点枢轴标为 AR
    (自动回落)。实测此前 BC 后 AT 从未产出, 导致派发 Phase A (BC→AR)
    在事件链中断。"""
    events = []
    lows = [p for p in pivots if p["type"] == "low"]
    highs = [p for p in pivots if p["type"] == "high"]
    volume = df["volume"].values
    for ev in climax:
        if ev["type"] == "SC":
            ar = next((h for h in highs if h["idx"] > ev["idx"]), None)
            if not ar:
                continue
            events.append(dict(type="AR", idx=ar["idx"], date=ar["date"], price=ar["price"],
                               desc="自动反弹", color=EVENT_COLORS["AR"]))
            for lo in lows:
                if lo["idx"] > ar["idx"] and lo["price"] < ev["price"] * 1.04:
                    vol_sc = volume[ev["idx"]]
                    vol_lo = volume[lo["idx"]]
                    if vol_lo < vol_sc * 0.85:
                        events.append(dict(type="ST", idx=lo["idx"], date=lo["date"], price=lo["price"],
                                           desc="缩量回踩 SC 区", color=EVENT_COLORS["ST"]))
                    break
        elif ev["type"] == "BC":
            ar = next((l for l in lows if l["idx"] > ev["idx"]), None)
            if not ar:
                continue
            events.append(dict(type="AR", idx=ar["idx"], date=ar["date"], price=ar["price"],
                               desc="自动回落", color=EVENT_COLORS["AR"]))
    return _dedup(events)


def detect_joc_lps_bu(df: pd.DataFrame, pivots, base_events):
    """JOC 跨越小溪 / SOS 突破 / LPS 最后支撑点 / BU 回撤"""
    events = []
    lows = [p for p in pivots if p["type"] == "low"]
    # JOC: 放量突破近60日震荡区间上沿 (要求区间已形成至少15根);
    #      收盘须超过区间上沿1%以上 (实测: 刚过点的弱突破假信号多, 命中率仅43%)
    # SOS: 放量突破近30日收盘高点, 仅在有吸筹背景时判定 (实测: 纯趋势SOS命中率31%)
    prev_close_30 = df["close"].rolling(30).max().shift(1).values
    prev_high_60 = df["high"].rolling(60).max().shift(1).values
    hival = df["high"].values
    vol = df["volume"].values
    cvals = df["close"].values
    vol_ma20 = df["vol_ma20"].values
    vol_ok = vol >= vol_ma20 * 1.25
    joc_vol = vol >= vol_ma20 * 1.8
    raw_joc = joc_vol & (cvals > prev_high_60 * 1.01)
    raw_sos = vol_ok & ~raw_joc & (cvals > prev_close_30)
    days = df["day"].values
    for i in np.where(raw_joc | raw_sos)[0]:
        if i < 61:
            continue
        i = int(i)
        is_joc = raw_joc[i]
        if is_joc:
            # 确认突破前存在震荡区间: 前60日高至少15根前已确立(非连续新高)
            if i < 75:
                continue
            prev_hi_indices = np.where(hival[max(0, i - 60):i - 14] >= prev_high_60[i] * 0.98)[0]
            if len(prev_hi_indices) == 0:
                continue
            events.append(dict(type="JOC", idx=i, date=pd.Timestamp(days[i]), price=hival[i],
                                desc="放量突破60日震荡区间上沿", color=EVENT_COLORS["JOC"]))
        else:
            has_accum = any(e["type"] in ("SC", "Spring", "ST")
                            and e["idx"] < i and i - e["idx"] <= 60
                            for e in base_events)
            if not has_accum:
                continue
            events.append(dict(type="SOS", idx=i, date=pd.Timestamp(days[i]), price=hival[i],
                                desc="放量突破30日收盘高点", color=EVENT_COLORS["SOS"]))
    events = _dedup(events, span=15)

    # LPS / BU: 基于 SOS/JOC 之后的缩量回踩低点。
    # base_events 是调用方传入的 (pivot_ev + climax), 不含本函数刚生成的
    # SOS/JOC —— 若用它作为"突破事件"来源, LPS/BU 永远为空 (实测11只股票
    # 近3年 0 个 LPS/BU 样本)。修复: 以本函数生成的 events 里的 SOS/JOC 为基准。
    days = df["day"].values
    lows_arr = df["low"].values
    highs_arr = df["high"].values
    volume = df["volume"].values
    vol_ma20 = df["vol_ma20"].values
    for base in events:
        if base["type"] not in ("SOS", "JOC"):
            continue
        b_idx = base["idx"]
        prior_lows = [l for l in lows if l["idx"] < b_idx]
        floor = prior_lows[-1]["price"] if prior_lows else \
            lows_arr[max(0, b_idx - 20):b_idx].min()
        range_high = highs_arr[max(0, b_idx - 60):b_idx].max()
        for lo in lows:
            if lo["idx"] > b_idx and lo["idx"] <= b_idx + 25:
                if lo["price"] > floor * 0.99 and volume[lo["idx"]] < vol_ma20[lo["idx"]]:
                    if base["type"] == "JOC" and lo["price"] >= range_high * 0.97:
                        events.append(dict(type="BU", idx=lo["idx"], date=lo["date"], price=lo["price"],
                                           desc="回撤至区间上沿", color=EVENT_COLORS["BU"]))
                    else:
                        events.append(dict(type="LPS", idx=lo["idx"], date=lo["date"], price=lo["price"],
                                           desc="缩量回踩不破", color=EVENT_COLORS["LPS"]))
                    break
    return _dedup(events)


def detect_ut(df: pd.DataFrame, pivots, base_events):
    """UT 上冲测试 (派发 Phase B, 与吸筹侧 ST 对称)。

    BC 之后价格反弹测试区间上沿/前高, 但冲高后收回区间 (收盘未能站稳前高)
    —— 需求衰减、供给再次压制的试探。UT 与 UTAD 的差别: UT 是区间内对前高的
    反复测试 (Phase B), UTAD 是刺破前高后的放量回落 (Phase C 诱多)。
    """
    events = []
    n = len(df)
    highs = [p for p in pivots if p["type"] == "high"]
    lows = [p for p in pivots if p["type"] == "low"]
    close = df["close"].values
    for bc in base_events:
        if bc["type"] != "BC":
            continue
        b_idx = bc["idx"]
        ref_high = bc["price"]
        refl = [h for h in highs if b_idx < h["idx"] <= b_idx + 25]
        if not refl:
            continue
        h = refl[0]
        # 冲高测试: 高点接近前高但收盘收回 (未站稳 BC 高点), 且未构成 UTAD 刺破形态
        if h["price"] >= ref_high * 0.97 and h["price"] <= ref_high * 1.03:
            a, b = h["idx"] + 1, min(h["idx"] + 8, n)
            if a < b and (close[a:b] < ref_high * 0.99).any():
                events.append(dict(type="UT", idx=h["idx"], date=h["date"], price=h["price"],
                                   desc="冲高测试前高后收回", color=EVENT_COLORS["UT"]))
    # 排除与 UTAD 同一根 (UTAD 需要刺破前高, 与 UT 形态互斥 — 以防重叠)
    utads = {e["idx"] for e in base_events if e["type"] == "UTAD"}
    events = [e for e in events if e["idx"] not in utads]
    return _dedup(events)


def detect_sow(df: pd.DataFrame, pivots, base_events):
    """SOW 弱势信号 (Sign of Weakness, 派发 Phase D→E 衔接)。

    UTAD/LPSY 之后放量跌破前低支撑 (LPSY 低点/区间下沿) → 派发确认、破位前奏,
    对应 config 中 DIST_PHASES Phase D "弱势信号 SOS失败 / LPS失守"。
    """
    events = []
    n = len(df)
    lows = [p for p in pivots if p["type"] == "low"]
    close = df["close"].values
    volume = df["volume"].values
    vol_ma20 = df["vol_ma20"].values
    days = df["day"].values
    for base in base_events:
        if base["type"] not in ("UTAD", "LPSY", "BC"):
            continue
        floor = base["price"]
        # 事件后 25 根内: 放量跌破前低支撑且收盘位于其下 → 弱势信号
        for lo in lows:
            if lo["idx"] <= base["idx"] or lo["idx"] > base["idx"] + 25:
                continue
            if lo["price"] < floor * 0.97 \
                    and volume[lo["idx"]] >= vol_ma20[lo["idx"]] * 1.25 \
                    and close[lo["idx"]] < floor:
                events.append(dict(type="SOW", idx=lo["idx"],
                                   date=pd.Timestamp(days[lo["idx"]]),
                                   price=lo["price"], desc="放量跌破前低支撑",
                                   color=EVENT_COLORS["SOW"]))
                break
    return _dedup(events)


def detect_lpsy(df: pd.DataFrame, pivots, base_events):
    """LPSY 最后供应点 (派发 Phase D 卖点, 与 LPS 对称)。

    LPS 是 SOS/JOC 之后缩量回踩不破前低 → 买点;
    LPSY 是 UTAD/BC 冲高回落之后, 反弹缩量且未能重新站上前高 → 卖点,
    跌破后进入 markdown (下跌趋势)。威科夫标准派发因果链:
    BC→AR→UT→UTAD→LPSY→SOW→markdown, 但软件此前只做吸筹侧 LPS,
    派发侧从无 LPSY —— 半组对称缺失。
    """
    events = []
    highs = [p for p in pivots if p["type"] == "high"]
    volume = df["volume"].values
    vol_ma20 = df["vol_ma20"].values
    days = df["day"].values
    for base in base_events:
        if base["type"] not in ("UTAD", "BC"):
            continue
        b_idx = base["idx"]
        anchor_high = base["price"]
        for hi in highs:
            if hi["idx"] <= b_idx or hi["idx"] > b_idx + 25:
                continue
            # 反弹缩量 (供方耗尽前的最后尝试) 且未站上前高 → LPSY
            if volume[hi["idx"]] < vol_ma20[hi["idx"]] and hi["price"] < anchor_high:
                events.append(dict(type="LPSY", idx=hi["idx"],
                                   date=pd.Timestamp(days[hi["idx"]]),
                                   price=hi["price"], desc="缩量反弹未过前高",
                                   color=EVENT_COLORS["LPSY"]))
                break
    return _dedup(events)


def detect_psy(df: pd.DataFrame, pivots, sc_idx) -> list:
    """PSY 初步支撑: SC 前的第一个放量止跌低点"""
    events = []
    lows = [p for p in pivots if p["type"] == "low" and p["idx"] < sc_idx]
    if not lows:
        return events
    candidate = lows[-1]
    if sc_idx - candidate["idx"] <= 40:
        events.append(dict(type="PSY", idx=candidate["idx"], date=candidate["date"],
                           price=candidate["price"], desc="初步支撑", color=EVENT_COLORS["PSY"]))
    return events


def confirm_events(df: pd.DataFrame, events, window: int = 3):
    """跟进确认: 事件后 window 根内收盘是否朝信号方向确认 (因果, 只用后续已见bar)。

    多头事件: 后续出现收盘 > 事件bar高点 (破位跟进, 如 Spring 收复 / SOS 延续);
    空头事件: 后续出现收盘 < 事件bar低点 (破位确认, 如 UTAD 后跌破);
    中性事件或数据不足(位于最末几根, 尚无后续bar) → confirmed=None (待确认)。
    返回带 confirmed 字段的新事件列表 (不改动原对象): True/False/None。
    """
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    out = []
    for e in events:
        ne = dict(e)
        i = ne.get("idx")
        d = event_dir(ne.get("type", ""))
        if d == 0 or not isinstance(i, int) or not (0 <= i < n) or i + window >= n:
            ne["confirmed"] = None
            out.append(ne)
            continue
        fut_close = close[i + 1:i + 1 + window]
        ne["confirmed"] = bool((fut_close > high[i]).any()) if d > 0 \
            else bool((fut_close < low[i]).any())
        out.append(ne)
    return out


def detect_all(df: pd.DataFrame, pivots):
    climax = detect_climaxes(df)
    pivot_ev = detect_pivot_events(df, pivots, climax)
    ar_st = detect_ar_st(df, pivots, climax)
    joc_lps = detect_joc_lps_bu(df, pivots, pivot_ev + climax)
    lpsy = detect_lpsy(df, pivots, pivot_ev + climax)
    ut = detect_ut(df, pivots, pivot_ev + climax)
    sow = detect_sow(df, pivots, pivot_ev + climax)
    scs = [e for e in climax if e["type"] == "SC"]
    psy = detect_psy(df, pivots, scs[-1]["idx"]) if scs else []
    merged = climax + pivot_ev + ar_st + joc_lps + lpsy + ut + sow + psy
    merged.sort(key=lambda x: x["idx"])
    scored = event_confidence(df, merged)
    _apply_empirical_calibration(scored)
    try:
        from .online_model import apply_model_conf
        apply_model_conf(scored)
    except Exception:
        pass  # 模型不可用/未达门槛时静默回退经验校准
    return confirm_events(df, scored)

def event_confidence(df: pd.DataFrame, events):
    """给每个事件打置信度(0-100): 量比/波幅/收盘位置 + 趋势契合 + 突破确认 + 同向共振。
    用于过滤假信号并让结构进度只由高置信度事件推进。"""
    n = len(df)
    vol_ma = df["vol_ma20"].values
    rng_ma = df["range"].rolling(20).mean().values
    close = df["close"].values
    low = df["low"].values
    rng = df["range"].values
    volume = df["volume"].values
    ma20 = df["price_ma20"].values
    ma50 = df["price_ma50"].values
    hi60 = df["high"].rolling(60).max().values
    lo60 = df["low"].rolling(60).min().values
    # 波动率状态 (因果: 只用事件bar及之前的布林带宽分位)。
    # 研究结论: "低波动蓄势→突破更可靠, 高波动→追高风险/失败率高"——
    # 带宽压缩后的放量突破(JOC/SOS/Spring)更可信, 高波动时信号打折扣。
    bw_series = None
    if {"boll_up", "boll_dn", "boll_mid"}.issubset(df.columns):
        try:
            upv = df["boll_up"].values
            dnv = df["boll_dn"].values
            midv = df["boll_mid"].values
            with np.errstate(divide="ignore", invalid="ignore"):
                bwv = np.where(midv > 0, (upv - dnv) / midv * 100, np.nan)
            bw_series = bwv
        except Exception:
            bw_series = None
    # L4 特征: 收盘在60日区间位置 / 布林带位置 (供在线校准模型使用, 无前瞻)
    span60 = np.where(hi60 - lo60 > 1e-9, hi60 - lo60, 1e-9)
    pos60 = np.clip((close - lo60) / span60, 0.0, 1.0)
    boll_pct_series = None
    if {"boll_up", "boll_dn"}.issubset(df.columns):
        try:
            bu = df["boll_up"].values
            bd = df["boll_dn"].values
            bspan = np.where(bu - bd > 1e-9, bu - bd, 1e-9)
            boll_pct_series = np.clip((close - bd) / bspan, 0.0, 1.0)
        except Exception:
            boll_pct_series = None
    locked = df["locked"].values if "locked" in df.columns else np.zeros(n, bool)
    ev_list = list(events)
    # 同向共振计数预索引 (每方向事件 idx 有序数组), 用二分把逐事件扫描从 O(E²) 降到 O(E log E)。
    dir_idx = {1: np.array([o["idx"] for o in ev_list if event_dir(o["type"]) == 1], dtype=int),
               -1: np.array([o["idx"] for o in ev_list if event_dir(o["type"]) == -1], dtype=int)}
    for idx, e in enumerate(ev_list):
        i = e["idx"]
        if not (0 <= i < n):
            e["conf"] = 50
            continue
        # 涨跌停/一字板 bar: 无法正常成交, 信号失真 (涨停买不到、跌停卖不出,
        # 缩量不反映真实供需)。事件本身保留但置信度降至阈值以下, 不推进结构、
        # 不作为买卖依据。
        if locked[i]:
            e["conf"] = 5
            continue
        vr = volume[i] / max(vol_ma[i], 1e-9)
        rm = rng_ma[i]
        rw = rng[i] / rm if (np.isfinite(rm) and rm > 1e-9) else 1.0
        cpos = (close[i] - low[i]) / max(rng[i], 1e-9)
        score = 40
        # 成交量: ST 奖励缩量(供方枯竭), 其他事件奖励放量
        if e["type"] == "ST":
            score += min(20, max(0, (2.0 - vr)) * 15)
        else:
            score += min(30, max(0, (vr - 1.0)) * 25)
        score += min(20, max(0, (rw - 1.0)) * 20)
        if e["type"] in ("SC", "ST", "Spring", "LPS", "PSY"):
            score += min(10, max(0, (0.35 - cpos)) * 25)
        elif e["type"] in ("BC", "UTAD", "SOS", "JOC", "BU", "AR", "LPSY"):
            score += min(10, max(0, (cpos - 0.65)) * 25)
        # 趋势/均线契合 (事件当日, 因果). ST 在吸筹区常逆均线, 不罚。
        d = event_dir(e["type"])
        up_i = False
        confluent = 0
        bw_pct_val = None
        if d:
            up_i = np.isfinite(ma20[i]) and np.isfinite(ma50[i]) and ma20[i] > ma50[i] and close[i] > ma50[i]
            if e["type"] == "ST":
                score += 5 if up_i else 0
            else:
                score += 10 if (up_i == (d > 0)) else -10
            # 突破/破位确认: 多头突破60日高 / 空头跌破60日低
            if d > 0 and np.isfinite(hi60[i]) and close[i] >= hi60[i] - 1e-9:
                score += 8
            elif d < 0 and np.isfinite(lo60[i]) and close[i] <= lo60[i] + 1e-9:
                score += 8
            # 波动率状态门 (因果): 低波动蓄势中的突破更可靠 → 加分;
            # 高波动时追高风险大 → 减分。仅对方向性突破/趋势事件生效。
            if bw_series is not None and np.isfinite(bw_series[i]):
                trail = bw_series[max(0, i - 120):i + 1]
                trail = trail[np.isfinite(trail)]
                if len(trail) >= 20:
                    bw_pct_val = float((trail < bw_series[i]).mean()) * 100
                    if bw_pct_val < 30:
                        score += 6   # 低波动蓄势 → 突破可靠性提高
                    elif bw_pct_val > 70:
                        score -= 6   # 高波动 → 追高风险
            # 同向事件共振 (前后8根内) — 二分计数, 不含自身 (i 严格在窗口内且 idx 唯一)
            darr = dir_idx[d]
            lo_c = np.searchsorted(darr, i - 8)
            hi_c = np.searchsorted(darr, i + 8, side="right")
            confluent = int(hi_c - lo_c)
            if lo_c < len(darr) and darr[lo_c] == i:
                confluent -= 1
            score += min(8, confluent * 4)
        # L4 在线校准模型特征 (原始结构输入, 无前瞻; 供记录落库与模型消费)
        e["feat"] = {
            "vr": round(float(vr), 4),
            "rw": round(float(rw), 4),
            "cpos": round(float(cpos), 4),
            "trend": int(up_i),
            "pos60": round(float(pos60[i]), 4),
            "boll_pct": round(float(boll_pct_series[i]), 4)
                        if boll_pct_series is not None and np.isfinite(boll_pct_series[i])
                        else None,
            "bw_pct": round(bw_pct_val, 4) if bw_pct_val is not None else None,
            "reson": int(confluent),
            "dir": d,
        }
        e["conf"] = int(round(min(100, max(0, score))))
    return ev_list
