"""资金流 / 筹码分布 / 股东户数 / 大盘背景 / 供需与交易区间分析。"""
import time
from threading import Lock

import numpy as np
import pandas as pd

from .config import MIN_KLINE_BARS, SINA_HEADERS, W_RECENT
from .datasource import fetch_kline
from .fundamental import _get, fetch_main_flow, holder_ratio_ok
from .indicators import add_indicators

_HOLDER_CACHE = {}
_HOLDER_TTL = 3600  # 股东户数 1小时缓存
_HOLDER_LOCK = Lock()   # 独立锁: 不与 datasource 的 K线缓存锁共用 (锁随数据走)

_MARKET_CACHE = {}
_MARKET_TTL = 1800  # 大盘背景 30分钟缓存
_MARKET_LOCK = Lock()


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
    with _HOLDER_LOCK:
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
    with _HOLDER_LOCK:
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
    补出的 [91~159] 是假区间, 宁缺毋滥)。

    口径提醒: 该函数 (K线枢轴聚类) 与 P&F 侧 TR (pnf.py 的列群 25/75 分位,
    见 _pnf_targets_at) 是两套独立定义, 数值可能不一致, 属预期行为——
    一个基于日线高低点触及次数, 一个基于点数图列群震荡区间。K线图 / 指标页
    使用本函数结果 (经 structure_lines 的 creek/ice 承接上沿/下沿)。"""
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


def boundary_events(df, tr, lookback=60):
    """破冰/冰层回测边界事件 - 向量化。用于 K线图表标注, 不进交易信号链。

    BOI (破冰): 价格放量收盘跌破区间冰线 (TR 下沿), 派发破位从试探转确认。
    BUI (冰层回测): 破冰后价格反弹回测冰线下方, 未收复冰线 (冰线成新压力)。

    返回 [{"type","idx","date","price","desc","color"}, ...] 或 []。
    """
    if not tr:
        return []
    from .config import EVENT_COLORS
    ice = float(tr["bottom"])
    close = df["close"].values
    high = df["high"].values
    vol = df["volume"].values
    vol_ma = df["vol_ma20"].values if "vol_ma20" in df else np.full(len(df), np.nan)
    n = len(df)
    start = max(5, n - lookback)
    events = []
    mode = "scan"
    for i in range(start, n):
        vm = vol_ma[i]
        if not np.isfinite(vm) or vm <= 0:
            continue
        if mode == "scan":
            # 首个放量收盘跌破冰线 → BOI
            if close[i] < ice and vol[i] >= vm * 1.25:
                events.append(dict(
                    type="BOI", idx=i, date=df["day"].iloc[i],
                    price=float(close[i]), desc="放量收盘跌破区间冰线",
                    color=EVENT_COLORS["BOI"]))
                mode = "backup"
        elif mode == "backup":
            # 破冰后反抽高点触及冰线 → BUI (冰线转为新压力, 反抽即离场位)
            if high[i] >= ice:
                events.append(dict(
                    type="BUI", idx=i, date=df["day"].iloc[i],
                    price=float(high[i]), desc="破冰后反抽触及冰线",
                    color=EVENT_COLORS["BUI"]))
                mode = "done"
    return events


def fetch_market_series():
    """上证指数K线 (日线, 缓存30分钟), 用于相对强度计算。失败返回 None。"""
    try:
        with _MARKET_LOCK:
            cached = _MARKET_CACHE.get("sse_df")
            if cached and time.time() - cached[0] < _MARKET_TTL:
                return cached[1]
        df = add_indicators(fetch_kline("sh000001", datalen=250, scale=240))
        with _MARKET_LOCK:
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
        with _MARKET_LOCK:
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
        with _MARKET_LOCK:
            _MARKET_CACHE["sse"] = (time.time(), out)
        return out
    except Exception:
        return None


_VP_HVN_MULT = 1.8    # HVN: 量显著高于同分布平均 (节点判定阈值倍数)
_VP_LVN_MULT = 0.35   # LVN: 量显著低于平均但仍有成交 (节点判定阈值倍数)
_VP_VA_FRAC = 0.70    # 价值区: POC 向外累积至总成交量的占比
_VP_MIN_BINS = 2      # HVN/LVN 带至少跨越的桶数 (滤噪)


def _node_bands(dist, grid, mask, min_bins=_VP_MIN_BINS):
    """把满足 mask 的连续价格桶合并为节点带 [{lo, hi, peak}], 短带滤除。"""
    step = (grid[1] - grid[0]) / 2
    out = []
    on = False
    start = 0
    peak = 0.0
    for i, ok in enumerate(mask):
        if ok:
            if not on:
                on = True
                start = i
                peak = float(dist[i])
            else:
                peak = max(peak, float(dist[i]))
        elif on:
            if i - start >= min_bins:
                out.append({"lo": float(grid[start]) - step,
                            "hi": float(grid[i - 1]) + step,
                            "peak": peak})
            on = False
    if on and len(grid) - start >= min_bins:
        out.append({"lo": float(grid[start]) - step,
                    "hi": float(grid[-1]) + step,
                    "peak": peak})
    return out


def _value_area(dist, grid, poc_i, frac=_VP_VA_FRAC):
    """市场轮廓价值区: 从 POC 桶向外纳入直到占比 ≥ frac, 返回 (val, vah)。"""
    total = float(dist.sum())
    if total <= 0:
        return grid[poc_i], grid[poc_i]
    order = sorted(range(len(dist)), key=lambda i: abs(i - poc_i))
    acc = float(dist[poc_i])
    vals = {poc_i}
    for i in order[1:]:
        if acc >= total * frac:
            break
        vals.add(i)
        acc += float(dist[i])
    step = (grid[1] - grid[0]) / 2
    return (float(grid[min(vals)]) - step, float(grid[max(vals)]) + step)


def volume_profile(df, window=W_RECENT):
    """量价分布 (Volume Profile) → POC / HVN / LVN / 价值区。失败返回 None。

    返回 {poc, grid, dist, avg, hvn, lvn, val, vah}:
      - poc: 成交量最大价位 (控制点)
      - hvn: 高成交量节点带 [{lo, hi, peak}] (成交显著密集的价位区)
      - lvn: 低成交量节点带 [{lo, hi, peak}] (真空/快速穿越区)
      - val/vah: 价值区下/上沿 (POC 向外累积 70% 成交量, 市场轮廓口径)
    """
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
        nonzero = dist > 0
        avg = float(np.mean(dist[nonzero])) if nonzero.any() else 0.0
        out = {"poc": float(grid[np.argmax(dist)]), "grid": grid, "dist": dist}
        if avg > 0:
            out["avg"] = avg
            out["hvn"] = _node_bands(dist, grid, dist >= avg * _VP_HVN_MULT)
            out["lvn"] = _node_bands(dist, grid,
                                     nonzero & (dist <= avg * _VP_LVN_MULT))
        val, vah = _value_area(dist, grid, int(np.argmax(dist)))
        out["val"], out["vah"] = val, vah
        return out
    except Exception:
        return None


# ── 辅助画线 / 结构标签 (威科夫画法) ───────────────────────────────
_STRUCT_SR_BAND = 0.25    # S/R 聚类检索带宽 (现价 ±25%)
_STRUCT_THROW_LOOK = 120  # Throwback 回溯窗口
_STRUCT_THROW_WIN = 20    # 突破后回踩搜索窗 (根)
_STRUCT_THROW_HOLD = 0.99 # 回踩守住上沿比例 (不收盘跌破该比例)
_STRUCT_THROW_MIN_DEPTH = 0.01  # 最小回踩深度 (×TR宽)
_STRUCT_THROW_VOL = 1.25  # 回踩均量相对突破前均量上限
_STRUCT_SOT_LOOK = 150     # SOT 回溯窗口
_STRUCT_SOT_MIN_LEGS = 2   # SOT 至少比较的下行腿数
_STRUCT_SOT_SHRINK = 0.8   # 末腿推力 ≤ 此倍数×以往均值才触发
_STRUCT_SOT_RECENT = 60    # SOT 点须落在最近根数内


def _detect_throwbacks(df, events, tr, look=_STRUCT_THROW_LOOK,
                       win=_STRUCT_THROW_WIN, hold=_STRUCT_THROW_HOLD,
                       pull_min=_STRUCT_THROW_MIN_DEPTH,
                       vol_mult=_STRUCT_THROW_VOL):
    """突破后回踩 Throwback: JOC 放量突破区间上沿后的缩量回踩, 且守住上沿。

    条件 (全部满足才标注, 防误标):
      - 存在 JOC 且突破当日收盘已站上 TR 上沿;
      - 其后 win 根内出现真实回踩 (从峰值回落深度 ≥ pull_min×TR宽);
      - 回踩全程收盘未跌破上沿 hold 比例 (未回补区间);
      - 回踩段均量 ≤ vol_mult × 突破前均量 (缩量回踩);
      - 截至当前收盘仍守住上沿 (突破未被证伪)。
    返回 [{"type","idx","price","conf","desc"}]。"""
    if not tr or not events:
        return []
    top = float(tr.get("top") or 0)
    if top <= 0:
        return []
    n = len(df)
    closes = df["close"].values
    lows = df["low"].values
    highs = df["high"].values
    vols = df["volume"].values
    tr_width = max(float(top) - float(tr.get("bottom", top * 0.8)), 1e-9)
    start = max(0, n - look)
    out = []
    for e in events:
        if e.get("type") != "JOC":
            continue
        i = int(e["idx"])
        if not (start <= i < n - 2):
            continue
        if not (closes[i] > top):
            continue  # 突破当日必须收盘站上上沿 (JOC 成立)
        base_vol = float(np.mean(vols[max(0, i - 4):i + 1]))
        if base_vol <= 0:
            base_vol = float(np.mean(vols)) or 1e-9
        j1 = min(n, i + 1 + win)
        if j1 - (i + 1) < 3:
            continue  # 突破后样本太少, 不足以形成真实回踩
        peak_slice = highs[i + 1:j1]
        peak_pos = int(np.argmax(peak_slice))
        peak_idx = i + 1 + peak_pos
        peak = float(peak_slice[peak_pos])
        pull_idx, pull_low, pull_vol, pull_n = None, None, 0.0, 0
        held = True
        for j in range(i + 1, j1):
            if closes[j] < top * hold:
                held = False
                break
            if j > peak_idx:  # 回踩低点须出现在突破后峰值之后
                this_low = float(lows[j])
                if pull_low is None or this_low < pull_low:
                    pull_low, pull_idx = this_low, j
                pull_vol += float(vols[j])
                pull_n += 1
        if not held or pull_idx is None or pull_n == 0:
            continue
        if peak - pull_low < pull_min * tr_width:
            continue  # 没有真实回踩 (横盘/直接走强), 不标
        avg_pull = pull_vol / pull_n
        if avg_pull > vol_mult * base_vol:
            continue  # 放量回踩 → 更像抛压, 不是干净的 Throwback
        if not (closes[n - 1] > top * hold):
            continue  # 突破已被证伪, 回踩意义消失
        conf = 60
        if (peak - pull_low) / tr_width < 0.5:
            conf += 5                    # 回踩浅, 结构更稳
        if avg_pull < 0.9 * base_vol:
            conf += 10                    # 明显缩量
        if closes[pull_idx] >= top:
            conf += 10                    # 回踩低点仍收在上沿上方
        conf = min(conf, 90)
        out.append({"type": "Throwback", "idx": pull_idx,
                    "price": pull_low, "conf": conf,
                    "desc": (f"突破区间上沿后缩量回踩, 守住 {top:.2f} 未回补"
                             f" (量 {avg_pull / base_vol:.2f}×前均)")})
    return out


def _detect_sot(df, pivots, phase=None, look=_STRUCT_SOT_LOOK,
                min_legs=_STRUCT_SOT_MIN_LEGS, shrink=_STRUCT_SOT_SHRINK,
                recent=_STRUCT_SOT_RECENT):
    """推力衰减 SOT: 下跌中连续创新低, 但最近一段的向下推力显著减弱。

    每段向下推力 = (段前高 - 新低) / 段前高。取最近 min_legs 段低点,
    若末段低点较上一段创新低、且推力 ≤ shrink×以往推力均值, 则标 SOT;
    末段低点量亦明显收缩时上调置信度。
    仅下跌/派发相位生效 (见 phases._phase_key)。
    返回 [{"type","idx","price","conf","desc"}]。"""
    try:
        from .phases import _phase_key
    except Exception:  # pragma: no cover
        _phase_key = None
    if _phase_key is not None:
        pkey = _phase_key(phase) if phase else "flat"
        if pkey not in ("markdown", "distribution"):
            return []
    n = len(df)
    if n < 60:
        return []
    start = max(0, n - look)
    lows = sorted([p for p in pivots if p["type"] == "low"
                   and p["idx"] >= start], key=lambda p: p["idx"])
    if len(lows) < 2:
        return []
    hi_arr = df["high"].values
    vol_arr = df["volume"].values
    legs = []
    for i in range(1, len(lows)):
        prev, cur = lows[i - 1], lows[i]
        if cur["price"] >= prev["price"]:
            continue  # 非新低段不构成向下推进
        hi = float(hi_arr[max(0, prev["idx"]):cur["idx"] + 1].max())
        thrust = (hi - cur["price"]) / hi if hi > 0 else 0.0
        legs.append({"idx": cur["idx"], "price": cur["price"],
                     "thrust": thrust, "vol": float(vol_arr[cur["idx"]])})
    if len(legs) < min_legs:
        return []
    last = legs[-1]
    prevs = legs[-min_legs:-1]
    mean_thrust = float(np.mean([l["thrust"] for l in prevs]))
    if mean_thrust <= 0:
        return []
    if last["thrust"] > shrink * mean_thrust:
        return []
    if last["idx"] < n - recent:
        return []
    vol_shrink = (len(prevs) > 0
                  and last["vol"] <= min(l["vol"] for l in prevs) * 1.05)
    conf = 55 + (20 if last["thrust"] <= 0.5 * mean_thrust else 10)
    if vol_shrink:
        conf += 15
    conf = min(conf, 90)
    return [{"type": "SOT", "idx": last["idx"], "price": last["price"],
             "conf": conf,
             "desc": (f"创新低但推力 {last['thrust'] * 100:.1f}% "
                      f"弱于前段均值 {mean_thrust * 100:.1f}%"
                      f"{', 量能收缩' if vol_shrink else ''}")}]


def structure_lines(df, pivots, events=None, tr=None, phase=None,
                    window=W_RECENT, min_tests=1):
    """威科夫辅助画线/结构标签数据 (供 K 线图叠加渲染)。

    返回 dict:
      - creek: 小溪 (吸筹区间上沿阻力) {"price","tests"} | None
      - ice:   冰线 (派发区间下沿支撑) {"price","tests"} | None
      - s_r:   现价附近支撑/阻力带 [{"price","role","tests","dist_pct"}]
      - events: 结构标签事件 (Throwback / SOT) 列表 (结构/方向解释见各检测器)

    creek/ice 优先取 find_trading_range 的 TR 上下轨 (与该箱体口径一致),
    无 TR 时退化为现价上方最近阻力/下方最近支撑聚类。
    """
    out = {"creek": None, "ice": None, "s_r": [], "events": []}
    n = len(df)
    if n < 40 or not pivots:
        return out
    last = float(df["close"].iloc[-1])
    start = max(0, n - window)
    lows = [p for p in pivots if p["type"] == "low" and p["idx"] >= start]
    highs = [p for p in pivots if p["type"] == "high" and p["idx"] >= start]

    # 现价附近的支撑/阻力聚类 (触及次数优先, 次数相同时距现价近者优先)
    band = _STRUCT_SR_BAND
    sup = [c for c in _cluster_prices([p["price"] for p in lows])
           if c[0] < last and c[0] > last * (1 - band) and c[1] >= min_tests]
    res = [c for c in _cluster_prices([p["price"] for p in highs])
           if c[0] > last and c[0] < last * (1 + band) and c[1] >= min_tests]

    def _pick(clusters):
        if not clusters:
            return None
        return sorted(clusters, key=lambda c: (-c[1], abs(c[0] - last)))[0]

    tr_top = tr.get("top") if tr else None
    tr_bottom = tr.get("bottom") if tr else None
    if tr_top:
        out["creek"] = {"price": float(tr_top),
                        "tests": int(tr.get("top_tests", 0) or 0)}
    else:
        ck = _pick(res)
        if ck:
            out["creek"] = {"price": float(ck[0]), "tests": int(ck[1])}
    if tr_bottom:
        out["ice"] = {"price": float(tr_bottom),
                      "tests": int(tr.get("bottom_tests", 0) or 0)}
    else:
        ic = _pick(sup)
        if ic:
            out["ice"] = {"price": float(ic[0]), "tests": int(ic[1])}

    for c in sorted(res, key=lambda x: x[0])[:2]:
        out["s_r"].append({"price": float(c[0]), "role": "R",
                           "tests": int(c[1]),
                           "dist_pct": (c[0] / last - 1.0) * 100.0})
    for c in sorted(sup, key=lambda x: -x[0])[:2]:
        out["s_r"].append({"price": float(c[0]), "role": "S",
                           "tests": int(c[1]),
                           "dist_pct": (1.0 - c[0] / last) * 100.0})
    out["s_r"].sort(key=lambda x: x["price"])

    try:
        out["events"] += _detect_throwbacks(df, events or [], tr)
    except Exception:
        pass
    try:
        out["events"] += _detect_sot(df, pivots, phase)
    except Exception:
        pass
    out["events"].sort(key=lambda x: x["idx"])
    return out


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
