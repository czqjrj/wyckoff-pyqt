"""大盘仪表盘数据聚合层 — 为 dashboard_widget 提供全部数据。

函数设计为 **纯数据/无 UI 依赖**, 供 worker 线程调用; 所有重型函数均内置
内存缓存 (锁线程安全), 避免重复抓取。缓存 TTL:
  - 实时行情: 30s (盘中高频)
  - 技术指标: 300s (5min, 日线盘中不变)
  - 威科夫分析: 1800s (30min, 阶段结论变化极慢)
  - 板块: 300s
  - 资金流/北向: 300s
  - 市场广度: 30s (盘中实时)
"""
import threading
import time

import numpy as np
import pandas as pd

from ._log import log_exc
from .datasource import fetch_kline, fetch_realtime, fetch_name
from .indicators import add_indicators, find_pivots
from .events import detect_all
from .phases import judge_phase
from .market import fetch_market_env
from .fundamental import fetch_all_board_stats
from .config import EVENT_CN

# ── 缓存基础设施 ──
_lock = threading.Lock()
_cache: dict = {}


def _cached(key, ttl, fn):
    with _lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < ttl:
            return entry[1]
    try:
        val = fn()
    except Exception:
        val = None
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def clear_cache():
    with _lock:
        _cache.clear()


# ── 指数集合 (固定6只) ──
DASH_INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000688", "name": "科创50"},
    {"code": "sh000300", "name": "沪深300"},
    {"code": "sh000905", "name": "中证500"},
]


def _fetch_index_realtime_raw():
    """6大指数实时行情 (新浪/腾讯双源), 返回 {symbol: dict}。"""
    codes = [idx["code"] for idx in DASH_INDICES]
    try:
        return fetch_realtime(codes) or {}
    except Exception as e:
        log_exc("仪表盘指数实时行情失败", e)
        return {}


def fetch_index_realtime():
    return _cached("dash_rt", 30, _fetch_index_realtime_raw)


def fetch_market_breadth():
    """市场广度: 从东财全市场 push2 聚合涨跌家数/涨停跌停。

    返回 {
        "up": int, "down": int, "flat": int,
        "limit_up": int, "limit_down": int,
        "total": int, "ts": float
    }
    """
    def _do():
        from .fundamental import _get  # noqa: 内部 HTTP 容器
        import re
        try:
            r = _get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                {"pn": "1", "pz": "5000", "po": "1", "np": "1",
                 "fltt": "2", "invt": "2", "fid": "f6",
                 "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                 "fields": "f12,f3,f14"},
                {"User-Agent": "Mozilla/5.0",
                 "Referer": "https://quote.eastmoney.com/"},
                retries=1, cache_fail=False)
            if r is None:
                return None
            diff = ((r.json().get("data") or {}).get("diff")) or []
            up = down = flat = lu = ld = 0
            for item in diff:
                pct = item.get("f3")
                if pct is None or pct == "-":
                    continue
                try:
                    pct = float(pct)
                except (TypeError, ValueError):
                    continue
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
                else:
                    flat += 1
                if pct >= 9.9:
                    lu += 1
                elif pct <= -9.9:
                    ld += 1
            total = up + down + flat
            if total == 0:
                return None
            return {
                "up": up, "down": down, "flat": flat,
                "limit_up": lu, "limit_down": ld,
                "total": total, "ts": time.time(),
            }
        except Exception as e:
            log_exc("市场广度获取失败", e)
            return None
    return _cached("breadth", 30, _do)


def build_index_technicals():
    """为每只指数构建技术指标快照 (缓存5min)。

    返回 {symbol: {
        "close": float, "ret": float,
        "ma20": float, "ma50": float, "ma200": float,
        "macd_dif": float, "macd_dea": float, "macd_hist": float,
        "rsi_6": float, "rsi_12": float,
        "kdj_k": float, "kdj_j": float,
        "boll_up": float, "boll_dn": float,
        "vol_ratio": float,
        "phase": str, "events": list,
    }}
    """
    def _do():
        out = {}
        for idx_info in DASH_INDICES:
            sym = idx_info["code"]
            name = idx_info["name"]
            try:
                df = fetch_kline(sym, datalen=250, scale=240)
                if df is None or len(df) < 30:
                    continue
                df = add_indicators(df, symbol=sym)
                pivots = find_pivots(df, order=6)
                events = detect_all(df, pivots)
                phase, _ = judge_phase(df, pivots, events)
                last = float(df["close"].iloc[-1])
                prev = float(df["close"].iloc[-2]) if len(df) >= 2 else last
                ret = (last / prev - 1) * 100 if prev else 0

                recent_events = [e for e in events if e["idx"] >= len(df) - 30]
                recent_events.sort(key=lambda e: e["idx"], reverse=True)

                out[sym] = {
                    "name": name,
                    "close": last,
                    "ret": round(ret, 2),
                    "ma20": float(df["price_ma20"].iloc[-1]) if "price_ma20" in df else None,
                    "ma50": float(df["price_ma50"].iloc[-1]) if "price_ma50" in df else None,
                    "ma200": float(df["price_ma200"].iloc[-1]) if "price_ma200" in df else None,
                    "macd_dif": float(df["macd_dif"].iloc[-1]) if "macd_dif" in df else None,
                    "macd_dea": float(df["macd_dea"].iloc[-1]) if "macd_dea" in df else None,
                    "macd_hist": float(df["macd_hist"].iloc[-1]) if "macd_hist" in df else None,
                    "rsi_6": float(df["rsi_6"].iloc[-1]) if "rsi_6" in df else None,
                    "rsi_12": float(df["rsi_12"].iloc[-1]) if "rsi_12" in df else None,
                    "kdj_k": float(df["kdj_k"].iloc[-1]) if "kdj_k" in df else None,
                    "kdj_j": float(df["kdj_j"].iloc[-1]) if "kdj_j" in df else None,
                    "boll_up": float(df["boll_up"].iloc[-1]) if "boll_up" in df else None,
                    "boll_dn": float(df["boll_dn"].iloc[-1]) if "boll_dn" in df else None,
                    "vol_ratio": float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df else None,
                    "phase": phase,
                    "events": [
                        {"type": e["type"], "date": str(e.get("date", "")),
                         "conf": e.get("conf", 50)}
                        for e in recent_events[:5]
                    ],
                }
            except Exception as e:
                log_exc(f"仪表盘指数技术指标失败 {sym}", e)
                continue
        return out
    return _cached("dash_tech", 300, _do)


def fetch_sector_ranking():
    """板块涨跌排名 (缓存5min), 返回 [{name, pct, tone, score, flow20_yi}]。"""
    def _do():
        try:
            stats = fetch_all_board_stats()
            results = []
            for s in stats:
                if not s.get("live", False):
                    continue
                flow_raw = s.get("flow20", 0)
                pct = s.get("pct", 0)
                if abs(flow_raw) > 1e6:
                    flow = flow_raw / 1e8
                elif abs(flow_raw) < 1e4:
                    flow = flow_raw
                else:
                    flow = flow_raw / 1e4
                if flow > 0.5 and pct > 0:
                    tone, score = "bullish", min(100, abs(flow) * 5 + pct * 3 + 40)
                elif flow < -0.5 and pct < 0:
                    tone, score = "bearish", min(100, abs(flow) * 5 + abs(pct) * 3 + 40)
                elif flow > 0.5 or flow < -0.5:
                    tone, score = "mixed", min(100, abs(flow) * 5 + 30)
                elif abs(pct) > 1:
                    tone, score = "mixed", min(100, abs(pct) * 3 + 25)
                else:
                    tone, score = "neutral", 10
                results.append({
                    "name": s["name"], "pct": pct, "tone": tone,
                    "score": round(score, 1), "flow20_yi": round(flow, 2),
                })
            results.sort(key=lambda x: (-x["score"], x["name"]))
            return results
        except Exception as e:
            log_exc("仪表盘板块排名失败", e)
            return []
    return _cached("dash_sector", 300, _do)


def fetch_north_flow():
    """北向资金 (缓存5min)。返回 list[{market, net, msg}]。"""
    def _do():
        try:
            from .flow_extra import fetch_north
            data = fetch_north()
            if not data:
                return []
            out = []
            for item in data:
                if item.get("market"):
                    out.append({
                        "market": item["market"],
                        "net": item.get("net"),
                        "msg": item.get("msg", ""),
                    })
            return out if out else None
        except Exception as e:
            log_exc("仪表盘北向资金失败", e)
            return None
    return _cached("dash_north", 300, _do)


def fetch_limit_up_pool():
    """涨停池数据 (缓存5min)。返回 list[dict]。"""
    def _do():
        try:
            from .flow_extra import fetch_ztpool
            return fetch_ztpool(lookback_days=2) or None
        except Exception as e:
            log_exc("仪表盘涨停池失败", e)
            return None
    return _cached("dash_zt", 300, _do)


# ── 图表数据 ──
CHART_BARS = 120  # 图表默认展示最近根数


def build_sse_chart(bars=CHART_BARS):
    """上证指数 K线图数据 (缓存5min)。返回 dict:
        days: list[str]  — 日期标签
        x: list[int]     — 柱索引
        open/high/low/close/volume: list[float]
        ma20/ma50: list[float] (前段 NaN)
        last_close: float, env: str, tone: str
    """
    def _do():
        try:
            df = add_indicators(
                fetch_kline("sh000001", datalen=250, scale=240), symbol="sh000001")
            if df is None or len(df) < 30:
                return None
            df = df.tail(bars).reset_index(drop=True)
            days = [str(d)[:10] for d in df["day"]]
            x = list(range(len(df)))
            env = fetch_market_env() or {}
            return {
                "days": days,
                "x": x,
                "open": [float(v) for v in df["open"]],
                "high": [float(v) for v in df["high"]],
                "low": [float(v) for v in df["low"]],
                "close": [float(v) for v in df["close"]],
                "volume": [float(v) for v in df["volume"]],
                "ma20": [None if pd.isna(v) else float(v) for v in df["price_ma20"]],
                "ma50": [None if pd.isna(v) else float(v) for v in df["price_ma50"]],
                "last_close": float(df["close"].iloc[-1]),
                "env": env.get("env", ""),
                "tone": env.get("tone", ""),
            }
        except Exception as e:
            log_exc("仪表盘上证K线图失败", e)
            return None
    return _cached("dash_sse_chart", 300, _do)


def build_index_compare(bars=CHART_BARS):
    """多指数归一化对比 (缓存5min)。返回 dict:
        days: list[str]
        series: list[{name, code, data: list[float]}]  — 以窗口首日为 100 归一
    """
    def _one(sym, name):
        try:
            df = fetch_kline(sym, datalen=250, scale=240)
            if df is None or len(df) < 30:
                return None
            df = df.tail(bars).reset_index(drop=True)
            close = df["close"].astype(float).values
            base = close[0]
            if not base or base <= 0:
                return None
            return {"name": name, "code": sym,
                    "data": [(c / base * 100) for c in close]}
        except Exception:
            return None

    def _do():
        pairs = [
            ("sh000001", "上证指数"),
            ("sz399001", "深证成指"),
            ("sz399006", "创业板指"),
            ("sh000300", "沪深300"),
        ]
        series = []
        base_days = None
        for sym, name in pairs:
            one = _one(sym, name)
            if one is None:
                continue
            series.append(one)
            if base_days is None:
                try:
                    df = fetch_kline(sym, datalen=250, scale=240)
                    base_days = [str(d)[:10] for d in df.tail(bars)["day"]]
                except Exception:
                    base_days = []
        if not series:
            return None
        return {"days": base_days or [], "series": series}
    return _cached("dash_index_cmp", 300, _do)


def build_sector_flow_chart(top_n=8):
    """板块资金流向条形图数据 (缓存5min)。返回 list[{name, flow, pct, tone}]
    按 |flow20_yi| 降序取 top_n 个板块 (正流入在前)。"""
    def _do():
        sectors = fetch_sector_ranking()
        if not sectors:
            return []
        rows = sorted(sectors, key=lambda s: abs(s.get("flow20_yi", 0)),
                      reverse=True)[:top_n]
        # 正流入(红)靠左递增, 负流出(绿)靠右, 一眼看多空
        rows.sort(key=lambda s: (s.get("flow20_yi", 0) <= 0,
                                 s.get("flow20_yi", 0)))
        return [{"name": s["name"], "flow": s.get("flow20_yi", 0),
                 "pct": s.get("pct", 0), "tone": s.get("tone", "neutral")}
                for s in rows]
    return _cached("dash_sector_flow", 300, _do)


def build_dashboard_data():
    """仪表盘全量数据聚合 (供 DashboardThread worker 线程调用)。

    返回 dict:
        indices: dict  — {symbol: index_info}
        tech: dict     — {symbol: tech_snapshot}
        breadth: dict  — 涨跌家数统计
        sectors: list  — 板块排名
        north: list    — 北向资金
        zt_pool: list  — 涨停池
        market_env: dict — 大盘环境 (bullish/bearish/neutral)
        sse_chart: dict — 上证K线图数据
        index_compare: dict — 多指数归一化对比
        sector_flow: list   — 板块资金条形图数据
    """
    indices = fetch_index_realtime()
    tech = build_index_technicals()
    breadth = fetch_market_breadth()
    sectors = fetch_sector_ranking()
    north = fetch_north_flow()
    zt_pool = fetch_limit_up_pool()
    market_env = fetch_market_env()
    return {
        "indices": indices or {},
        "tech": tech or {},
        "breadth": breadth or {},
        "sectors": sectors or [],
        "north": north or [],
        "zt_pool": zt_pool or [],
        "market_env": market_env or {},
        "sse_chart": build_sse_chart() or None,
        "index_compare": build_index_compare() or None,
        "sector_flow": build_sector_flow_chart() or [],
    }
