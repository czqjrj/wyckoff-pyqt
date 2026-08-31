"""威科夫阶段判断与 K 线阶段带切分。"""
import numpy as np
import pandas as pd

from .config import (
    _PHASE_STYLE,
    ACC_RANGE_EV,
    BOTTOM_HEAD,
    BOTTOM_JMIN_BARS,
    BOTTOM_LOOK,
    BOTTOM_MIN_BARS,
    BOTTOM_REC_HI,
    BOTTOM_REC_LO,
    DIST_RANGE_EV,
    RANGE_BAND,
    RANGE_EVENT_WEIGHT,
    RANGE_MERGE_GAP,
    RANGE_MIN_BARS,
    RANGE_MIN_TOUCHES,
    RANGE_PROBE_WIN,
    RANGE_TOL,
    W_RECENT,
)


def _vol_pct_median(df, window: int = 20) -> float:
    """计算最近 window 根 K 线的收盘价波动率中位数 (%): 以中位而非均值，避免极端行情 (黑天鹅/封 ST) 污染。
    若数据不足窗口，自动收窄至可用根数。"""
    close = df["close"].values.astype(float)
    win = min(window, len(close))
    if win < 3:
        return 0.02  # 极少数据回退最小阈值
    # 逐根 True Range (最高-最低, 以及之和前收盘价的绝对差)
    tr = np.zeros(len(close))
    tr[0] = max(df["high"].iloc[0], df["close"].iloc[0]) - min(df["low"].iloc[0], df["close"].iloc[0])
    for i in range(1, len(close)):
        tr[i] = max(
            df["high"].iloc[i] - df["low"].iloc[i],
            abs(df["high"].iloc[i] - df["close"].iloc[i-1]),
            abs(df["low"].iloc[i] - df["close"].iloc[i-1])
        )
    median_close = np.median(close[-win:])
    if median_close <= 0:
        return 0.03  # 防止零价位误判
    atr_pct = np.median(tr[-win:]) / median_close * 100.0
    return float(atr_pct)


def _adapt_min_rec(vol_pct: float, base: float = 0.03, alpha: float = 0.5) -> float:
    """基于波动率自适应回升阈值: base(3%) + 波动系数。
    波动率 10% 时: 3% + 10%*0.5 = 3.5%; 波动率 40% 时: 3% + 40%*0.5 = 5%.
    上限不超过 6% (避免高波动市放宽过度)。"""
    return min(0.06, base + vol_pct * alpha)


def _bottom_turning(df, pivots, events, rtypes, low_win=60, min_rec=0.04,
                    ma_win=120):
    """下跌末段是否已现"低点防守 + 回升"的筑底迹象。

    相比旧版固定 min_rec=4%, 改用基于近期波动率的自适应阈值:
    - 高波动股 (波动率 > 30%): 阈值放宽至 ~5%, 避免把正常波动当作反转信号;
    - 低波动股 (波动率 < 10%): 阈值收窄至 ~3%, 提高敏感度;
    - 普通市 (波动率 10-30%): 介于 3-5% 之间。
    保持原有三项判断不变: 回升、低点防守、确认(事件/MA站上)。"""
    n = len(df)
    if n < 30:
        return False
    close = df["close"].values
    # 自适应波动率
    vol_pct = _vol_pct_median(df, window=low_win)
    min_rec_ad = _adapt_min_rec(vol_pct, base=0.03, alpha=0.5)
    w = df.tail(low_win)
    recent_lo = float(w["low"].min())
    if recent_lo <= 0 or close[-1] <= recent_lo * (1 + min_rec_ad):
        return False
    lows = [p for p in pivots if p["type"] == "low" and p["idx"] >= n - ma_win]
    held_low = False
    if lows:
        last_low = lows[-1]["price"]
        if last_low > 0 and close[-1] > last_low * 1.02:
            held_low = True
    acc_ev = bool(rtypes & {"Spring", "ST", "SOS", "JOC", "SC"})
    ma20 = df["price_ma20"].values
    ma20_up = np.isfinite(ma20[-1]) and np.isfinite(ma20[-8]) \
        and ma20[-1] > ma20[-8]
    above_ma20 = np.isfinite(ma20[-1]) and close[-1] > ma20[-1]
    confirm = acc_ev or above_ma20 or ma20_up
    return (held_low or above_ma20) and confirm


def flow_confirmed(df, back=5):
    """近 back 根量价资金净流入占比 > 0 才确认资金承接。

    口径: Σ(body×vol) / Σ(|body|×vol) ∈ [-1,1], 无量纲跨股票可比 (含
    放量/缩量、红绿K方向)。仅靠K线推导, 全历史可回测; 若东财主力资金流
    可用 (run_analysis 层), 另行加权叠加, 此处为纯量价口径。
    校准: 偏多阶段判定后 20 根上涨, net5>0 组 56.8% vs net5<=0 组 45.7% ——
    近期无资金流入的"底部/上升"多为下跌中继或诱多, 应降为观望。
    """
    close = df["close"].values
    o = df["open"].values
    vol = df["volume"].values
    b = close[-back:] - o[-back:]
    v = vol[-back:]
    num = float(np.sum(b * v))
    den = float(np.sum(np.abs(b) * v)) or 1.0
    return num / den > 0.0


def _bull_confirmed(df, back=8):
    """多头价格确认: 收盘站上 MA20 或 MA20 斜率向上 (回调末段至少初显企稳)。
    用于收紧"底部整固"判定 —— 结构低点 + 事件, 若价格仍深陷下跌 (价在 MA20
    下方且均线向下), 过早标多只是抄底赌博。"""
    ma20 = df["price_ma20"].values
    close = df["close"].values
    if not np.isfinite(ma20[-1]):
        return False
    if close[-1] > ma20[-1]:
        return True
    if len(ma20) > back and np.isfinite(ma20[-1 - back]) \
            and ma20[-1] > ma20[-1 - back]:
        return True
    return False


def _top_turning(df, events, rtypes, hi_win=60, min_dn=0.04, ma_win=120):
    """上升趋势末段是否已现"冲高回落 + 破位"的见顶迹象。

    相比旧版固定 min_dn=4%, 改用基于近期波动率的自适应阈值:
    - 高波动股 (波动率 > 30%): 阈值放宽至 ~5%, 避免把正常回调当作见顶;
    - 低波动股 (波动率 < 10%): 阈值收窄至 ~3%, 提高敏感度;
    - 普通市 (波动率 10-30%): 介于 3-5% 之间。
    保持原有三项判断不变: 回落、破位 MA20、确认(事件/MA斜率向下)。"""
    n = len(df)
    if n < 30:
        return False
    close = df["close"].values
    # 自适应波动率 (复用 _vol_pct_median 与 _adapt_min_rec)
    vol_pct = _vol_pct_median(df, window=hi_win)
    min_dn_ad = _adapt_min_rec(vol_pct, base=0.03, alpha=0.5)
    w = df.tail(hi_win)
    recent_hi = float(w["high"].max())
    if recent_hi <= 0 or close[-1] >= recent_hi * (1 - min_dn_ad):
        return False
    ma20 = df["price_ma20"].values
    if not (np.isfinite(ma20[-1]) and close[-1] < ma20[-1]):
        return False
    dist_ev = bool(rtypes & {"UTAD", "BC", "LPSY", "SOW"})
    ma20_dn = np.isfinite(ma20[-1]) and np.isfinite(ma20[-8]) \
        and ma20[-1] < ma20[-8]
    return dist_ev or ma20_dn


def judge_phase(df: pd.DataFrame, pivots, events):
    recent_lows = [p for p in pivots[-6:] if p["type"] == "low"]
    recent_highs = [p for p in pivots[-6:] if p["type"] == "high"]
    close = df["close"].values
    ma20 = df["price_ma20"].values
    ma50 = df["price_ma50"].values
    n = len(df)
    recent_cutoff = df["day"].iloc[max(0, len(df) - W_RECENT)]
    recent_ev = [e for e in events if e["date"] >= recent_cutoff]
    rtypes = {e["type"] for e in recent_ev}
    phase, detail = "待定", ""
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        h_last, h_prev = recent_highs[-1]["price"], recent_highs[-2]["price"]
        l_last, l_prev = recent_lows[-1]["price"], recent_lows[-2]["price"]
        hh_down = h_last < h_prev * 0.98
        ll_down = l_last < l_prev * 0.98
        hh_up = h_last > h_prev * 1.02
        ll_up = l_last > l_prev * 1.02
        # 价格在近期区间的位置: 高位→拉升/派发语境, 低位→下跌/筑底语境。
        # "高点下移+低点有支撑"在低位(长期下跌末段)是收敛筑底, 非高位派发。
        ctx = df.tail(min(120, n))
        ctx_hi = float(ctx["high"].max())
        ctx_lo = float(ctx["low"].min())
        pos = (close[-1] - ctx_lo) / (ctx_hi - ctx_lo) if ctx_hi > ctx_lo else 0.5
        if hh_down and ll_down:
            # 下跌趋势末段: 若近期已现"低点防守 + 回升"的筑底迹象, 不直接标下跌
            # (校准: 标"下跌"后 20 根上涨占 74%, 多数实为底部, 需在回升确认后
            # 尽快转多)。筑底要求: 自近期低点回升 ≥4% 且守住最近枢轴低点,
            # 并辅以吸筹事件/站上MA20之一确认。
            if _bottom_turning(df, pivots, events, rtypes):
                phase = "底部整固 (Accumulation)"
                detail = "高、低点此前同步下移, 但近期低点防守 + 回升确认 → 收敛筑底迹象"
            else:
                phase, detail = "下跌趋势 (Markdown)", "高、低点同步下移 → 派发/再派发"
        elif hh_up and ll_up:
            # 上升趋势末段: 若近期已现"冲高回落 + 跌破MA20"的见顶迹象, 不直接
            # 标上升 (校准: 标"拉升"后 20 根下跌占 71%, 多数实为顶部派发)。
            if _top_turning(df, events, rtypes):
                phase = "顶部构筑 (Distribution)"
                detail = "高、低点此前同步上移, 但近期冲高回落 + 破位确认 → 见顶派发迹象"
            elif np.isfinite(ma20[-1]) and np.isfinite(ma20[-8]) \
                    and close[-1] < ma20[-1] and ma20[-1] < ma20[-8]:
                # 高点低点仍上移但价格跌破 MA20 且 MA20 已拐头向下: 上升结构被
                # 回调动摇, 方向待定 (校准: 跌破MA20时仍标"上升趋势"后 20 根
                # 上涨仅 35%, 转中性更诚实)。仅轻微回踩 (MA20 仍上行) 不降级。
                phase, detail = "区间整理", "高、低点同步上移但价格跌破MA20且均线拐头 → 回调动摇上升结构, 待收复确认"
            else:
                phase, detail = "上升趋势 (Markup)", "高、低点同步上移 → 积累后拉升"
        elif ll_up and not hh_down:
            if pos >= 0.65:
                # 低点上移 + 高位滞涨: 实测该形态后20根上涨占比≈基准 (非看空),
                # 轻易标"顶部构筑"会给出过早的减仓/卖空建议 (校准: 实盘反馈中
                # 顶部构筑后继续上涨占比高), 改为中性区间整理, 待 UTAD/BC 或
                # 有效跌破确认后再转空。
                phase, detail = "区间整理", "低点上移+高位滞涨, 高点未破 → 方向待定, 不急于判顶"
            elif rtypes & {"Spring", "ST", "SOS", "JOC"}:
                if _bull_confirmed(df) and flow_confirmed(df):
                    phase, detail = "底部整固 (Accumulation)", "低点上移 + 吸筹事件确认 → 吸筹迹象"
                else:
                    # 吸筹事件出现但价格仍深陷 MA20 下方且均线向下, 或近期资金
                    # 净流出 (net5<=0): 事件可能是下降中继的反弹, 过早标多危险
                    # (校准: 底部整固标多命中率仅33%; net5<=0 组上涨仅45.7%),
                    # 降为中性等待价格/资金确认。
                    phase, detail = "区间整理", "低点上移 + 吸筹事件, 但价在MA20下方或无资金流入 → 待确认"
            else:
                # 低点上移但无吸筹事件: 多为下降中继, 过早抄底危险 (校准: 底部整固
                # 标多命中率仅33%, 多数其后继续下跌)。
                phase, detail = "区间整理", "低点上移但无吸筹事件确认 → 待 Spring/ST/突破确认"
        elif hh_down and not ll_down:
            if pos < 0.35:
                # 价处区间低位: 下跌末段收敛, 需吸筹事件 (Spring/ST/SOS/JOC) 确认,
                # 否则多数仍是下跌中继, 直接标"底部整固"会给出过早抄底建议。
                if rtypes & {"Spring", "ST", "SOS", "JOC"}:
                    if _bull_confirmed(df) and flow_confirmed(df):
                        phase, detail = "底部整固 (Accumulation)", "高点下移+低点有支撑, 价处区间低位 → 收敛筑底(有吸筹事件确认)"
                    else:
                        # 吸筹事件但价仍在MA20下方且均线向下, 或近期资金净流出:
                        # 多为下跌中继的反弹 (校准: 底部整固标多命中率仅33%,
                        # net5<=0 组上涨仅45.7%), 降为中性等待价格/资金收复。
                        phase, detail = "区间整理", "高点下移+低位收敛+吸筹事件, 但价在MA20下方或无资金流入 → 待确认"
                else:
                    phase, detail = "区间整理", "高点下移+低位收敛, 但无吸筹事件确认 → 待 Spring/ST/突破确认"
            else:
                # 高点下移+低点有支撑且处区间高位: 可能派发, 但需派发事件 (UTAD/BC)
                # 确认; close<MA50 单独不构成顶 (校准: 2026-08 导出中无派发事件
                # 的顶部构筑 60分钟 10bar 后 6/7 上涨, 强趋势中跌破MA50多属回调)。
                if rtypes & {"UTAD", "BC"}:
                    phase, detail = "顶部构筑 (Distribution)", "高点下移 + 派发事件(UTAD/BC)确认 → 派发迹象"
                elif close[-1] < ma50[-1] and not (rtypes & {"Spring", "ST", "SOS", "JOC"}):
                    phase, detail = "顶部构筑 (Distribution)", "高点下移 + 跌破MA50 且无吸筹事件 → 派发迹象"
                else:
                    phase, detail = "区间整理", "高点下移+高位滞涨, 无派发事件确认 → 待 UTAD/BC/跌破区间确认"
        else:
            phase, detail = "区间整理", "高低点方向不一致, 待结构明确"
    elif n >= 50:
        # 枢轴不足时用均线方向后备判断
        if (np.isfinite(ma20[-1]) and np.isfinite(ma50[-1])
                and ma20[-1] > ma50[-1] and close[-1] > ma20[-1]):
            phase, detail = "上升趋势 (Markup)", "均线多头排列(枢轴不足, 均线后备)"
        elif (np.isfinite(ma20[-1]) and np.isfinite(ma50[-1])
                and ma20[-1] < ma50[-1] and close[-1] < ma20[-1]):
            phase, detail = "下跌趋势 (Markdown)", "均线空头排列(枢轴不足, 均线后备)"
        elif n >= 100 and np.isfinite(ma50[-1]):
            if close[-1] > ma50[-1]:
                phase, detail = "底部整固 (Accumulation)", "价站上MA50(枢轴不足, 均线后备)"
            else:
                phase, detail = "顶部构筑 (Distribution)", "价跌破MA50(枢轴不足, 均线后备)"
    springs = [e for e in events if e["type"] == "Spring" and e["date"] >= recent_cutoff]
    uts = [e for e in events if e["type"] == "UTAD" and e["date"] >= recent_cutoff]
    scs = [e for e in events if e["type"] == "SC" and e["date"] >= recent_cutoff]
    jocs = [e for e in events if e["type"] == "JOC" and e["date"] >= recent_cutoff]
    soss = [e for e in events if e["type"] == "SOS" and e["date"] >= recent_cutoff]
    # 事件修正: SC后出现SOS/JOC且无UTAD → 底部整固(吸筹)迹象
    if scs and (jocs or soss) and not uts:
        last_sc = scs[-1]
        if last_sc["price"] < float(df["close"].iloc[-1]):
            phase = "底部整固 (Accumulation)"
            detail = f"SC({last_sc['date'].date()})后现{'JOC' if jocs else 'SOS'} 且无UTAD → 吸筹后上攻迹象"
    # 事件细节: 根据阶段上下文给出不同解读
    if springs:
        if "Accumulation" in phase or "底部" in phase or "上升" in phase:
            detail += f"；近期 {len(springs)} 个 Spring → 底部确认, 可靠性↑"
        elif "Distribution" in phase or "顶部" in phase or "下跌" in phase:
            detail += f"；近期 {len(springs)} 个 Spring → 注意: 派发中Spring可能是假信号(UTAD形态)"
        else:
            detail += f"；近期 {len(springs)} 个 Spring → 关注能否守住"
    if uts:
        if "Distribution" in phase or "顶部" in phase or "下跌" in phase:
            detail += f"；近期 {len(uts)} 个 UTAD → 派发确认, 反弹遇阻"
        elif "Accumulation" in phase or "底部" in phase:
            detail += f"；近期 {len(uts)} 个 UTAD → 吸筹中异常冲高, 关注回落"
        else:
            detail += f"；近期 {len(uts)} 个 UTAD → 反弹遇阻风险"
    if jocs:
        detail += f"；近期 {len(jocs)} 个 JOC → 突破确认"
    if scs:
        detail += f"；近期 {len(scs)} 个 SC → 恐慌抛售已现"
    return phase, detail


def _phase_key(phase: str):
    """judge_phase 的中文阶段名 → 阶段带 key。"""
    if not phase:
        return "flat"
    if "Markdown" in phase or "下跌" in phase:
        return "markdown"
    if "Accumulation" in phase or "吸筹" in phase or "底部" in phase:
        return "accumulation"
    if "Markup" in phase or "上升" in phase:
        return "markup"
    if "Distribution" in phase or "派发" in phase or "顶部" in phase:
        return "distribution"
    return "flat"


def _is_range(df, a, e, band, min_bars, min_crosses=4):
    """判断 [a,e] 是否为横向震荡区间: 振幅受带约束 + 收盘反复穿越中线。"""
    n = e - a + 1
    if n < min_bars:
        return False
    hi_arr = df["high"].values
    lo_arr = df["low"].values
    cl_arr = df["close"].values
    hi = hi_arr[a:e + 1].max()
    lo = lo_arr[a:e + 1].min()
    if hi / lo - 1 > band:
        return False
    mid = (hi + lo) / 2
    crosses = 0
    prev = None
    for c in cl_arr[a:e + 1]:
        side = 1 if c >= mid else -1
        if prev is not None and side != prev:
            crosses += 1
        prev = side
    return crosses >= min_crosses


def _detect_ranges(df, pivots, band=None, tol=None, min_bars=None, min_touches=None,
                   merge_gap=None, probe_win=None):
    """枢轴触点法区间检测。沿枢轴行走: 区间内不允许创新低/新高(容差 tol)或带宽超限
    (含原始bar高低价核对, 防下跌途中无枢轴漏检); 验收要求双侧枢轴各≥min_touches次、
    长度≥min_bars、合并时并集带宽不超限。

    刺破收回 (Spring/UTAD 假突破): 枢轴刺破前低/前高超出 tol 后, 若后续 probe_win
    根内有收盘收回参考水平, 则判定为弹簧/陷阱而不切断区间; 该刺破点仍计入触次数,
    但被排除出参照与带宽核算 (避免弹簧拉宽区间)。否则视为有效突破切断区间。"""
    band = RANGE_BAND if band is None else band
    tol = RANGE_TOL if tol is None else tol
    min_bars = RANGE_MIN_BARS if min_bars is None else min_bars
    min_touches = RANGE_MIN_TOUCHES if min_touches is None else min_touches
    merge_gap = RANGE_MERGE_GAP if merge_gap is None else merge_gap
    probe_win = RANGE_PROBE_WIN if probe_win is None else probe_win
    seq = sorted(pivots, key=lambda p: p["idx"])
    hi_arr = df["high"].values
    lo_arr = df["low"].values
    cl_arr = df["close"].values
    n0 = len(df)
    idxs = np.arange(n0)
    ranges = []
    start = None
    lows, highs = [], []
    skipped = set()
    for p in seq:
        if start is None:
            start = p["idx"]
            lows = [p] if p["type"] == "low" else []
            highs = [p] if p["type"] == "high" else []
            continue
        lo_ref = min((l["price"] for l in lows if l["idx"] not in skipped),
                     default=None)
        hi_ref = max((h["price"] for h in highs if h["idx"] not in skipped),
                     default=None)
        broke = False
        # 刺破参考 → 先判断是否为假突破(后续收盘收回参考即吞并, 不切断区间;
        # 刺破点仍计入触次数但被排除出参照与带宽核算)
        if p["type"] == "low" and lo_ref is not None and p["price"] < lo_ref * (1 - tol):
            w1 = min(n0, p["idx"] + probe_win + 1)
            if w1 > p["idx"] + 1 and bool((cl_arr[p["idx"] + 1:w1] > lo_ref).any()):
                skipped.add(p["idx"])
                lows.append(p)
                continue
            broke = True
        if p["type"] == "high" and hi_ref is not None and p["price"] > hi_ref * (1 + tol):
            w1 = min(n0, p["idx"] + probe_win + 1)
            if w1 > p["idx"] + 1 and bool((cl_arr[p["idx"] + 1:w1] < hi_ref).any()):
                skipped.add(p["idx"])
                highs.append(p)
                continue
            broke = True
        if not broke:
            if lo_ref is not None and hi_ref is not None and lo_ref > 0 \
                    and hi_ref / lo_ref - 1 > band:
                broke = True
            sel = ~np.isin(idxs[start:p["idx"] + 1], list(skipped))
            sub = idxs[start:p["idx"] + 1][sel]
            if sub.size:
                lo_min = float(lo_arr[sub].min())
                hi_max = float(hi_arr[sub].max())
                if lo_min > 0 and hi_max / lo_min - 1 > band:
                    broke = True
        if broke:
            if len(lows) >= min_touches and len(highs) >= min_touches \
                    and p["idx"] - start >= min_bars:
                ranges.append([start, p["idx"] - 1])
            start = p["idx"]
            lows = [p] if p["type"] == "low" else []
            highs = [p] if p["type"] == "high" else []
            skipped = set()
        else:
            (lows if p["type"] == "low" else highs).append(p)
    if start is not None and len(lows) >= min_touches and len(highs) >= min_touches \
            and seq[-1]["idx"] - start >= min_bars:
        ranges.append([start, seq[-1]["idx"]])
    # 合并相邻区间: 仅当并集带宽仍不超限
    merged = []
    for r in ranges:
        if merged and r[0] - merged[-1][1] <= merge_gap:
            a0, e0 = merged[-1]
            u_hi = max(hi_arr[a0:e0 + 1].max(), hi_arr[r[0]:r[1] + 1].max())
            u_lo = min(lo_arr[a0:e0 + 1].min(), lo_arr[r[0]:r[1] + 1].min())
            if u_hi / u_lo - 1 <= band:
                merged[-1][1] = r[1]
                continue
        merged.append(r)
    return [(a, e, float(hi_arr[a:e + 1].max()), float(lo_arr[a:e + 1].min()))
            for a, e in merged if _is_range(df, a, e, band, min_bars)]


def _validate_phase(df, s, e, k, look=120, break_pct=0.05):
    """阶段带一致性校验: 吸筹/派发必须满足各自结构定义, 否则改写为趋势段。

    - 吸筹: 低点防守 (后段低点不得比前段低点低 >3%), 且不得是"双边抬升"的
      上行结构 (后段低点/高点同时高出前段 >2% → 拉升); 且低点向前防守
      (带后 look 根内有效跌破带低且不收复 → 基底失败, 非吸筹)。
    - 派发: 高点封顶 (后段高点高出前段 >3% → 突破续涨), 低点失守
      (>3% 下移 → 下跌), 低点抬升且净涨 → 上行结构; 且高点向前突破
      (带后 look 根内有效突破带高且不回撤 → 续涨, 非派发)。
    - 净变动超出 ±10% 一律改写为趋势段。
    - 带宽 >50% 的吸筹/派发段视为过宽 (含多段趋势), 改写为趋势段。
    """
    if k not in ("accumulation", "distribution"):
        return k
    close = df["close"].values
    lo = df["low"].values
    hi = df["high"].values
    n = len(df)
    net = close[e] / close[s] - 1
    mid = (s + e) // 2
    lo1 = float(lo[s:mid + 1].min())
    lo2 = lo[mid + 1:e + 1]
    hi1 = float(hi[s:mid + 1].max())
    hi2 = hi[mid + 1:e + 1]
    if abs(net) > 0.10:
        return "markup" if net > 0 else "markdown"
    # 带宽过宽 (含多段趋势) → 改写为趋势段
    band_hi = float(hi[s:e + 1].max())
    band_lo = float(lo[s:e + 1].min())
    if band_hi / band_lo - 1 > 0.50:
        return "markup" if net >= 0 else "markdown"
    if k == "accumulation":
        # 吸筹要求低点防守: 后段低点跌破前段低点 >3% → 已跌破支撑, 非吸筹
        if lo2.size and lo2.min() < lo1 * 0.97:
            return "markdown"
        # 前向低点防守: 带后 look 根内有效跌破带低 (低点低于带低 5%) 且带末收盘
        # 未收复带低 → 基底失败, 该带实为下跌中继, 非吸筹 (校准: 反馈标注中
        # 吸筹带低点随后 -16% 失守仍标吸筹属系统性误判)。
        w_end = min(n, e + 1 + look)
        band_lo = float(lo[s:e + 1].min())
        fwd = lo[e + 1:w_end]
        if fwd.size and fwd.min() < band_lo * (1 - break_pct):
            if close[w_end - 1] < band_lo:
                return "markdown"
    else:  # distribution
        # 派发要求高点封顶: 后段高点突破前段 >3% → 突破续涨, 非派发
        if hi2.size and hi2.max() > hi1 * 1.03:
            return "markup"
        # 派发中低点明显抬升且净涨 → 实为上行结构
        if lo2.size and lo2.min() > lo1 * 1.03 and net > 0.02:
            return "markup"
        # 前向高点突破: 带后 look 根内有效突破带高 (高点高于带高 5%) 且带末收盘
        # 仍高于带高 → 续涨, 非派发。
        w_end = min(n, e + 1 + look)
        band_hi = float(hi[s:e + 1].max())
        fwd = hi[e + 1:w_end]
        if fwd.size and fwd.max() > band_hi * (1 + break_pct):
            if close[w_end - 1] > band_hi:
                return "markup"
    return k


def _range_type_by_events(events, rs, re, prior):
    """区间类型: 区间内吸筹/派发事件加权证据 + 进入方向先验按权重混合。

    事件证据强时覆盖进入方向先验 (RANGE_EVENT_WEIGHT); 无任何事件时退回先验。
    权重表见 config.ACC_RANGE_EV / DIST_RANGE_EV。"""
    if not events:
        return prior
    acc_w = sum(ACC_RANGE_EV.get(e["type"], 0)
                for e in events if rs <= e["idx"] <= re)
    dist_w = sum(DIST_RANGE_EV.get(e["type"], 0)
                 for e in events if rs <= e["idx"] <= re)
    if acc_w == 0 and dist_w == 0:
        return prior
    evidence = 0.0
    if dist_w > acc_w:
        evidence = 1.0
    elif acc_w > dist_w:
        evidence = -1.0
    prior_v = -1.0 if prior == "accumulation" else 1.0
    mix = RANGE_EVENT_WEIGHT * evidence + (1 - RANGE_EVENT_WEIGHT) * prior_v
    return "accumulation" if mix < 0 else "distribution"


def _build_phases(df, ranges, median, events=None):
    """由区间列表构建完整阶段带。区间类型由进入趋势方向先验(下跌进入→吸筹,
    上涨进入→派发; 首区间不足40根时按中线位置)与区间内事件证据按权重混合
    (事件证据缺失或为空退回纯先验), 区间间/首尾趋势段按净方向或突破区间轨判定,
    最后强制区间类型段平缓(|净变动|≤0.12)以消除标签矛盾。"""
    n = len(df)
    close = df["close"].values
    typed = []
    for rs, re, top, bottom in ranges:
        net_in = float(close[rs] / close[max(0, rs - 40)] - 1)
        if rs < 40:
            mid = (top + bottom) / 2
            typ = "accumulation" if mid < median else "distribution"
        else:
            prior = "accumulation" if net_in < 0 else "distribution"
            typ = _range_type_by_events(events, rs, re, prior)
        typed.append((rs, re, top, bottom, typ))
    phases = []
    if not typed:
        final = "markup" if close[-1] > close[0] else "markdown"
        return [(0, n - 1, final)]
    prev_end = -1
    for rs, re, top, bottom, typ in typed:
        seg_start = 0 if prev_end < 0 else prev_end + 1
        if seg_start <= rs - 1:
            net = close[rs - 1] / close[seg_start] - 1
            prior = "markup" if net >= 0 else "markdown"
            if prev_end < 0:
                if typ == "accumulation" and net > 0.1:
                    prior = "markup"
                if typ == "distribution" and net < -0.1:
                    prior = "markdown"
            phases.append((seg_start, rs - 1, prior))
        phases.append((rs, re, typ))
        prev_end = re
    if prev_end < n - 1:
        seg_start = prev_end + 1
        last = typed[-1]
        if last[4] == "accumulation":
            if close[-1] > last[2]:
                final = "markup"
            elif close[-1] < last[3]:
                final = "markdown"
            else:
                final = "accumulation"
        elif last[4] == "distribution":
            if close[-1] < last[3]:
                final = "markdown"
            elif close[-1] > last[2]:
                final = "markup"
            else:
                final = "distribution"
        else:
            final = "markup" if close[-1] > close[seg_start] else "markdown"
        phases.append((seg_start, n - 1, final))
    return [(s, e, _validate_phase(df, s, e, k)) for s, e, k in phases]


def _fix_breakout_type(df, segs, break_pct=0.05):
    """按后续突破方向修正区间类型: 只看进入方向会误判中继大区间。

    威科夫吸筹/派发可多轮出现 (吸筹→拉升→再吸筹, 派发→下跌→再派发), 故
    改写需同时满足自身结构特征:
    - 派发带之后紧邻拉升带且突破其上沿 → 可能是再吸筹/中继:
        整段基本走平 (净变动 ≥ -5%) → 平底再吸筹; 明显回落 (净变动 < -5%,
        实质是回落下跌段) → markdown, 其底部尾段交给 _mark_bottoms 标吸筹;
    - 吸筹带之后紧邻下跌带且跌破其下沿 → 可能是失败基底: 仅当高点封顶
        (不创新高) 才判派发; 高点仍在抬升则保留吸筹 (基底被打破而非派发)。
    迭代至稳定 (改写可能引发新的相邻同类型合并)。
    """
    hi = df["high"].values
    lo = df["low"].values
    cl = df["close"].values
    for _ in range(4):
        changed = False
        out = []
        for i, (a, e, k) in enumerate(segs):
            if i + 1 < len(segs):
                a2, e2, k2 = segs[i + 1]
                if k == "distribution" and k2 == "markup":
                    top = float(hi[a:e + 1].max())
                    if float(hi[a2:e2 + 1].max()) > top * (1 + break_pct):
                        if cl[e] / cl[a] - 1 >= -0.05:
                            k = "accumulation"
                        else:
                            k = "markdown"
                        changed = True
                elif k == "accumulation" and k2 == "markdown":
                    bot = float(lo[a:e + 1].min())
                    if float(lo[a2:e2 + 1].min()) < bot * (1 - break_pct):
                        mid = (a + e) // 2
                        hi1 = float(hi[a:mid + 1].max())
                        hi2 = hi[mid + 1:e + 1].max()
                        if hi2.size and hi2 <= hi1 * 1.03:
                            k = "distribution"
                            changed = True
            out.append((a, e, k))
        segs = out
        if not changed:
            break
    return segs


def _has_accum_evidence(events, a, b):
    """拐点窗口内是否存在吸筹事件证据 (SC/ST/Spring/SOS/PSY/LPS/BU)。
    无事件表(调用方未传)视为无证据。"""
    if not events:
        return False
    return any(a <= e["idx"] <= b and e["type"] in ACC_RANGE_EV for e in events)


def _bottom_structure_ok(df, pivots, m, e):
    """末段筑底的结构确认 (无事件时用): 守住最近枢轴低点, 且收盘站上 MA20
    或 MA20 斜率向上。用于"回升未达 8%"的早期筑底, 避免把下跌中继当底。"""
    close = df["close"].values
    n = len(df)
    lows = [p for p in pivots if p["type"] == "low" and m <= p["idx"] <= n]
    if lows:
        last_low = lows[-1]["price"]
        if last_low > 0 and close[e] <= last_low * 1.01:
            return False
    ma20 = df["price_ma20"].values
    if np.isfinite(ma20[e]) and close[e] > ma20[e]:
        return True
    if e >= 8 and np.isfinite(ma20[e]) and np.isfinite(ma20[e - 8]) \
            and ma20[e] > ma20[e - 8]:
        return True
    return False


def _mark_bottoms(df, phases, events=None, pivots=None, min_bars=None,
                  jmin_bars=None, rec_lo=None, rec_hi=None, look=None,
                  head=None):
    """把"低点防守 + 回升"的威科夫 Phase A 底部标为吸筹带。

    两种情形:
    - 情形A (段内筑底): 下跌段尾部自底回升 (无新低 + 回升) → 尾部 [m,e] 标吸筹;
    - 情形B (跨段拐点): 下跌段在段末触底, 回升发生在紧随的吸筹/拉升段
      (markdown→markup/accumulation) → 跨边界找底, 从底部到回升 8% 标吸筹。
    校准: 标"下跌"后 20 根上涨占 74%; "markdown→accumulation/markup" 拐点
    其后 20 根上涨占 66~84% —— 下跌带延伸到谷底才切换是最大误判源。
    标准筑底要求 回升8~30% (情形A 需吸筹事件或价格结构确认, 情形B 依赖后续
    段本身已是吸筹/拉升, 无需再等事件); 情形A 允许回升 4~8% 的早期筑底,
    条件是守住枢轴低点且站上/走平 MA20。
    """
    min_bars = BOTTOM_MIN_BARS if min_bars is None else min_bars
    jmin_bars = BOTTOM_JMIN_BARS if jmin_bars is None else jmin_bars
    rec_lo = BOTTOM_REC_LO if rec_lo is None else rec_lo
    rec_hi = BOTTOM_REC_HI if rec_hi is None else rec_hi
    look = BOTTOM_LOOK if look is None else look
    head = BOTTOM_HEAD if head is None else head
    need_events = events is not None
    lo = df["low"].values
    cl = df["close"].values
    out = []
    i = 0
    while i < len(phases):
        a, e, k = phases[i]
        if k != "markdown":
            out.append((a, e, k))
            i += 1
            continue
        m = a + int(np.argmin(lo[a:e + 1]))
        # 情形A: 所有下跌段 (不限于末段) 尾部若现"低点防守 + 回升"即切出吸筹带。
        # 校准: 标"下跌"后 20 根上涨占 74%; "markdown→accumulation" 与
        # "markdown→markup" 两种拐点共 66 例正确率仅 16~34% —— 下跌带延伸到
        # 谷底才切换, 把底部回升全算成"下跌"是最大误判源。要求先有真实下跌段
        # (底不能就在波段起点), 且回升段不长于下跌段2倍。
        if m - a >= min_bars \
                and e - m + 1 >= min_bars \
                and e - m + 1 <= (m - a) * 2 \
                and lo[m + 1:e + 1].min() >= lo[m] * 0.995:
            rec = cl[e] / cl[m] - 1
            ev_ok = not need_events or _has_accum_evidence(events, m, e)
            # 标准筑底: 回升 8~30% (需吸筹事件或结构确认);
            # 早期筑底: 回升 4~8% 且守住枢轴低点 + 站上/走平 MA20。
            if (rec_lo <= rec <= rec_hi and (ev_ok or _bottom_structure_ok(df, pivots, m, e))) \
                    or (rec_lo * 0.5 <= rec < rec_lo
                        and _bottom_structure_ok(df, pivots, m, e)):
                z = None
                for j in range(m + 1, e + 1):
                    if cl[j] >= cl[m] * (1 + rec_lo * 0.5):
                        z = j
                        break
                if m > a:
                    out.append((a, m - 1, "markdown"))
                # 回升温和(≤20%)整段算吸筹; 回升过强则只把底+初段回升算吸筹,
                # 强回升部分归拉升
                if z is None or rec <= 0.20:
                    out.append((m, e, "accumulation"))
                else:
                    out.append((m, z, "accumulation"))
                    out.append((z + 1, e, "markup"))
                i += 1
                continue
        if i + 1 < len(phases):
            a2, e2, k2 = phases[i + 1]
            if k2 in ("markup", "accumulation"):
                w0 = max(a, e - head)
                w1 = min(e2, e + look)
                if w1 - w0 + 1 >= min_bars:
                    mb = w0 + int(np.argmin(lo[w0:w1 + 1]))
                    if mb < w1 and lo[mb + 1:w1 + 1].min() >= lo[mb] * 0.995:
                        rec = cl[w1] / cl[mb] - 1
                        # 拐点确认: 回升8~30% + 无新低即为确认 —— 后续段本身是
                        # 吸筹/拉升 (价格已走出基底), 无需再等事件证据。
                        # 校准: "markdown→accumulation/markup" 拐点其后 20 根上涨
                        # 占 66~84%, 死等事件证据会漏掉绝大多数无事件的 V 型底。
                        if rec_lo <= rec <= rec_hi:
                            z = None
                            for j in range(mb + 1, w1 + 1):
                                if cl[j] >= cl[mb] * (1 + rec_lo):
                                    z = j
                                    break
                            if z is not None and z - mb + 1 >= jmin_bars:
                                if mb > a:
                                    out.append((a, mb - 1, "markdown"))
                                out.append((mb, z, "accumulation"))
                                if z + 1 <= e2:
                                    out.append((z + 1, e2, k2))
                                i += 2
                                continue
        out.append((a, e, k))
        i += 1
    return out


def phase_segments(df: pd.DataFrame, pivots, events=None, order=6, smooth=9, min_len=12,
                   current_phase=None):
    """把K线时间轴切成威科夫阶段带 (Markdown/Accumulation/Markup/Distribution)。
    枢轴触点法: 先检测横向震荡区间(双侧枢轴触次+带宽+中线穿越), 按进入方向定区间
    类型(吸筹/派发), 区间间与首尾段按净方向或突破区间轨定趋势类型(拉升/下跌)。
    返回 [(start,end,key,label)]。"""
    if df is None or len(df) < 80 or not pivots:
        return []
    ranges = _detect_ranges(df, pivots)
    segs = _build_phases(df, ranges, float(np.median(df["close"].values)), events)
    # 合并相邻同类型段后, 需对"合并后"的波段再做一致性校验 (子段各自合规不代表
    # 合并段合规), 翻转可能引发新的相邻合并, 故迭代至稳定。
    for _ in range(6):
        merged = []
        for a, e, k in segs:
            # 过滤零宽/过短波段 (如 [0,0] 单根), 避免产生无意义的阶段带
            if e - a + 1 < min_len:
                continue
            if merged and k == merged[-1][2]:
                merged[-1][1] = e
                continue
            merged.append([a, e, k])
        out = [(a, e, _validate_phase(df, a, e, k)) for a, e, k in merged]
        if out == segs:
            segs = out
            break
        segs = out
    # 区间类型与后续突破方向矛盾时改写 (进入方向只看"怎么来", 不看"往哪去"):
    # 派发带随后向上突破 → 再吸筹/中继; 吸筹带随后向下破位 → 失败基底(派发)
    segs = _fix_breakout_type(df, segs)
    # 底部标吸筹: 所有下跌→拉升拐点 (受信任, 不再校验翻转; 提供事件表时需
    # 吸筹事件证据 (SC/ST/Spring/SOS 等) 才标记)
    segs = _mark_bottoms(df, segs, events, pivots)
    # 末段近期急跌: 把"拉升/吸筹"带里最近的下跌尾巴切出来 (如冲高后崩落)
    segs = _mark_recent_decline(df, segs)
    merged = []
    for a, e, k in segs:
        if e - a + 1 < min_len:
            continue
        if merged and k == merged[-1][2]:
            merged[-1][1] = e
            continue
        merged.append([a, e, k])
    return [(a, e, k, _PHASE_STYLE[k][0]) for a, e, k in merged]


def _mark_recent_decline(df, segs, tail=20, thr=0.15, min_bars=12):
    """最后一段若是拉升/吸筹, 但近期尾巴相对高点急跌超过阈值, 则切出下跌尾段。

    整段净变动会把"冲高再崩落"平均成近零, 掩盖当前下跌 (如中芯国际日线
    176→121)。依据近期 tail 根内从高点回落幅度判定, 与判段面板口径一致。
    """
    if len(segs) < 1:
        return segs
    a, e, k = segs[-1]
    if k in ("markdown", "distribution"):
        return segs
    n = e - a + 1
    if n < min_bars * 2:
        return segs
    t0 = max(a, e - tail + 1)
    hi = df["high"].values
    lo = df["low"].values
    seg_hi = float(hi[t0:e + 1].max())
    seg_lo = float(lo[t0:e + 1].min())
    if seg_lo > seg_hi * (1 - thr):
        return segs
    m = t0 + int(np.argmax(hi[t0:e + 1]))
    if e - m + 1 < min_bars:
        return segs
    out = list(segs[:-1])
    if m > a:
        out.append((a, m - 1, k))
    out.append((m, e, "markdown"))
    return out
