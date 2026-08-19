# -*- coding: utf-8 -*-
"""资金流 / 筹码分布 / 股东户数 / 大盘背景 / 供需与交易区间分析。"""
import time

import numpy as np
import pandas as pd

from .config import MIN_KLINE_BARS, SINA_HEADERS, W_RECENT
from .datasource import fetch_kline, _KLINE_LOCK
from .fundamental import _get, fetch_main_flow, holder_ratio_ok
from .indicators import add_indicators

_HOLDER_CACHE = {}
_HOLDER_TTL = 3600  # 股东户数 1小时缓存

_MARKET_CACHE = {}
_MARKET_TTL = 1800  # 大盘背景 30分钟缓存


def estimate_fund_flow(df, bars: int = 20) -> float:
    """用量价关系估算资金净流入: 红K (close>open) 计流入, 绿K 计流出。
    返回近 bars 根K线净流入(元)。volume 单位为股。"""
    body = (df["close"] - df["open"]).tail(bars)
    vol = df["volume"].tail(bars)
    return float(np.sum(body * vol))


def compute_chip_concentration(df, window: int = W_RECENT):
    """筹码分布 → 90%成本集中度 (越低越集中)、平均成本、当前价获利盘比例。
    把每根K线的成交量按价格区间均匀铺开, 汇总成筹码分布。"""
    look = df.tail(window)
    if len(look) < MIN_KLINE_BARS:
        return None
    lo_all, hi_all = float(look["low"].min()), float(look["high"].max())
    if hi_all <= lo_all:
        return None
    grid = np.linspace(lo_all, hi_all, 500)
    dist = np.zeros_like(grid)
    los = look["low"].to_numpy()
    his = look["high"].to_numpy()
    vols = look["volume"].to_numpy()
    for lo, hi, vol in zip(los, his, vols):
        if hi <= lo:
            continue
        s = np.searchsorted(grid, lo, side="left")
        e = np.searchsorted(grid, hi, side="right")
        if e > s:
            dist[s:e] += vol / (e - s)
    total = dist.sum()
    if total <= 0:
        return None
    cdf = np.cumsum(dist) / total
    p5 = float(np.interp(0.05, cdf, grid))
    p95 = float(np.interp(0.95, cdf, grid))
    if p95 + p5 <= 0:
        return None
    cur = float(df["close"].iloc[-1])
    avg_cost = float(np.dot(dist, grid) / total)
    return {
        "conc": (p95 - p5) / (p95 + p5) * 100,
        "avg_cost": avg_cost,
        "profit": float(np.interp(cur, grid, cdf) * 100),
    }


def build_chip_distribution(df, window: int = W_RECENT, bins: int = 30):
    """当前筹码堆积形态: 各价位段的筹码占比 (横向柱状图数据)。
    返回 {"prices": [...], "weights": [...], "cur": 现价, "poc": 最大堆积价位,
          "below": 现价下方筹码占比} 或 None。"""
    look = df.tail(window)
    if len(look) < MIN_KLINE_BARS:
        return None
    lo_all, hi_all = float(look["low"].min()), float(look["high"].max())
    if hi_all <= lo_all:
        return None
    edges = np.linspace(lo_all, hi_all, bins + 1)
    dist = np.zeros(bins)
    los = look["low"].to_numpy()
    his = look["high"].to_numpy()
    vols = look["volume"].to_numpy()
    for lo, hi, vol in zip(los, his, vols):
        if hi <= lo:
            continue
        s = np.searchsorted(edges, lo, side="left")
        e = np.searchsorted(edges, hi, side="right")
        if e > s:
            dist[s:e] += vol / (e - s)
    total = dist.sum()
    if total <= 0:
        return None
    weights = dist / total
    cur = float(df["close"].iloc[-1])
    below = float(weights[edges[:-1] < cur].sum())
    poc = float(edges[np.argmax(dist)] + (edges[1] - edges[0]) / 2)
    return {
        "prices": [float((edges[i] + edges[i + 1]) / 2) for i in range(bins)],
        "weights": [float(w) for w in weights],
        "cur": cur,
        "poc": poc,
        "below": below,
    }


def fetch_holder_history(code: str):
    """东方财富股东户数历史(季度), 按时间正序返回列表; 带1小时缓存。"""
    code6 = (code or "")[-6:]
    if not (len(code6) == 6 and code6.isdigit()):
        return []
    with _KLINE_LOCK:
        cached = _HOLDER_CACHE.get(code6)
        if cached and time.time() - cached[0] < _HOLDER_TTL:
            return list(cached[1])
    try:
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "reportName": "RPT_HOLDERNUM_DET", "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code6}")',
            "pageNumber": "1", "pageSize": "40",
            "sortColumns": "END_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }
        r = _get(url, params, SINA_HEADERS)
        rows = ((r.json().get("result") or {}).get("data")) or []
        out = []
        for row in rows:
            out.append({
                "end_date": str(row.get("END_DATE", ""))[:10],
                "pre_date": str(row.get("PRE_END_DATE", ""))[:10],
                "holder_num": row.get("HOLDER_NUM"),
                "pre_num": row.get("PRE_HOLDER_NUM"),
                "ratio": row.get("HOLDER_NUM_RATIO"),
            })
        out.reverse()  # 时间正序
        # 过滤失真环比: 首发上市等特殊记录 (PRE_HOLDER_NUM 过小/缺失或 ratio 缺失)
        # 会造成假性天量增幅 (如福莱特上市前17户→上市后13.9万户, 环比+819570%),
        # 环比对比无意义, 丢弃。判断复用 holder_ratio_ok 单一权威函数,
        # 保证与 fundamental.build_confirm_section / chart 展示口径一致。
        out = [s for s in out if holder_ratio_ok(s)]
    except Exception:
        return []
    with _KLINE_LOCK:
        _HOLDER_CACHE[code6] = (time.time(), out)
    return out


def build_flow_series(df, bars: int = 60):
    """逐日资金净流入序列 (元), 取最近 bars 根。"""
    body = (df["close"] - df["open"]) * df["volume"]
    n = len(df)
    lo = max(0, n - bars)
    return [{"day": df["day"].iloc[i], "flow": float(body.iloc[i])}
            for i in range(lo, n)]


def build_chips_series(df, window: int = W_RECENT, points: int = 60):
    """滚动筹码集中度/获利盘/平均成本序列 (最近 points 个采样点)。"""
    n = len(df)
    start = min(n, window)
    if n - start < 2:
        return []
    step = max(1, (n - start) // points)
    idxs = list(range(start, n, step))
    if not idxs or idxs[-1] != n - 1:
        idxs.append(n - 1)
    out = []
    for i in idxs:
        c = compute_chip_concentration(df.iloc[:i + 1], window=window)
        if c:
            out.append({"day": df["day"].iloc[i], "conc": c["conc"],
                        "profit": c["profit"], "avg_cost": c["avg_cost"]})
    return out


def build_market_labels(code: str, symbol: str, df, scale: int, confirm_enabled: bool = True):
    """汇总资金流/筹码/股东户数三项的当前值与历史序列, 供标签页图表展示。"""
    out = {}
    period = "近20日" if scale == 240 else "近20根"
    try:
        out["flow_period"] = period
        out["flow_20"] = estimate_fund_flow(df, 20)
        out["flow_5"] = estimate_fund_flow(df, 5)
        out["flow_series"] = build_flow_series(df)
        out["vol_series"] = [float(v) for v in df["volume"].tail(60)]
    except Exception:
        pass
    try:
        # 真实主力资金流 (东财, 日线口径) 优先用于图表; 失败退回估算
        # confirm_enabled=False 时不抓取, 资金流面板显示估算口径
        if scale == 240 and confirm_enabled:
            fl = fetch_main_flow(symbol, 120)
            if fl is not None and len(fl):
                out["main_flow_series"] = [
                    {"day": r.day, "main": r.main, "super": r.super,
                     "large": r.large, "mid": r.mid, "small": r.small}
                    for r in fl.itertuples()
                ]
                out["main_flow"] = float(fl.tail(20)["main"].sum())
    except Exception:
        pass
    try:
        chips = compute_chip_concentration(df)
        if chips:
            out["chips"] = chips
        out["chips_series"] = build_chips_series(df)
        out["chip_dist"] = build_chip_distribution(df)
    except Exception:
        pass
    try:
        hist = fetch_holder_history(code)
        if hist:
            out["holder_series"] = hist
            out["holder"] = hist[-1]
    except Exception:
        pass
    return out or None


def _cluster_prices(prices, tol_pct=0.02):
    """把价格相近的点聚类, 返回 [(组均价, 触及次数), ...] (按均价升序)"""
    if not prices:
        return []
    prices = sorted(prices)
    groups = [[prices[0]]]
    for p in prices[1:]:
        if p <= groups[-1][0] * (1 + tol_pct):
            groups[-1].append(p)
        else:
            groups.append([p])
    return [(sum(g) / len(g), len(g)) for g in groups]


def find_trading_range(df, pivots, window=150, min_tests=1):
    """在最近 window 根内找交易区间(TR): 被多次触及的支撑(下轨)/阻力(上轨)。
    返回 {"top","bottom","top_tests","bottom_tests"} 或 None。

    校准 (2026-08): min_tests 原默认 2, 但真实行情中支撑/阻力常各只被触及
    一次 (600104/688981 日线均返回 None), 与 P&F 列群区间口径不一致。
    改为 min_tests=1 且只需一侧 (支撑或阻力) 满足; 聚类侧/补足侧均要求贴近
    现价 (距现价 <25%), 否则视为无该侧 (688981 现价下方 30% 才有支撑,
    补出的 [91~159] 是假区间, 宁缺毋滥)。"""
    n = len(df)
    start = max(0, n - window)
    last = float(df["close"].iloc[-1])
    lows = [p["price"] for p in pivots if p["type"] == "low" and p["idx"] >= start]
    highs = [p["price"] for p in pivots if p["type"] == "high" and p["idx"] >= start]
    sup = [s for s in _cluster_prices(lows)
           if s[0] < last and s[0] > last * 0.75 and s[1] >= min_tests]
    res = [r for r in _cluster_prices(highs)
           if r[0] > last and r[0] < last * 1.25 and r[1] >= min_tests]
    if not sup and not res:
        return None
    # 同触及次数时选距现价最近的 (max/min), 避免 688981 全是单次聚类时选到
    # 最远的旧低点 91.2。触及次数多者优先, 次数相同时近者优先。
    def _pick(clusters, nearest):
        return sorted(clusters, key=lambda c: (-c[1], abs(c[0] - last)))[0]
    bottom = _pick(sup, True) if sup else None
    top = _pick(res, False) if res else None
    if not bottom or not top:
        return None
    if top[0] > bottom[0] * 1.03:
        return {"top": top[0], "bottom": bottom[0],
                "top_tests": top[1], "bottom_tests": bottom[1]}
    return None


def fetch_market_series():
    """上证指数K线 (日线, 缓存30分钟), 用于相对强度计算。失败返回 None。"""
    try:
        with _KLINE_LOCK:
            cached = _MARKET_CACHE.get("sse_df")
            if cached and time.time() - cached[0] < _MARKET_TTL:
                return cached[1]
        df = add_indicators(fetch_kline("sh000001", datalen=250, scale=240))
        with _KLINE_LOCK:
            _MARKET_CACHE["sse_df"] = (time.time(), df)
        return df
    except Exception:
        return None


def relative_strength(df, index_df=None, windows=(20, 60)):
    """相对强度: 个股各窗口涨幅 - 上证指数同窗口涨幅 (%). 数据不足返回 {}。

    按日期对齐两个序列: 个股日线可能带实时bar(最新为当日), 指数无, 直接按
    位置取末尾窗口会把两者错开一天; 交集对齐后两者窗口截止日一致, 口径公平。
    """
    if index_df is None or index_df is False:
        return {}
    out = {}
    try:
        s = df.set_index("day")["close"]
        m = index_df.set_index("day")["close"]
        idx = s.index.intersection(m.index)
        if len(idx) <= max(windows) + 1:
            return out
        s = s.loc[idx]
        m = m.loc[idx]
        sv = s.values
        mv = m.values
        for w in windows:
            if len(sv) <= w:
                continue
            sr = sv[-1] / sv[-1 - w] - 1
            mr = mv[-1] / mv[-1 - w] - 1
            out[w] = (sr - mr) * 100
    except Exception:
        pass
    return out


def relative_strength_series(df, index_df=None, window=20):
    """相对强度时序: 每个交易日该股票相对指数 window 日滚动超额涨幅 (%).

    与 relative_strength 口径一致 (按日期对齐、交集截取), 但返回整条时序而非
    仅最新值, 供技术指标页画 RS 曲线。数据不足或异常返回 None。
    返回序列与 df 等长且位置对齐 (交集起点之前填 NaN): 个股与指数交易日往往
    不完全一致 (停牌/新股/实时bar), 若只返回交集长度会被 build_ind_data 因
    len(rs) != len(df) 丢弃, RS 面板将无数据。
    """
    if index_df is None or index_df is False:
        return None
    try:
        s = df.set_index("day")["close"]
        m = index_df.set_index("day")["close"]
        idx = s.index.intersection(m.index)
        if len(idx) <= window + 1:
            return None
        s = s.loc[idx].values
        m = m.loc[idx].values
        inter = np.full(len(idx), np.nan)
        for i in range(window, len(idx)):
            inter[i] = (s[i] / s[i - window] - 1) * 100 - (m[i] / m[i - window] - 1) * 100
        pos = df["day"].isin(idx)
        out = np.full(len(df), np.nan)
        out[pos] = inter
        return out
    except Exception:
        return None


def fetch_market_env():
    """上证指数背景: 价/MA20/MA50/MA200 定牛熊震荡。失败返回 None。"""
    try:
        with _KLINE_LOCK:
            cached = _MARKET_CACHE.get("sse")
            if cached and time.time() - cached[0] < _MARKET_TTL:
                return cached[1]
        # 复用 fetch_market_series 的指数K线+指标计算 (同一份 250 根指数数据,
        # 不再重复 fetch_kline + add_indicators)。
        df = fetch_market_series()
        if df is None or len(df) < 50:
            return None
        last = float(df["close"].iloc[-1])
        ma20, ma50 = float(df["price_ma20"].iloc[-1]), float(df["price_ma50"].iloc[-1])
        m200 = df["price_ma200"].iloc[-1]
        ma200 = float(m200) if pd.notna(m200) else None
        if last > ma20 > ma50 and (ma200 is None or last > ma200):
            env, tone = "牛市环境", "bullish"
        elif last < ma20 < ma50 and (ma200 is None or last < ma200):
            env, tone = "熊市环境", "bearish"
        else:
            env, tone = "震荡环境", "neutral"
        out = {"name": "上证指数", "close": last, "ma20": ma20, "ma50": ma50,
               "ma200": ma200, "env": env, "tone": tone}
        with _KLINE_LOCK:
            _MARKET_CACHE["sse"] = (time.time(), out)
        return out
    except Exception:
        return None


def volume_profile(df, window=W_RECENT):
    """成交量分布 → POC (最大成交价位)。失败返回 None。"""
    try:
        look = df.tail(window)
        lo, hi = float(look["low"].min()), float(look["high"].max())
        if hi <= lo:
            return None
        grid = np.linspace(lo, hi, 300)
        dist = np.zeros_like(grid)
        los = look["low"].to_numpy()
        his = look["high"].to_numpy()
        vols = look["volume"].to_numpy()
        for rlo, rhi, vol in zip(los, his, vols):
            if rhi <= rlo:
                continue
            s = np.searchsorted(grid, rlo, side="left")
            e = np.searchsorted(grid, rhi, side="right")
            if e > s:
                dist[s:e] += vol / (e - s)
        return {"poc": float(grid[np.argmax(dist)]), "grid": grid, "dist": dist}
    except Exception:
        return None


def supply_demand(df, window=20):
    """近 window 根供需强度: 需求=量×收盘位置, 供给=量×(1-收盘位置)。"""
    seg = df.tail(window)
    cpos = (seg["close"] - seg["low"]) / seg["range"].replace(0, np.nan)
    cpos = cpos.fillna(0.5)
    demand = float((seg["volume"] * cpos).sum())
    supply = float((seg["volume"] * (1 - cpos)).sum())
    ratio = demand / supply if supply > 0 else float("inf")
    return {"demand": demand, "supply": supply, "ratio": ratio}


def build_sd_series(df, bars=30):
    """逐根供需序列 (量×收盘位置 分配), 供图表展示。"""
    n = len(df)
    lo = max(0, n - bars)
    out = []
    for i in range(lo, n):
        row = df.iloc[i]
        rng = row["range"] or 1e-9
        cpos = (row["close"] - row["low"]) / rng
        out.append({"day": row["day"], "demand": float(row["volume"] * cpos),
                    "supply": float(row["volume"] * (1 - cpos))})
    return out
