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
from concurrent.futures import ThreadPoolExecutor

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


def _aggregate_breadth(diff):
    """从东财 list diff 聚合涨跌/涨停跌停计数。diff 为空返回 None。"""
    if not diff:
        return None
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


_BREADTH_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def _breadth_from_em():
    """主源: 东财全市场聚合涨跌家数/涨停跌停 (盘中实时)。

    主频道 push2 一次全量拉取; 若被限流 (RemoteDisconnected), 自动切
    push2delay 延迟频道按页 (100/页) 并发聚合, 保证广度在任一主机可达时可用。
    """
    from .fundamental import _get  # noqa: 内部 HTTP 容器
    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://quote.eastmoney.com/"}
    base = {"po": "1", "np": "1", "fltt": "2", "invt": "2",
            "fid": "f6", "fs": _BREADTH_FS, "fields": "f12,f3,f14"}
    try:
        r = _get("https://push2.eastmoney.com/api/qt/clist/get",
                 {**base, "pn": "1", "pz": "5000"}, headers,
                 retries=1, cache_fail=False)
        if r is not None:
            diff = ((r.json().get("data") or {}).get("diff")) or []
            if len(diff) > 100:  # 主频道整量; 排除被网关截断的极小响应
                out = _aggregate_breadth(diff)
                if out:
                    return out
    except Exception as e:
        log_exc("市场广度东财主源失败", e)
    # ── 兜底: push2delay 延迟频道分页 (100/页, 并发) ──
    try:
        head = _get("https://push2delay.eastmoney.com/api/qt/clist/get",
                    {**base, "pn": "1", "pz": "100"}, headers,
                    timeout=5, retries=1, cache_fail=False)
        if head is None:
            return None
        dd = (head.json().get("data") or {})
        try:
            total_n = int(dd.get("total") or 0)
        except (TypeError, ValueError):
            total_n = 0
        pages = max(1, (total_n + 99) // 100)
        diff = list((dd.get("diff") or []))
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_get,
                              "https://push2delay.eastmoney.com/api/qt/clist/get",
                              {**base, "pn": str(p), "pz": "100"}, headers,
                              5, 1, False)
                    for p in range(2, pages + 1)]
            for f in futs:
                rr = f.result()
                if rr is not None:
                    diff.extend((rr.json().get("data") or {}).get("diff") or [])
        out = _aggregate_breadth(diff)
        if out:
            out["src"] = "em-delay"
        return out
    except Exception as e:
        log_exc("市场广度东财delay源失败", e)
        return None


def _breadth_from_legu():
    """兜底源: 乐咕市场活跃度 (交易结束后/东财限流时)。

    接口仅在收盘后更新当日数据, 计数按「乐咕口径」(沪深A股, 剔除停牌/北交)。
    """
    try:
        import akshare as ak  # noqa: PLC0415
        import datetime as _dt
        df = ak.stock_market_activity_legu()
        if df is None or df.empty:
            return None
        m = {}
        for _, row in df.iterrows():
            item = str(row.get("item") or "")
            value = row.get("value")
            m[item] = value
        def _num(x):
            try:
                return int(float(str(x).replace("%", "").strip()))
            except (TypeError, ValueError):
                return 0
        up = _num(m.get("上涨"))
        down = _num(m.get("下跌"))
        flat = _num(m.get("平盘"))
        total = up + down + flat
        if total == 0:
            return None
        ts = None
        try:
            d = m.get("统计日期")
            if d:
                ts = _dt.datetime.strptime(str(d)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (TypeError, ValueError):
            ts = None
        return {
            "up": up, "down": down, "flat": flat,
            "limit_up": _num(m.get("涨停")),
            "limit_down": _num(m.get("跌停")),
            "total": total, "ts": ts or time.time(),
            "src": "legu",
        }
    except Exception as e:
        log_exc("市场广度乐咕源失败", e)
        return None


def fetch_market_breadth():
    """市场广度: 涨跌家数/涨停跌停 (双源: 东财盘中实时 → 乐咕收盘兜底, 30s缓存)。

    返回 {
        "up": int, "down": int, "flat": int,
        "limit_up": int, "limit_down": int,
        "total": int, "ts": float, "src": str|None
    }
    """
    def _do():
        return _breadth_from_em() or _breadth_from_legu() or None
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
    """板块涨跌排名 (缓存5min), 返回 [{name, pct, tone, score, flow20_yi, amount_yi}]。"""
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
                    "amount_yi": round(float(s.get("amount", 0) or 0), 2),
                })
            results.sort(key=lambda x: (-x["score"], x["name"]))
            return results
        except Exception as e:
            log_exc("仪表盘板块排名失败", e)
            return []
    return _cached("dash_sector", 300, _do)


def fetch_north_flow():
    """北向资金 (缓存5min)。返回 list[{market, net, msg}]。

    注意: 2024-08 起沪深港通停止逐日披露北向净流入, 该接口多返回全 0,
    已不对仪表盘主卡展示; 保留供其他模块兜底。"""
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


_MAIN_FLOW_INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
]


def _fnum(x):
    """东财字段 → float; "-"/None/NaN → None。"""
    if x is None or x == "-":
        return None
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def fetch_market_fund_flow():
    """主要指数主力资金流向 (东财 push2 快照, 盘中 60s 缓存)。

    返回 {
        "items": [{name, code, net_yi, net_pct, super_yi, big_yi, mid_yi, small_yi}],
        "total_yi": float,   # 上证+深成+创业板 主力净流入合计 (亿)
        "ts": float
    } 或 None (离线/失败)。
    """
    def _do():
        try:
            from .fundamental import _get  # noqa: 内部 HTTP 容器
            secids = ",".join(s for s, _ in _MAIN_FLOW_INDICES)
            r = _get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                {"secids": secids,
                 "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"},
                {"User-Agent": "Mozilla/5.0",
                 "Referer": "https://data.eastmoney.com/"},
                retries=2, cache_fail=False)
            if r is None:
                return None
            diff = (r.json().get("data") or {}).get("diff") or []
            items = []
            for item in diff:
                name = str(item.get("f14") or "")
                net = _fnum(item.get("f62"))
                if not name or net is None:
                    continue
                items.append({
                    "name": name,
                    "code": str(item.get("f12") or ""),
                    "pct": _fnum(item.get("f3")),
                    "net_yi": round(net / 1e8, 2),
                    "net_pct": _fnum(item.get("f184")),  # 千分位占比
                    "super_yi": _yyi(item.get("f66")),
                    "big_yi": _yyi(item.get("f72")),
                    "mid_yi": _yyi(item.get("f78")),
                    "small_yi": _yyi(item.get("f84")),
                })
            if not items:
                return None
            main = sum(x["net_yi"] for x in items
                       if x["name"] in ("上证指数", "深证成指", "创业板指"))
            return {"items": items, "total_yi": round(main, 2), "ts": time.time()}
        except Exception as e:
            log_exc("仪表盘主力资金失败", e)
            return None
    return _cached("dash_fund_flow", 60, _do)


def _yyi(x):
    """东财金额字段 → 亿 (失败返回 None)。"""
    v = _fnum(x)
    return None if v is None else round(v / 1e8, 2)


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


def build_sector_heatmap(top_n=24):
    """板块热力图数据 (缓存5min)。返回 [{name, pct, tone, amount_yi}]。

    画布含义: 方块大小=板块成交额 (越大越有资金关注), 颜色=涨跌幅
    (红涨绿跌, 深浅=幅度)。按成交额降序取 top_n, 排成 4x6 网格;
    成交额缺失 (东财源) 时按强度分数兜底排序。"""
    def _do():
        sectors = fetch_sector_ranking()
        if not sectors:
            return []
        rows = sorted(sectors, key=lambda s: -s.get("amount_yi", 0))
        if rows and all(s.get("amount_yi", 0) <= 0 for s in rows):
            rows = sorted(rows, key=lambda s: -s.get("score", 0))
        rows = rows[:top_n]
        return [{"name": s["name"], "pct": s.get("pct", 0),
                 "tone": s.get("tone", "neutral"),
                 "amount_yi": s.get("amount_yi", 0)} for s in rows]
    return _cached("dash_sector_heatmap", 300, _do)


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
        fund_flow: dict  — 主要指数主力资金流向
        sse_chart: dict — 上证K线图数据
        index_compare: dict — 多指数归一化对比
        sector_flow: list   — 板块资金条形图数据
        sector_heatmap: list — 板块热力图数据 (量价)
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
        "fund_flow": fetch_market_fund_flow() or None,
        "sse_chart": build_sse_chart() or None,
        "index_compare": build_index_compare() or None,
        "sector_flow": build_sector_flow_chart() or [],
        "sector_heatmap": build_sector_heatmap() or [],
    }
