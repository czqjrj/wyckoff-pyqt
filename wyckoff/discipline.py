"""三重共振纪律硬门禁 —— 统一单一数据源 (供 paper/entries 等入口复用)。

审计结论: paper.pick_candidates (实际撮合的交关口) 与 entries 入场点扫描此前
各自用一套阈值 + 一套实现判定「大盘/板块强度/资金流」三重共振, 数值近似但
硬编码在两处, 容易漂移。收敛到这里作为唯一源:
  - 阈值: 大盘 MA20 向上、板块强度 > 60 分位、资金流净流入 > 50 分位
  - 判定函数: 数据不可用一律视为门禁不满足 (fail-close), 与纪律"缺一不可"一致。

scan_adv/screener 的相位/信号加分表是不同量纲、各自独立调参的评分体系,
不并入此处 (避免强改评分影响既有行为)。本模块只管"能不能入场"的三重门禁。
"""
from __future__ import annotations

# ── 三重共振阈值 (唯一数据源) ──
# 大盘 20 日线向上 (站上 MA20 / MA20 斜率>0)
MARKET_TREND_20 = 0.0
# 板块强度百分位 > 60 分位
SECTOR_PCT_GATE = 0.60
# 资金流 20 日净额分位 > 50 分位
FUND_NET_PCT_GATE = 0.50


def market_trend_ok():
    """大盘 20 日线向上门禁: 上证收盘站上 MA20。返回 (ok, reason)。
    失败 (数据拿不到) 视为门禁不满足 → fail-close。"""
    try:
        from .market import fetch_market_env
        env = fetch_market_env()
        if not env:
            return False, "大盘环境数据不可用"
        close = env.get("close")
        ma20 = env.get("ma20")
        if close is None or ma20 is None:
            return False, "大盘MA20不可用"
        return (close > ma20), f"大盘{'站上' if close > ma20 else '未站上'}MA20({ma20:.0f})"
    except Exception:
        return False, "大盘环境获取异常"


def market_trend_slope_ok(ma20_now=None, ma20_prev=None):
    """大盘 MA20 斜率门禁 (entries 口径): slope = ma20[-1] - ma20[-2] > 0。
    返回 bool。数据缺失按 fail-close 处理已由调用方决定。"""
    if ma20_now is None or ma20_prev is None:
        return False
    return (ma20_now - ma20_prev) > MARKET_TREND_20


def sector_strength_ok(sector):
    """板块强度 > 60 分位门禁。失败/不在板块表 → fail-close。返回 (ok, reason)。"""
    try:
        from .chain import sector_strength_pct
        pct = sector_strength_pct(sector)
        if pct is None:
            return False, f"板块「{sector or '未知'}」强度不可用"
        return (pct >= SECTOR_PCT_GATE), f"板块强度{pct*100:.0f}分位"
    except Exception:
        return False, "板块强度获取异常"


def flow_net5(code):
    """近 5 日主力净流入 (元); 数据不可用返回 None。"""
    try:
        from .fundamental import fetch_main_flow
        df = fetch_main_flow(code, n=10)
        if df is None or len(df) == 0:
            return None
        return float(df["main"].tail(5).sum())
    except Exception:
        return None


def fund_net_pct(flow_df):
    """资金流净额分位 (entries 口径): 近20日主力净额 vs 近60日, tanh 缩放至 (0,1)。
    数据不足返回 None。"""
    import numpy as np
    if flow_df is None or len(flow_df) < 20:
        return None
    recent_flow = float(flow_df.tail(20)["main"].sum())
    hist = float(flow_df.tail(60)["main"].sum()) if len(flow_df) >= 60 else recent_flow
    return 0.5 + 0.5 * np.tanh(recent_flow / (abs(hist) + 1e-6))
