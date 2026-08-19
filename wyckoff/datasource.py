# -*- coding: utf-8 -*-
"""行情数据获取 (K线 / 实时 / 名称), 带两级缓存与多数据源容灾。

默认走新浪财经接口; 失败时自动依次切换到东方财富、腾讯行情 (见 F3)。
所有 K 线统一返回同构 DataFrame: day/open/high/low/close/volume (单位为股),
且全部为**前复权**口径: 新浪返回不复权后用其复权因子表折算, 东财(fqt=1)与
腾讯(qfq)本身即前复权。三源口径一致, 保证跨会话/跨源分析结果可复现。

缓存两级: 进程内内存 (5min TTL) 之上叠加 SQLite 持久缓存 (见 sqldb.py,
K线 4h / 复权因子 7d), 重启应用不重抓历史, 批量扫描/回测大量复用。
"""
import json
import re
import time
from threading import Lock

import numpy as np
import pandas as pd
import requests

from .config import MIN_KLINE_BARS, SINA_HEADERS
from .utils import normalize_symbol
from . import sqldb

_KLINE_CACHE = {}
_KLINE_CACHE_TTL = 300  # 5分钟
_KLINE_CACHE_MAX = 64
_KLINE_LOCK = Lock()

# SQLite 持久缓存 TTL: 内存未命中时回退到落盘的行情数据, 跨会话复用。
# 比内存 TTL 长得多 (K线 4h, 复权因子 7d), 兼顾"重启不重抓历史"与"数据不过期失真"。
_KLINE_DB_TTL = 4 * 3600
_FACTOR_DB_TTL = 7 * 86400

# 新浪复权因子缓存 (除权除息低频, 缓存1天即可)
_FACTOR_CACHE = {}
_FACTOR_CACHE_TTL = 86400
_FACTOR_LOCK = Lock()

# 数据源日志: {(symbol, datalen, scale): source_name}, 供界面/报告标注
_SOURCE_LOG = {}

# 数据源健康度: {source: {ok, fail, last_ok_ts, last_err_ts, last_err}}
# 用于跨请求统计各源可用性, 界面可展示并帮助调整源优先级。
_SOURCE_HEALTH = {}
_HEALTH_LOCK = Lock()


def _health_hit(source, ok, err=None):
    """记录一次源请求结果 (成功/失败), 线程安全。"""
    with _HEALTH_LOCK:
        h = _SOURCE_HEALTH.setdefault(source, {"ok": 0, "fail": 0,
                                               "last_ok_ts": 0, "last_err_ts": 0,
                                               "last_err": ""})
        if ok:
            h["ok"] += 1
            h["last_ok_ts"] = time.time()
        else:
            h["fail"] += 1
            h["last_err_ts"] = time.time()
            h["last_err"] = str(err)[:200] if err else ""


def source_health():
    """返回各源健康度快照 {source: {ok, fail, ok_ratio, ...}}。"""
    with _HEALTH_LOCK:
        out = {}
        for name, h in _SOURCE_HEALTH.items():
            total = h["ok"] + h["fail"]
            out[name] = {
                "ok": h["ok"], "fail": h["fail"],
                "ok_ratio": round(h["ok"] / total, 3) if total else None,
                "last_ok_ts": h["last_ok_ts"], "last_err_ts": h["last_err_ts"],
                "last_err": h["last_err"],
            }
        return out


def reset_source_health():
    with _HEALTH_LOCK:
        _SOURCE_HEALTH.clear()

# K线源单次请求超时 (秒)。正常网络下各源 <1s, 6s 给足余量; 同时把"三源串行
# 全部失败"的最坏降级时长从 45s (3×15) 收紧到 18s (3×6), 提升无网络/源挂掉
# 时的响应性。
_HTTP_TIMEOUT = 6


def data_source_of(symbol: str, datalen: int, scale: int) -> str:
    """最近一次该键 K 线实际命中的数据源名称。"""
    with _KLINE_LOCK:
        return _SOURCE_LOG.get((symbol, datalen, scale), "新浪")


def _normalize_kline_df(rows) -> pd.DataFrame:
    """把各源解析出的 [date, open, high, low, close, volume] 行统一为分析用的 DataFrame。"""
    df = pd.DataFrame(rows, columns=["day", "open", "high", "low", "close", "volume"])
    df["day"] = pd.to_datetime(df["day"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    df.sort_values("day", inplace=True)
    df.reset_index(drop=True, inplace=True)
    if df.empty or len(df) < MIN_KLINE_BARS:
        raise RuntimeError(f"有效数据不足(仅{len(df)}根), 请选择更长的周期")
    return df


# ── 新浪 (主源) ──
def _fetch_kline_sina(symbol: str, datalen: int, scale: int) -> pd.DataFrame:
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/data/CN_MarketDataService.getKLineData"
    params = {"symbol": symbol, "scale": str(scale), "datalen": str(datalen), "ma": "no"}
    r = requests.get(url, params=params, headers=SINA_HEADERS, timeout=_HTTP_TIMEOUT)
    raw = re.search(r"data\((.*)\)", r.text, re.DOTALL)
    if not raw:
        raise RuntimeError("新浪接口未返回数据, 请检查代码或网络")
    data = json.loads(raw.group(1))
    if not data:
        raise RuntimeError(f"未获取到 {symbol} 的行情数据, 请检查代码是否正确")
    df = _normalize_kline_df([[d["day"], d["open"], d["high"], d["low"],
                               d["close"], d["volume"]] for d in data])
    _apply_sina_qfq(symbol, df)
    return df


def _sina_qfq_factors(symbol: str):
    """拉取新浪复权因子表 (qfq.js), 返回按日期升序的 DataFrame(d, f) 或 None。
    内存 → SQLite → 网络 三级: 重启后直接从落盘缓存恢复, 不再重复请求。"""
    now = time.time()
    with _FACTOR_LOCK:
        cached = _FACTOR_CACHE.get(symbol)
        if cached and now - cached[0] < _FACTOR_CACHE_TTL:
            return cached[1]
    fac = sqldb.qfq_load(symbol, _FACTOR_DB_TTL)
    if fac is not None and len(fac):
        with _FACTOR_LOCK:
            _FACTOR_CACHE[symbol] = (now, fac)
        return fac
    try:
        r = requests.get(f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js",
                         headers=SINA_HEADERS, timeout=_HTTP_TIMEOUT)
        js, _ = json.JSONDecoder().raw_decode(r.text[r.text.index("{") :])
        rows = js.get("data") or []
        if not rows:
            return None
        fac = pd.DataFrame(rows)
        fac["d"] = pd.to_datetime(fac["d"])
        fac["f"] = pd.to_numeric(fac["f"])
        fac = fac.sort_values("d").reset_index(drop=True)
        with _FACTOR_LOCK:
            _FACTOR_CACHE[symbol] = (now, fac)
        sqldb.qfq_save(symbol, fac)
        return fac
    except Exception:
        return None


def _apply_sina_qfq(symbol: str, df: pd.DataFrame) -> None:
    """把新浪不复权 K 线就地折算为前复权 (qfq = raw / 因子, 最新一段因子为1不变)。"""
    fac = _sina_qfq_factors(symbol)
    if fac is None or not len(fac):
        return
    idx = np.searchsorted(fac["d"].to_numpy(dtype="datetime64[ns]"),
                          df["day"].to_numpy(dtype="datetime64[ns]"), side="right") - 1
    idx = np.clip(idx, 0, len(fac) - 1)
    f = fac["f"].to_numpy()[idx]
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] / f


# ── 东方财富 (备源1) ──
_KLT_MAP = {240: 101, 120: None, 60: 60, 30: 30, 15: 15}


def _em_secid(symbol: str) -> str:
    code = symbol[-6:]
    return f"1.{code}" if symbol.startswith("sh") else f"0.{code}"


def _fetch_kline_eastmoney(symbol: str, datalen: int, scale: int) -> pd.DataFrame:
    klt = _KLT_MAP.get(scale)
    if klt is None:
        raise RuntimeError(f"东方财富不支持 {scale} 分钟周期")
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _em_secid(symbol), "klt": str(klt), "fqt": "1",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "beg": "0", "end": "20500101", "lmt": str(datalen),
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
    klines = ((r.json().get("data") or {}).get("klines")) or []
    if not klines:
        raise RuntimeError(f"东方财富未返回 {symbol} 数据")
    rows = []
    for k in klines:
        f = k.split(",")
        if len(f) < 6:
            continue
        date, op, cl, hi, lo, vol = f[0], f[1], f[2], f[3], f[4], f[5]
        rows.append([date, op, hi, lo, cl, float(vol) * 100])   # 手 → 股
    return _normalize_kline_df(rows)


# ── 腾讯 (备源2) ──
_TX_PERIOD = {240: "day", 120: "m120", 60: "m60", 30: "m30", 15: "m15"}


def _fetch_kline_tencent(symbol: str, datalen: int, scale: int) -> pd.DataFrame:
    period = _TX_PERIOD.get(scale)
    if period is None:
        raise RuntimeError(f"腾讯行情不支持 {scale} 分钟周期")
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{symbol},{period},,,{datalen},qfq"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
    node = (r.json().get("data") or {}).get(symbol) or {}
    klines = node.get(period) or node.get("qfq" + period) or node.get("day") or []
    if not klines:
        raise RuntimeError(f"腾讯未返回 {symbol} 数据")
    rows = []
    for k in klines:
        if len(k) < 6:
            continue
        date, op, cl, hi, lo, vol = k[0], k[1], k[2], k[3], k[4], k[5]
        rows.append([date, op, hi, lo, cl, float(vol) * 100])   # 手 → 股
    return _normalize_kline_df(rows)


def fetch_kline(symbol: str, datalen: int = 700, scale: int = 240, use_cache: bool = True) -> pd.DataFrame:
    """从行情接口获取K线 (scale: 240日线 / 120两小时 / 60一小时), 两级缓存。
    内存 5min 命中直接返回; 未命中回退到 SQLite 持久缓存 (重启后跨会话复用,
    需满足 datalen 根数否则视为失效重拉); 均未命中才请求网络
    (新浪 → 东方财富 → 腾讯 自动切换)。
    无论哪个源, 最终都只保留最近 datalen 根 K 线 (东方财富接口在 beg=0 时会忽略
    lmt 参数返回全部历史, 必须在此强制截断), 保证"时间段"选择真正生效。
    use_cache=False 时忽略缓存强制重新拉取 (并刷新内存/SQLite 缓存,
    供定时刷新/手动刷新用)。"""
    key = (symbol, datalen, scale)
    if use_cache:
        with _KLINE_LOCK:
            cached = _KLINE_CACHE.get(key)
            if cached and time.time() - cached[0] < _KLINE_CACHE_TTL:
                return cached[1].copy()
        hit = sqldb.kline_load(symbol, scale, _KLINE_DB_TTL)
        if hit is not None:
            df, source = hit
            if len(df) >= datalen:
                df = df.tail(datalen).reset_index(drop=True)
                with _KLINE_LOCK:
                    if len(_KLINE_CACHE) >= _KLINE_CACHE_MAX:
                        _KLINE_CACHE.pop(next(iter(_KLINE_CACHE)), None)
                    _KLINE_CACHE[key] = (time.time(), df.copy())
                    _SOURCE_LOG[key] = source
                return df.copy()
    last_err = None
    for name, fn in (("新浪", _fetch_kline_sina),
                     ("东方财富", _fetch_kline_eastmoney),
                     ("腾讯", _fetch_kline_tencent)):
        try:
            df = fn(symbol, datalen, scale)
            _health_hit(name, True)
            df = df.tail(datalen).reset_index(drop=True)
            with _KLINE_LOCK:
                if len(_KLINE_CACHE) >= _KLINE_CACHE_MAX:
                    _KLINE_CACHE.pop(next(iter(_KLINE_CACHE)), None)
                _KLINE_CACHE[key] = (time.time(), df.copy())
                _SOURCE_LOG[key] = name
            sqldb.kline_save(symbol, scale, df, name)
            return df
        except Exception as e:
            _health_hit(name, False, e)
            last_err = e
    raise RuntimeError(f"所有数据源均获取失败: {last_err}")


def fetch_realtime(codes):
    """批量获取实时行情, 返回 {code: {name, price, pct, ...}}; 新浪失败自动切腾讯。"""
    if not codes:
        return {}
    symbols = []
    for c in codes:
        try:
            symbols.append(normalize_symbol(c))
        except ValueError:
            continue
    if not symbols:
        return {}
    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
        r = requests.get(url, headers=SINA_HEADERS, timeout=_HTTP_TIMEOUT)
        r.encoding = "gbk"
        entries = {}
        for line in r.text.strip().splitlines():
            m = re.match(r'var hq_str_([a-z]{2}\d{6})="([^"]*)"', line)
            if not m:
                continue
            sym, payload = m.groups()
            f = payload.split(",")
            if len(f) < 32 or not f[0]:
                continue
            name = f[0]
            open_p, prev_close, cur = (float(x) for x in f[1:4])
            high, low = float(f[4]), float(f[5])
            pct = (cur - prev_close) / prev_close * 100 if prev_close else 0
            # 按完整符号为键, 指数 sh000001 与个股 sz000001 互不覆盖
            entries[sym] = {"name": name, "price": cur, "pct": pct,
                            "open": open_p, "high": high, "low": low,
                            "prev_close": prev_close, "symbol": sym,
                            "date": f[30].strip() if len(f) > 30 else "",
                            "volume": float(f[8])}
        if entries:
            _health_hit("新浪", True)
            # 集合竞价时段 (09:15-09:25) 新浪价格字段常为 0, 导致界面误判"获取失败":
            # 对 price<=0 的标的用腾讯源补齐 (腾讯可返回盘前撮合价), 仍为 0 则剔除。
            bad = [s for s in symbols if entries.get(s, {}).get("price", 0) <= 0]
            if bad:
                try:
                    fill = _fetch_realtime_tencent(bad)
                    for s in bad:
                        e = entries.get(s)
                        q = fill.get(s[2:]) or {}
                        if e and q.get("price") and q["price"] > 0:
                            e = dict(e)
                            e.update(q)
                            entries[s] = e
                except Exception:
                    pass
                for s in list(entries):
                    if entries[s].get("price", 0) <= 0:
                        entries.pop(s)
            # 返回: 完整符号键为主, 另附 6 位代码别名 (同码冲突时别名指向任一)
            out = dict(entries)
            for s, e in entries.items():
                out.setdefault(s[2:], e)
            return out
        _health_hit("新浪", False, "空响应")
    except Exception as e:
        _health_hit("新浪", False, e)
        pass
    return _fetch_realtime_tencent(symbols)


def _fetch_realtime_tencent(symbols):
    """腾讯实时行情兜底。返回结构同 fetch_realtime。"""
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                       "Referer": "https://gu.qq.com/"},
                         timeout=_HTTP_TIMEOUT)
        r.encoding = "gbk"
        entries = {}
        for line in r.text.strip().splitlines():
            m = re.match(r'v_([a-z]{2}\d{6})="([^"]*)"', line)
            if not m:
                continue
            sym, payload = m.groups()
            f = payload.split("~")
            if len(f) < 35 or not f[1]:
                continue
            name = f[1]
            try:
                cur = float(f[3])
                prev_close = float(f[4])
                open_p = float(f[5])
                high = float(f[33])
                low = float(f[34])
            except (ValueError, IndexError):
                continue
            pct = (cur - prev_close) / prev_close * 100 if prev_close else 0
            dstr = f[30] if len(f) > 30 and f[30] else ""
            if len(dstr) >= 8:
                dstr = f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:8]}"
            # 按完整符号为键, 指数与同代码个股互不覆盖
            entries[sym] = {"name": name, "price": cur, "pct": pct,
                            "open": open_p, "high": high, "low": low,
                            "prev_close": prev_close, "symbol": sym,
                            "date": dstr,
                            "volume": float(f[6]) * 100}
        if entries:
            _health_hit("腾讯", True)
        else:
            _health_hit("腾讯", False, "空响应")
        out = dict(entries)
        for s, e in entries.items():
            out.setdefault(s[2:], e)
        return out
    except Exception as e:
        _health_hit("腾讯", False, e)
        return {}


_NAME_CACHE = {}
_NAME_TTL = 86400  # 股票名称稳定, 缓存1天 (与复权因子缓存一致)
_NAME_LOCK = Lock()


def _name_from_local(symbol: str) -> str:
    """本地股票名称表 (wyckoff_stock_names.json / wyckoff_all_stocks.json) 查询。
    覆盖全A (~5900只), 命中即可免一次网络请求; 失败返回空串。延迟导入
    screener 避免 datasource → screener 循环依赖。"""
    try:
        from .screener import _get_stock_name
        return _get_stock_name(symbol) or ""
    except Exception:
        return ""


def fetch_name(symbol: str, use_cache: bool = True) -> str:
    """获取股票名称: 内存缓存 → 本地名表 → 新浪网络 三级, 网络失败返回 symbol。

    批量扫描/回测对每只股票取名称时, 本地名表命中即零网络开销。
    """
    now = time.time()
    if use_cache:
        with _NAME_LOCK:
            cached = _NAME_CACHE.get(symbol)
            if cached and now - cached[0] < _NAME_TTL:
                return cached[1]
    name = _name_from_local(symbol)
    if not name:
        try:
            r = requests.get(f"https://hq.sinajs.cn/list={symbol}",
                             headers=SINA_HEADERS, timeout=_HTTP_TIMEOUT)
            m = re.search(r'="([^,]+),', r.text)
            if m:
                name = m.group(1)
        except Exception:
            pass
    if not name:
        name = symbol
    with _NAME_LOCK:
        _NAME_CACHE[symbol] = (now, name)
    return name


def merge_realtime_bar(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """把实时行情合并为K线最新一根bar (仅日线)。

    新浪/东财/腾讯日线只返回**已完成**的交易日, 盘中最新bar缺失, 导致分析图
    "现价"停留在昨收, 与实际价格不一致。用实时行情补齐当日bar: 前复权口径下
    当日复权因子=1, 实时价即为前复权价, 可直接合并。分钟级K线已含当日bar, 不做合并。
    """
    if df["day"].dt.hour.nunique() > 1:
        return df
    code = symbol[-6:]
    rt = fetch_realtime([symbol])
    q = rt.get(code)
    if not q:
        return df
    price = q.get("price")
    if price is None or price <= 0:
        return df
    day = pd.to_datetime(q.get("date") or pd.Timestamp.now().strftime("%Y-%m-%d"))
    last_day = df["day"].iloc[-1].normalize()
    if day <= last_day:
        return df
    # 集合竞价时段 open/high/low 常为 0, 用 prev_close 兜底, 避免合并出 low=0 的 bar
    # 导致后续分析除零 (如 phases._detect_ranges 带宽计算)。
    prev = float(df["close"].iloc[-1])
    hi = float(q["high"]) if (q.get("high") or 0) > 0 else max(price, prev)
    lo = float(q["low"]) if (q.get("low") or 0) > 0 else min(price, prev)
    op = float(q["open"]) if (q.get("open") or 0) > 0 else prev
    bar = pd.DataFrame([{
        "day": day,
        "open": op, "high": hi, "low": lo, "close": float(price),
        "volume": float(q.get("volume") or 0),
    }])
    return pd.concat([df, bar], ignore_index=True)
