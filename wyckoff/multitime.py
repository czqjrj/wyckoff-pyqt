"""多周期共振: 日线 + 周线 + 月线, 并支持分钟级对照日线方向。"""
import numpy as np

from .events import detect_all
from .indicators import add_indicators, find_pivots
from .phases import phase_segments

_PHASE_CN = {
    "markdown": "下跌趋势 (Markdown)",
    "accumulation": "底部整固 (Accumulation)",
    "markup": "上升趋势 (Markup)",
    "distribution": "顶部构筑 (Distribution)",
}


def weekly_resample(df):
    """日线 → 周线 (周五收盘聚合)。"""
    w = df.set_index("day").resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"}).dropna()
    return w.reset_index()


def monthly_resample(df):
    """日线 → 月线 (月末收盘聚合)。兼容 pandas 2.2+ (M→ME)。"""
    try:
        m = df.set_index("day").resample("ME").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"}).dropna()
    except ValueError:
        m = df.set_index("day").resample("M").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"}).dropna()
    return m.reset_index()


def _current_phase(df, pivots):
    """当前阶段判定 (与日线图表同口径): 优先用 phase_segments 阶段带, 它能正确
    区分"下跌后企稳"与"高位派发" (简单 judge_phase 对稀疏周/月枢轴会把
    高点下移+低点有支撑误判为顶部构筑)。短序列(如月线)不足以切阶段带时,
    退回均线同侧/近期净方向判定。"""
    segs = phase_segments(df, pivots)
    if segs:
        return _PHASE_CN.get(segs[-1][2])
    close = float(df["close"].iloc[-1])
    ma20 = df["price_ma20"].iloc[-1] if "price_ma20" in df.columns else np.nan
    ma50 = df["price_ma50"].iloc[-1] if "price_ma50" in df.columns else np.nan
    if np.isfinite(ma20) and np.isfinite(ma50):
        if close > ma20 > ma50:
            return _PHASE_CN["markup"]
        if close < ma20 < ma50:
            return _PHASE_CN["markdown"]
    closes = df["close"].values
    if len(closes) >= 7:
        net = closes[-1] / closes[-7] - 1
        if net > 0.03:
            return _PHASE_CN["markup"]
        if net < -0.03:
            return _PHASE_CN["markdown"]
    return "区间整理"


def multi_tf_analysis(df, daily_phase=None):
    """多周期共振。

    输入 df 为日线时: 计算周线/月线阶段作长周期参照;
    输入为分钟级 df 且传入 daily_phase (日线阶段文本) 时: 用日线方向对照
    分钟级当前方向, 识别"日线下跌 / 短周期反弹"这类背离, 提示信号打折。

    返回 dict 或 None。字段:
      - weekly_phase / monthly_phase: 周/月线阶段 (日线输入时)
      - daily_phase: 传入的日线阶段 (分钟级输入时)
      - intraday_phase: 分钟级自身阶段
      - trend_divergence: "up"/"down"/"same"/None — 当前周期 vs 长周期方向
    """
    try:
        intraday_phase = _current_phase(df, find_pivots(df))
        out = {"intraday_phase": intraday_phase}
        if daily_phase is not None:
            out["daily_phase"] = daily_phase
            # 方向: 底部整固/上升趋势=偏多, 顶部构筑/下跌趋势=偏空
            cur_bull = intraday_phase.split(" ")[0] in ("底部整固", "上升趋势")
            day_bull = daily_phase.split(" ")[0] in ("底部整固", "上升趋势")
            out["trend_divergence"] = ("same" if cur_bull == day_bull
                                       else ("up" if cur_bull else "down"))
        wdf = add_indicators(weekly_resample(df))
        wpivots = find_pivots(wdf, order=3)
        wevents = detect_all(wdf, wpivots)
        wphase = _current_phase(wdf, wpivots)
        recent = [e for e in wevents if e["idx"] >= len(wdf) - 12]
        monthly_phase = None
        try:
            mdf = add_indicators(monthly_resample(df))
            if len(mdf) >= 12:
                mpivots = find_pivots(mdf, order=2)
                monthly_phase = _current_phase(mdf, mpivots)
        except Exception:
            monthly_phase = None
        out.update({"weekly_phase": wphase,
                    "weekly_events": sorted({e["type"] for e in recent}),
                    "last_weekly": float(wdf["close"].iloc[-1]),
                    "monthly_phase": monthly_phase})
        return out
    except Exception:
        return None
