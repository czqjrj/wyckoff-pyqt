"""威科夫事件检测与置信度打分 - 向量化优化版。"""
import numpy as np
import pandas as pd

from .config import EVENT_COLORS, confirm_dir, event_dir

USE_EMPIRICAL_CONF = True
# 经验校准混合权重: 历史类型胜率对原 conf 的覆盖比例。
# 0.55 过高 → 原 conf=80 被压缩到 63.5, 高分桶样本量塌缩, 导致
# Spearman IC≈0 (打分与收益无关)。0.20 保留原打分排序主导权的同时
# 仍允许历史数据对小样本类型做轻度收缩。
EMPIRICAL_CONF_BLEND = 0.20


def _empirical_reliability(type_, d=0):
    """取某类型信号的历史方向命中占比 (L1 贝叶斯收缩值)。

    返回 None 表示无可用历史样本 (样本不足 / 类型未追踪), 调用方应保留原 conf。
    旧实现: d==0 (中性事件) 一律返回 0.5, 再被 0.55 权重压回中段,
    导致 SC/BC/AR 三类全部落入 60-79 区, 完全抹掉原打分的相对排序。
    """
    if not USE_EMPIRICAL_CONF:
        return None
    try:
        from .signal_accuracy import MIN_SHRUNK_N, load_win_rates
        rates = load_win_rates(20)
        rec = rates.get(("event", str(type_)))
        if not rec or rec["n"] < MIN_SHRUNK_N:
            return None  # 样本不足 → 保留原 conf, 不强行收缩
        return rec["shrunk"]
    except Exception:
        return None


# 类型实证 conf 天花板: 高分信号若类型历史命中差 (Wilson CI 上界 < 60%),
# 把 conf 封在 ci_hi 附近 — 防止弱类型 (SOS/JOC/BC/AR/PSY/BU) 被打分/模型
# 抬到 80-100 档 (实测该档弱类型命中率越低反而越高置信, 反向亏钱)。
# 强类型 (Spring ci_hi≈88, Shakeout≈88) 天花板高, 不受影响。
CONF_CI_FLOOR = 0.55   # 仅当 ci_hi < 该值才封顶 (弱类型门), 进一步压低SOS/JOC/BC/AR
CONF_CEIL_MARGIN = 0.04  # 天花板 = ci_hi + margin, 给低样本少量余量


def expected_conf(type_, d=0, kind="event"):
    """某类型可给到的最高 conf (类型实证天花板)。无样本时返回 None → 不封顶。

    弱类型 (ci_hi < CONF_CI_FLOOR) 封到 ci_hi×100; 强/中性类型不封。
    返回 [0,100] 整数上限或 None。"""
    if not USE_EMPIRICAL_CONF:
        return None
    try:
        from .signal_accuracy import load_win_rates
        rates = load_win_rates(20)
        rec = rates.get((kind, str(type_)))
        if not rec or not rec.get("ci_hi"):
            return None
        ci_hi = rec["ci_hi"]
        if ci_hi >= CONF_CI_FLOOR:
            return None  # 有实证强边缘 → 不封顶
        return int(round(min(100.0, (ci_hi + CONF_CEIL_MARGIN) * 100)))
    except Exception:
        return None


def _cap_to_ceiling(events):
    """把超过类型实证天花板的 conf 压回天花板 (弱类型高分幻觉修复)。"""
    for e in events:
        conf = e.get("conf")
        if not isinstance(conf, (int, float)) or conf <= 5:
            continue
        cap = expected_conf(e.get("type", ""), event_dir(e.get("type", "")),
                            kind=e.get("kind", "event"))
        if cap is not None and conf > cap:
            e["conf"] = cap
    return events


def _apply_empirical_calibration(events):
    for e in events:
        if not USE_EMPIRICAL_CONF:
            return
        conf = e.get("conf")
        if not isinstance(conf, (int, float)) or conf <= 5:
            continue
        rel = _empirical_reliability(e.get("type", ""), event_dir(e.get("type", "")))
        # rel=None 表示无足够历史样本 → 保留原 conf, 不做强制收缩
        # (旧版用 0.5 强制混合, 导致样本不足的类型被打到中段, IC→0)
        if rel is None:
            continue
        e["conf"] = int(round(min(100, max(0,
            conf * (1.0 - EMPIRICAL_CONF_BLEND) + rel * 100 * EMPIRICAL_CONF_BLEND))))
    return events


def _dedup(events, span: int = 8):
    """同类型事件在 span 天内只保留第一个 - 向量化版本。"""
    if not events:
        return []
    idx = np.array([e["idx"] for e in events])
    types = np.array([e["type"] for e in events])
    order = np.argsort(idx)
    idx = idx[order]
    types = types[order]
    keep = np.ones(len(idx), dtype=bool)
    for i in range(1, len(idx)):
        if types[i] == types[i - 1] and idx[i] - idx[i - 1] <= span:
            keep[i] = False
    return [events[order[i]] for i in np.where(keep)[0]]


class _EventContext:
    """预计算上下文, 避免各检测函数重复计算 rolling 与数组提取。"""
    __slots__ = ("df", "n", "close", "high", "low", "volume", "range", "vol_ma20",
                 "vol_ratio_20", "lower_wick", "upper_wick", "days",
                 "hi40", "lo40", "prev_close_30", "prev_high_60",
                 "bw_series", "pos60", "boll_pct", "locked", "hi60", "lo60",
                 "rsi_6", "kdj_d")

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.n = len(df)
        self.close = df["close"].values
        self.high = df["high"].values
        self.low = df["low"].values
        self.volume = df["volume"].values
        self.range = df["range"].values
        self.vol_ma20 = df["vol_ma20"].values
        self.vol_ratio_20 = df["vol_ratio_20"].values
        self.lower_wick = df["lower_wick"].values
        self.upper_wick = df["upper_wick"].values
        self.days = df["day"].values

        # 预计算 rolling 极值
        c = self.close
        self.lo40 = pd.Series(c).rolling(41, min_periods=1).min().values
        self.hi40 = pd.Series(c).rolling(41, min_periods=1).max().values
        self.prev_close_30 = pd.Series(c).rolling(30).max().shift(1).values
        self.prev_high_60 = pd.Series(self.high).rolling(60).max().shift(1).values
        self.hi60 = pd.Series(self.high).rolling(60).max().values
        self.lo60 = pd.Series(self.low).rolling(60).min().values

        # 布林带宽分位
        self.bw_series = None
        if {"boll_up", "boll_dn", "boll_mid"}.issubset(df.columns):
            try:
                upv = df["boll_up"].values
                dnv = df["boll_dn"].values
                midv = df["boll_mid"].values
                with np.errstate(divide="ignore", invalid="ignore"):
                    bwv = np.where(midv > 0, (upv - dnv) / midv * 100, np.nan)
                self.bw_series = bwv
            except Exception:
                pass

        # 区间位置
        span60 = np.where(self.hi60 - self.lo60 > 1e-9, self.hi60 - self.lo60, 1e-9)
        self.pos60 = np.clip((c - self.lo60) / span60, 0.0, 1.0)
        self.boll_pct = None
        if {"boll_up", "boll_dn"}.issubset(df.columns):
            try:
                bu = df["boll_up"].values
                bd = df["boll_dn"].values
                bspan = np.where(bu - bd > 1e-9, bu - bd, 1e-9)
                self.boll_pct = np.clip((c - bd) / bspan, 0.0, 1.0)
            except Exception:
                pass

        self.locked = df["locked"].values if "locked" in df.columns else np.zeros(self.n, bool)

        # RSI/KDJ (供置信度打分用)
        self.rsi_6 = df["rsi_6"].values if "rsi_6" in df.columns else None
        self.kdj_d = df["kdj_d"].values if "kdj_d" in df.columns else None


def _split_pivots(pivots):
    """一次性分离高低枢轴, 返回 (lows_array, highs_array)。"""
    lows = np.array([p for p in pivots if p["type"] == "low"], dtype=object)
    highs = np.array([p for p in pivots if p["type"] == "high"], dtype=object)
    return lows, highs


def _as_ctx(obj):
    """兼容 DataFrame / _EventContext 两种入口 (detect_all 内复用预计算上下文)。"""
    if not isinstance(obj, _EventContext):
        return _EventContext(obj)
    return obj


def detect_climaxes(ctx: _EventContext):
    """SC/BC: 放量 + 极端价位 + 长影线 - 完全向量化。"""
    ctx = _as_ctx(ctx)
    cvals = ctx.close
    rng = np.where(ctx.range > 1e-9, ctx.range, 1e-9)
    span = np.where(ctx.hi40 - ctx.lo40 > 1e-9, ctx.hi40 - ctx.lo40, 1e-9)

    lo_zone = (cvals - ctx.lo40) / span < 0.15
    vol_ok = ctx.vol_ratio_20 >= 1.6
    vol_hi = ctx.vol_ratio_20 >= 2.0

    sc_cond = vol_ok & lo_zone & (ctx.lower_wick / rng > 0.30)
    hi_zone_narrow = (ctx.hi40 - cvals) / span < 0.10
    bc_cond = vol_hi & hi_zone_narrow & (ctx.upper_wick / rng > 0.30)

    idx = np.where((sc_cond | bc_cond) & (np.arange(ctx.n) >= 20))[0]
    events = []
    for i in idx:
        if sc_cond[i]:
            events.append(dict(type="SC", idx=int(i), date=pd.Timestamp(ctx.days[i]),
                               price=float(ctx.low[i]), desc=f"卖出高潮 vol={ctx.vol_ratio_20[i]:.1f}x",
                               color=EVENT_COLORS["SC"]))
        if bc_cond[i]:
            events.append(dict(type="BC", idx=int(i), date=pd.Timestamp(ctx.days[i]),
                               price=float(ctx.high[i]), desc=f"买入高潮 vol={ctx.vol_ratio_20[i]:.1f}x",
                               color=EVENT_COLORS["BC"]))
    return _dedup(events)


def detect_pivot_events(ctx: _EventContext, pivots, climax_events):
    """Spring / UTAD / SOS - 使用预分离的枢轴数组。"""
    ctx = _as_ctx(ctx)
    lows, highs = _split_pivots(pivots)
    events = []
    n = ctx.n
    closes = ctx.close
    volume = ctx.volume
    vol_ma20 = ctx.vol_ma20

    # Spring: 刺破前低后收回
    if len(lows) > 1:
        low_idx = np.array([p["idx"] for p in lows])
        low_price = np.array([p["price"] for p in lows])
        low_date = [p["date"] for p in lows]
        for k in range(1, len(lows)):
            if low_price[k] < low_price[k - 1] * 0.98:
                a, b = low_idx[k], min(low_idx[k] + 20, n)
                if b > a and np.any(closes[a:b] > low_price[k - 1]):
                    events.append(dict(type="Spring", idx=int(low_idx[k]),
                                       date=low_date[k], price=float(low_price[k]),
                                       desc=f"刺破{low_price[k-1]:.2f}后收回",
                                       color=EVENT_COLORS["Spring"]))

    # UTAD: 冲高突破前高后回落
    if len(highs) > 1:
        high_idx = np.array([p["idx"] for p in highs])
        high_price = np.array([p["price"] for p in highs])
        high_date = [p["date"] for p in highs]
        for k in range(1, len(highs)):
            if high_price[k] > high_price[k - 1] * 1.02:
                a, b = high_idx[k], min(high_idx[k] + 20, n)
                if b > a and np.any(closes[a:b] < high_price[k - 1]):
                    events.append(dict(type="UTAD", idx=int(high_idx[k]),
                                       date=high_date[k], price=float(high_price[k]),
                                       desc=f"冲高{high_price[k]:.2f}后回落",
                                       color=EVENT_COLORS["UTAD"]))

    # SOS: 放量上破前枢轴高点 + 吸筹背景
    if len(highs) > 1:
        high_idx = np.array([p["idx"] for p in highs])
        high_price = np.array([p["price"] for p in highs])
        high_date = [p["date"] for p in highs]
        accum_idx = np.array([e["idx"] for e in climax_events + events
                              if e["type"] in ("SC", "Spring", "ST")])
        for k in range(1, len(highs)):
            if not (high_price[k] > high_price[k - 1] and high_idx[k] - high_idx[k - 1] <= 30):
                continue
            a, b = int(high_idx[k - 1]), int(high_idx[k]) + 1
            vr = volume[a:b].mean() / max(vol_ma20[int(high_idx[k])], 1e-9)
            if vr <= 1.3:
                continue
            i = int(high_idx[k])
            if len(accum_idx) and np.any((accum_idx < i) & (i - accum_idx <= 60)):
                events.append(dict(type="SOS", idx=i, date=high_date[k],
                                   price=float(high_price[k]), desc=f"量比{vr:.1f}",
                                   color=EVENT_COLORS["SOS"]))
    return _dedup(events)


def detect_ar_st(ctx: _EventContext, pivots, climax):
    """AR / ST - 向量化查找首个符合条件的枢轴。"""
    ctx = _as_ctx(ctx)
    lows, highs = _split_pivots(pivots)
    events = []
    volume = ctx.volume

    high_idx = np.array([p["idx"] for p in highs]) if len(highs) else np.array([], dtype=int)
    high_price = np.array([p["price"] for p in highs]) if len(highs) else np.array([])
    high_date = [p["date"] for p in highs] if len(highs) else []
    low_idx = np.array([p["idx"] for p in lows]) if len(lows) else np.array([], dtype=int)
    low_price = np.array([p["price"] for p in lows]) if len(lows) else np.array([])
    low_date = [p["date"] for p in lows] if len(lows) else []

    for ev in climax:
        if ev["type"] == "SC":
            # AR: SC 后第一个高点
            mask = high_idx > ev["idx"]
            if not np.any(mask):
                continue
            ar_i = np.where(mask)[0][0]
            events.append(dict(type="AR", idx=int(high_idx[ar_i]), date=high_date[ar_i],
                               price=float(high_price[ar_i]), desc="自动反弹",
                               color=EVENT_COLORS["AR"]))
            # ST: AR 后第一个低点且缩量
            mask = (low_idx > high_idx[ar_i]) & (low_price < ev["price"] * 1.04)
            if np.any(mask):
                lo_i = np.where(mask)[0][0]
                if volume[int(low_idx[lo_i])] < volume[ev["idx"]] * 0.85:
                    events.append(dict(type="ST", idx=int(low_idx[lo_i]), date=low_date[lo_i],
                                       price=float(low_price[lo_i]), desc="缩量回踩 SC 区",
                                       color=EVENT_COLORS["ST"]))
        elif ev["type"] == "BC":
            mask = low_idx > ev["idx"]
            if np.any(mask):
                ar_i = np.where(mask)[0][0]
                events.append(dict(type="AR", idx=int(low_idx[ar_i]), date=low_date[ar_i],
                                   price=float(low_price[ar_i]), desc="自动回落",
                                   color=EVENT_COLORS["AR"]))
    return _dedup(events)


def detect_joc_lps_bu(ctx: _EventContext, pivots, base_events):
    """JOC / SOS / LPS / BU - 向量化条件判断。"""
    ctx = _as_ctx(ctx)
    lows, highs = _split_pivots(pivots)
    events = []

    # 预计算数组
    cvals = ctx.close
    hival = ctx.high
    vol = ctx.volume
    vol_ma20 = ctx.vol_ma20
    days = ctx.days

    vol_ok = vol >= vol_ma20 * 1.25
    joc_vol = vol >= vol_ma20 * 1.8
    raw_joc = joc_vol & (cvals > ctx.prev_high_60 * 1.01)
    raw_sos = vol_ok & ~raw_joc & (cvals > ctx.prev_close_30)

    idx = np.where((raw_joc | raw_sos) & (np.arange(ctx.n) >= 61))[0]
    for i in idx:
        i = int(i)
        # 高位过滤: boll_pct>0.8 时多头突破信号易失败 (SOS 48% / JOC 42% 胜率)
        bp_i = ctx.boll_pct[i] if ctx.boll_pct is not None and i < len(ctx.boll_pct) else 0.5
        if bp_i > 0.8:
            continue
        if raw_joc[i]:
            if i < 75:
                continue
            if not np.any(hival[max(0, i - 60):i - 14] >= ctx.prev_high_60[i] * 0.98):
                continue
            events.append(dict(type="JOC", idx=i, date=pd.Timestamp(days[i]),
                               price=float(hival[i]), desc="放量突破60日震荡区间上沿",
                               color=EVENT_COLORS["JOC"]))
        else:
            accum_idx = np.array([e["idx"] for e in base_events
                                  if e["type"] in ("SC", "Spring", "ST")])
            if len(accum_idx) and np.any((accum_idx < i) & (i - accum_idx <= 60)):
                events.append(dict(type="SOS", idx=i, date=pd.Timestamp(days[i]),
                                   price=float(hival[i]), desc="放量突破30日收盘高点",
                                   color=EVENT_COLORS["SOS"]))

    events = _dedup(events, span=15)

    # LPS / BU
    low_idx = np.array([p["idx"] for p in lows]) if len(lows) else np.array([], dtype=int)
    low_price = np.array([p["price"] for p in lows]) if len(lows) else np.array([])
    low_date = [p["date"] for p in lows] if len(lows) else []
    high_arr = ctx.high
    low_arr = ctx.low

    for base in events:
        if base["type"] not in ("SOS", "JOC"):
            continue
        b_idx = base["idx"]
        prior_mask = low_idx < b_idx
        if np.any(prior_mask):
            floor = low_price[prior_mask][-1]
        else:
            floor = low_arr[max(0, b_idx - 20):b_idx].min()
        range_high = high_arr[max(0, b_idx - 60):b_idx].max()

        mask = (low_idx > b_idx) & (low_idx <= b_idx + 25) & \
               (low_price > floor * 0.99) & (vol[low_idx] < vol_ma20[low_idx])
        if np.any(mask):
            lo_i = np.where(mask)[0][0]
            if base["type"] == "JOC" and low_price[lo_i] >= range_high * 0.97:
                events.append(dict(type="BU", idx=int(low_idx[lo_i]), date=low_date[lo_i],
                                   price=float(low_price[lo_i]), desc="回撤至区间上沿",
                                   color=EVENT_COLORS["BU"]))
            else:
                events.append(dict(type="LPS", idx=int(low_idx[lo_i]), date=low_date[lo_i],
                                   price=float(low_price[lo_i]), desc="缩量回踩不破",
                                   color=EVENT_COLORS["LPS"]))
    return _dedup(events)


def detect_ut(ctx: _EventContext, pivots, base_events):
    """UT - 向量化。"""
    ctx = _as_ctx(ctx)
    _, highs = _split_pivots(pivots)
    events = []
    if not len(highs):
        return events

    high_idx = np.array([p["idx"] for p in highs])
    high_price = np.array([p["price"] for p in highs])
    high_date = [p["date"] for p in highs]
    close = ctx.close
    n = ctx.n

    utad_idx = {e["idx"] for e in base_events if e["type"] == "UTAD"}

    for bc in base_events:
        if bc["type"] != "BC":
            continue
        b_idx = bc["idx"]
        ref_high = bc["price"]
        mask = (high_idx > b_idx) & (high_idx <= b_idx + 25)
        if not np.any(mask):
            continue
        h_i = np.where(mask)[0][0]
        hp = high_price[h_i]
        if hp >= ref_high * 0.97 and hp <= ref_high * 1.03:
            a, b = int(high_idx[h_i]) + 1, min(int(high_idx[h_i]) + 8, n)
            if a < b and np.any(close[a:b] < ref_high * 0.99):
                idx_val = int(high_idx[h_i])
                if idx_val not in utad_idx:
                    events.append(dict(type="UT", idx=idx_val, date=high_date[h_i],
                                       price=float(hp), desc="冲高测试前高后收回",
                                       color=EVENT_COLORS["UT"]))
    return _dedup(events)


def detect_sow(ctx: _EventContext, pivots, base_events, confirm_bars=10):
    """SOW / Shakeout - 向量化。"""
    ctx = _as_ctx(ctx)
    lows, _ = _split_pivots(pivots)
    events = []
    if not len(lows):
        return events

    low_idx = np.array([p["idx"] for p in lows])
    low_price = np.array([p["price"] for p in lows])
    low_date = [p["date"] for p in lows]
    close = ctx.close
    lowv = ctx.low
    volume = ctx.volume
    vol_ma20 = ctx.vol_ma20
    n = ctx.n

    for base in base_events:
        if base["type"] not in ("UTAD", "LPSY", "BC"):
            continue
        floor = base["price"]
        mask = (low_idx > base["idx"]) & (low_idx <= base["idx"] + 25)
        if not np.any(mask):
            continue
        cand = np.where(mask)[0]
        for lo_i in cand:
            i = int(low_idx[lo_i])
            if low_price[lo_i] < floor * 0.97 and volume[i] >= vol_ma20[i] * 1.25 and close[i] < floor:
                a, b = i + 1, min(i + 1 + confirm_bars, n)
                fut_close = close[a:b] if a < b else np.array([])
                new_low = a < b and float(lowv[a:b].min()) < low_price[lo_i]
                # 快速反弹过滤: 破位后 confirm_bars 内收盘反弹回支撑上方 → 震仓/诱空
                fast_rebound = len(fut_close) > 0 and float(fut_close.max()) > floor
                ev_type = "SOW" if (new_low and not fast_rebound) else "Shakeout"
                desc = ("放量破位+持续走弱(弱势确认)" if (new_low and not fast_rebound)
                        else "放量假破位+快速反弹(震仓/诱空)")
                events.append(dict(type=ev_type, idx=i, date=pd.Timestamp(low_date[lo_i]),
                                   price=float(low_price[lo_i]), desc=desc,
                                   color=EVENT_COLORS[ev_type]))
                break
    return _dedup(events)


def detect_lpsy(ctx: _EventContext, pivots, base_events):
    """LPSY - 向量化。"""
    ctx = _as_ctx(ctx)
    _, highs = _split_pivots(pivots)
    events = []
    if not len(highs):
        return events

    high_idx = np.array([p["idx"] for p in highs])
    high_price = np.array([p["price"] for p in highs])
    high_date = [p["date"] for p in highs]
    volume = ctx.volume
    vol_ma20 = ctx.vol_ma20

    for base in base_events:
        if base["type"] not in ("UTAD", "BC"):
            continue
        b_idx = base["idx"]
        anchor = base["price"]
        mask = (high_idx > b_idx) & (high_idx <= b_idx + 25) & \
               (volume[high_idx] < vol_ma20[high_idx]) & (high_price < anchor)
        if np.any(mask):
            hi_i = np.where(mask)[0][0]
            events.append(dict(type="LPSY", idx=int(high_idx[hi_i]), date=high_date[hi_i],
                               price=float(high_price[hi_i]), desc="缩量反弹未过前高",
                               color=EVENT_COLORS["LPSY"]))
    return _dedup(events)


def detect_psy(ctx: _EventContext, pivots, sc_idx):
    """PSY - 向量化。"""
    ctx = _as_ctx(ctx)
    lows, _ = _split_pivots(pivots)
    if not len(lows):
        return []
    low_idx = np.array([p["idx"] for p in lows])
    mask = low_idx < sc_idx
    if not np.any(mask):
        return []
    cand = np.where(mask)[0][-1]
    if sc_idx - low_idx[cand] <= 40:
        return [dict(type="PSY", idx=int(low_idx[cand]), date=lows[cand]["date"],
                     price=float(lows[cand]["price"]), desc="初步支撑",
                     color=EVENT_COLORS["PSY"])]
    return []


def _is_neutral_event(e_type: str) -> bool:
    """判断是否为中立事件类型, 默认确认窗口返回 None。"""
    return e_type in ("SC", "BC", "AR")


# 动态确认窗口: 不同事件类型最佳确认期 (根数)
# 基于历史实测: Spring需要较长确认, SOS需要中等, 低置信度事件需要较短窗口
DYNAMIC_WINDOW = {
    "Spring": 8,      # 刺破后收回需更多确认
    "Shakeout": 5,    # 假破位确认相对快
    "UTAD": 4,        # 冲高后回落确认快
    "LPSY": 3,        # 缩量反弹确认快
    "SOS": 5,         # 量价突破确认
    "JOC": 5,         # 突破60日区间确认
    "BU": 4,          # 回踩区间上沿确认
    "LPS": 3,         # 缩量回踩确认
    "ST": 4,          # 缩量回踩 SC 区
    "AR": 4,          # 自动反弹/回落确认
    "BC": 4,          # 买入高潮确认
    "SC": 3,          # 卖出高潮确认 (中立类型默认 None)
    "default": 3,     # 其他事件默认窗口
}


def confirm_events(df: pd.DataFrame, events, window: int = 3):
    """跟进确认 - 向量化。为每个事件使用动态确认窗口。"""
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    if not events:
        return events

    idx = np.array([e["idx"] for e in events])
    types = np.array([e["type"] for e in events])
    dirs = np.array([confirm_dir(t) for t in types])

    out = []
    for j, e in enumerate(events):
        ne = dict(e)
        i = idx[j]
        t = types[j]
        d = dirs[j]
        # 使用动态窗口: 根据事件类型确定最佳确认期
        dyn_window = DYNAMIC_WINDOW.get(t, DYNAMIC_WINDOW["default"])
        # 中立事件类型 (SC/BC/AR) 默认确认窗口返回 None
        if _is_neutral_event(e["type"]) or d == 0 or not (0 <= i < n) or i + dyn_window >= n:
            ne["confirmed"] = None
        else:
            fut_close = close[i + 1:i + 1 + dyn_window]
            if d > 0:
                cond = fut_close > high[i]
            else:
                cond = fut_close < low[i]
            ne["confirmed"] = bool(np.any(cond))
            if ne["confirmed"]:
                # 确认后首根可交易 bar
                av = int(i + 1 + np.argmax(cond))
                ne["avail_idx"] = av
                ne["avail_date"] = str(df["day"].iloc[av])
                # 记录使用的窗口长度 (便于追踪)
                ne["confirm_window"] = dyn_window
        out.append(ne)
    return out


def detect_all(df: pd.DataFrame, pivots):
    ctx = _EventContext(df)
    climax = detect_climaxes(ctx)
    pivot_ev = detect_pivot_events(ctx, pivots, climax)
    ar_st = detect_ar_st(ctx, pivots, climax)
    joc_lps = detect_joc_lps_bu(ctx, pivots, pivot_ev + climax)
    lpsy = detect_lpsy(ctx, pivots, pivot_ev + climax)
    ut = detect_ut(ctx, pivots, pivot_ev + climax)
    sow = detect_sow(ctx, pivots, pivot_ev + climax)
    scs = [e for e in climax if e["type"] == "SC"]
    psy = detect_psy(ctx, pivots, scs[-1]["idx"]) if scs else []
    merged = climax + pivot_ev + ar_st + joc_lps + lpsy + ut + sow + psy
    merged.sort(key=lambda x: x["idx"])
    scored = event_confidence(ctx, merged)
    _apply_empirical_calibration(scored)
    try:
        from .context import enrich
        enrich(df, pivots, scored)
    except Exception:
        pass
    try:
        from .online_model import apply_model_conf
        apply_model_conf(scored)
    except Exception:
        pass
    # 类型实证天花板封顶: 防止弱类型被启发式/模型抬到高分档 (高分反向亏钱)。
    try:
        _cap_to_ceiling(scored)
    except Exception:
        pass
    return confirm_events(df, scored)


def event_confidence(ctx: _EventContext, events):
    """置信度打分 - 使用预计算上下文, 避免重复提取数组。"""
    ctx = _as_ctx(ctx)
    if not events:
        return events

    n = ctx.n
    vol_ma = ctx.vol_ma20
    rng_ma = pd.Series(ctx.range).rolling(20).mean().values
    close = ctx.close
    low = ctx.low
    rng = ctx.range
    volume = ctx.volume
    ma20 = ctx.df["price_ma20"].values
    ma50 = ctx.df["price_ma50"].values
    hi60 = ctx.hi60
    lo60 = ctx.lo60
    bw_series = ctx.bw_series
    pos60 = ctx.pos60
    boll_pct = ctx.boll_pct
    rsi_6 = ctx.rsi_6
    kdj_d = ctx.kdj_d
    locked = ctx.locked

    ev_list = list(events)
    types = np.array([e["type"] for e in ev_list])
    idx = np.array([e["idx"] for e in ev_list])
    dirs = np.array([event_dir(t) for t in types])

    rsi_val = None
    kdj_val = None
    confluent = 0

    # 预计算同向共振索引
    dir_idx = {1: np.array([i for i, d in zip(idx, dirs) if d == 1], dtype=int),
               -1: np.array([i for i, d in zip(idx, dirs) if d == -1], dtype=int)}

    for e in ev_list:
        i = e["idx"]
        if not (0 <= i < n):
            e["conf"] = 50
            continue
        if locked[i]:
            e["conf"] = 5
            continue

        vr = volume[i] / max(vol_ma[i], 1e-9)
        rm = rng_ma[i]
        rw = rng[i] / rm if (np.isfinite(rm) and rm > 1e-9) else 1.0
        cpos = (close[i] - low[i]) / max(rng[i], 1e-9)
        score = 40

        if e["type"] == "ST":
            score += min(20, max(0, (2.0 - vr)) * 15)
        else:
            score += min(30, max(0, (vr - 1.0)) * 25)
        score += min(20, max(0, (rw - 1.0)) * 20)

        if e["type"] in ("SC", "ST", "Spring", "LPS", "PSY", "Shakeout"):
            score += min(10, max(0, (0.35 - cpos)) * 25)
        elif e["type"] in ("BC", "UTAD", "SOS", "JOC", "BU", "AR", "LPSY"):
            score += min(10, max(0, (cpos - 0.65)) * 25)

        d = event_dir(e["type"])
        up_i = False
        confluent = 0
        bw_pct_val = None
        if d:
            up_i = np.isfinite(ma20[i]) and np.isfinite(ma50[i]) and ma20[i] > ma50[i] and close[i] > ma50[i]
            # trend 交互: 数据显示底部反转在非上升趋势更有效, 顶部反转在上升趋势更有效
            if e["type"] in ("Spring", "ST", "Shakeout"):
                # 底部反转: 非上升趋势加分 (Spring 83.9% vs 50%)
                score += 10 if not up_i else -5
            elif e["type"] in ("UTAD", "LPSY"):
                # 顶部反转: 上升趋势加分 (UTAD 89.7% vs 71.7%)
                score += 10 if up_i else -5
            else:
                # 趋势延续/其他: 保持原逻辑
                score += 10 if (up_i == (d > 0)) else -10
            if d > 0 and np.isfinite(hi60[i]) and close[i] >= hi60[i] - 1e-9:
                score += 8
            elif d < 0 and np.isfinite(lo60[i]) and close[i] <= lo60[i] + 1e-9:
                score += 8
            # boll_pct (布林位置) 打分: rho=-0.36, 最强预测特征
            # 多头信号: boll_pct 低 (接近下轨) → 加分; 高 → 减分
            # 空头信号: boll_pct 高 (接近上轨) → 加分; 低 → 减分
            if boll_pct is not None and np.isfinite(boll_pct[i]):
                bp = float(boll_pct[i])
                if d > 0:
                    score += min(12, max(0, (0.3 - bp)) * 40)
                    score -= min(8, max(0, (bp - 0.7)) * 27)
                elif d < 0:
                    score += min(12, max(0, (bp - 0.7)) * 40)
                    score -= min(8, max(0, (0.3 - bp)) * 27)
            # bw_pct (布林宽度百分位) 保留记录但不参与打分 (rho=+0.007, 无预测力)
            if bw_series is not None and np.isfinite(bw_series[i]):
                trail = bw_series[max(0, i - 120):i + 1]
                trail = trail[np.isfinite(trail)]
                if len(trail) >= 20:
                    bw_pct_val = float((trail < bw_series[i]).mean()) * 100
            # RSI_6 打分: 多头信号 RSI 越低越有效 (rho=-0.126, 74.8% vs 56.9%)
            rsi_val = rsi_6[i] if rsi_6 is not None and i < len(rsi_6) and np.isfinite(rsi_6[i]) else None
            if rsi_val is not None and d > 0:
                if rsi_val < 30:
                    score += 10
                elif rsi_val < 50:
                    score += 6
                elif rsi_val > 70:
                    score -= 8
            # KDJ_D 打分: 多头信号 KDJ_D 越低越有效 (rho=-0.159, 74.5% vs 63.2%)
            kdj_val = kdj_d[i] if kdj_d is not None and i < len(kdj_d) and np.isfinite(kdj_d[i]) else None
            if kdj_val is not None and d > 0:
                if kdj_val < 20:
                    score += 10
                elif kdj_val < 50:
                    score += 5
                elif kdj_val > 80:
                    score -= 6
            # 同向共振
            darr = dir_idx.get(d, np.array([], dtype=int))
            if len(darr):
                lo_c = np.searchsorted(darr, i - 8)
                hi_c = np.searchsorted(darr, i + 8, side="right")
                confluent = int(hi_c - lo_c)
                if lo_c < len(darr) and darr[lo_c] == i:
                    confluent -= 1
                score += min(8, confluent * 4)

        e["feat"] = {
            "vr": round(float(vr), 4),
            "rw": round(float(rw), 4),
            "cpos": round(float(cpos), 4),
            "trend": int(up_i),
            "pos60": round(float(pos60[i]), 4) if pos60 is not None and np.isfinite(pos60[i]) else None,
            "boll_pct": round(float(boll_pct[i]), 4) if boll_pct is not None and np.isfinite(boll_pct[i]) else None,
            "bw_pct": round(bw_pct_val, 4) if bw_pct_val is not None else None,
            "reson": int(confluent),
            "dir": d,
            "rsi_6": round(float(rsi_val), 2) if rsi_val is not None else None,
            "kdj_d": round(float(kdj_val), 2) if kdj_val is not None else None,
            # 新增特征: 非线性交互 (使用 up_i 替代未定义的 trend)
            "cpos_trend": round(float(cpos) * (1 if up_i else -1), 4),
            "vr_cpos": round(float(vr) * float(cpos), 4),
            "vr_trend": round(float(vr) * (1 if up_i else -1), 4),
        }
        # 根据新特征调整置信度得分
        cpt = e["feat"].get("cpos_trend")
        vrcp = e["feat"].get("vr_cpos")
        vrt = e["feat"].get("vr_trend")
        if cpt is not None:
            # cpos_trend: 底部反转在非上升趋势更有效, 顶部反转在上升趋势更有效
            if e["type"] in ("SC", "ST", "Spring", "LPS", "PSY", "Shakeout"):
                # cpos_trend < 0 表示卖在低位+趋势向下 (强信号)
                # 使用 up_i 判断趋势: up_i=True 表示上升趋势
                score += min(5, max(0, -cpt * 15)) if not up_i else min(5, max(0, cpt * 15)) * -1
            elif e["type"] in ("BC", "UTAD", "SOS", "JOC", "BU", "AR", "LPSY"):
                # cpos_trend > 0 表示买在高位+趋势向上 (强信号)
                score += min(5, max(0, cpt * 15)) if up_i else min(5, max(0, -cpt * 15))
        if vrcp is not None:
            # vr_cpos: 体量×收盘位置综合信号
            if d > 0:
                # 多头: 高vr_cpos (体量高+收盘靠下) → 看空倾向
                score -= min(6, max(0, (vrcp - 0.4) * 20))
            elif d < 0:
                # 空头: 低vr_cpos (体量高+收盘靠上) → 看多倾向
                score += min(6, max(0, (0.4 - vrcp) * 20))
        if vrt is not None:
            # vr_trend: 体量×趋势综合信号
            if d > 0:
                # 多头: 高vr_trend (体量大+趋势向上) → 确认多头
                score += min(5, max(0, vrt * 10)) if up_i else min(5, max(0, -vrt * 10))
            elif d < 0:
                # 空头: 高vr_trend (体量大+趋势向下) → 确认空头
                score += min(5, max(0, vrt * 10)) if not up_i else min(5, max(0, -vrt * 10))
        e["conf"] = int(round(min(100, max(0, score))))
    return ev_list
