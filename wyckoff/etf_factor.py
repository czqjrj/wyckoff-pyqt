"""ETF 三因子份额监测: 用上交所/深交所 ETF 每日份额变化 + 量能/方向因子,
推断汇金等国家队在宽基 ETF 上的加仓/减仓信号。

数据源:
  - 份额: akshare fund_etf_scale_sse(单日快照, 上交所) / fund_scale_daily_szse(区间, 深交所)
  - K线/成交量: wyckoff.datasource.fetch_kline (新浪→东财→腾讯 自动切换)

三因子模型 (与 etf-three-factor 口径一致):
  综合概率 = 量能概率×50% + 方向概率×20% + 份额概率×30%
  量能因子: 当日成交量相对近20日均量的异常放大程度
  方向因子: ETF 相对沪深300 近5日表现 + 护盘特征 (市场下跌而ETF逆势走强)
  份额因子: 近5日基金份额变化 (一级市场申购/赎回信号)

⚠ 关键点: ETF 份额增加 ≠ 一定是国家队买入 — 也可能是普通机构/散户申购,
属概率性信号, 不是确凿官方数据。信号分级: ≥0.7 高确信 / 0.5~0.7 中等关注 / <0.5 正常。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .datasource import fetch_kline
from .nteam import NTEAM_ETFS

SHARE_CACHE = {}
SHARE_TTL = 1800  # 份额数据缓存 30 分钟 (盘中更新)
SHARE_LOCK = threading.Lock()
# 原始快照缓存: SSE 按日期 (多只基金共享一次下载), SZSE 按区间 (整段 xlsx 共享)
_SSE_SNAP = {}
_SZSE_FRAME = {}
_SNAP_TTL = 1800

BENCH = "sh000300"  # 方向因子基准: 沪深300


def _code(symbol: str) -> str:
    return symbol[2:]


def fetch_share_history(symbol: str, days: int = 40) -> pd.DataFrame:
    """该 ETF 的份额历史, 返回 DataFrame(day, shares) 按日期升序; 失败返回空 df。"""
    key = (symbol, days)
    now = time.time()
    with SHARE_LOCK:
        c = SHARE_CACHE.get(key)
        if c and now - c[0] < SHARE_TTL:
            return c[1]
    out = pd.DataFrame(columns=["day", "shares"])
    try:
        if symbol.startswith("sz"):
            out = _share_history_szse(symbol, days)
        else:
            out = _share_history_sse(symbol, days)
    except Exception:
        out = pd.DataFrame(columns=["day", "shares"])
    with SHARE_LOCK:
        SHARE_CACHE[key] = (now, out)
    return out


def _share_history_szse(symbol: str, days: int) -> pd.DataFrame:
    """深交所: fund_scale_daily_szse 一次拉取全区间所有 ETF (原始帧按区间缓存),
    再按代码过滤。"""
    import akshare as ak
    end = pd.Timestamp.now()
    start = end - pd.Timedelta(days=int(days * 1.4) + 15)
    key = (start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    with SHARE_LOCK:
        raw = _SZSE_FRAME.get(key)
        if raw is not None and time.time() - raw[0] < _SNAP_TTL:
            frame = raw[1]
        else:
            frame = None
    if frame is None:
        frame = ak.fund_scale_daily_szse(
            start_date=key[0], end_date=key[1])
        with SHARE_LOCK:
            _SZSE_FRAME[key] = (time.time(), frame)
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["day", "shares"])
    sub = frame[frame["基金代码"].astype(str) == _code(symbol)]
    if len(sub) == 0:
        return pd.DataFrame(columns=["day", "shares"])
    out = pd.DataFrame({
        "day": pd.to_datetime(sub["日期"]),
        "shares": pd.to_numeric(sub["基金份额"], errors="coerce"),
    }).sort_values("day").dropna()
    return out.reset_index(drop=True)


def _share_history_sse(symbol: str, days: int) -> pd.DataFrame:
    """上交所: 按最近交易日逐日快照 (fund_etf_scale_sse 仅支持单日), 并发抓取。
    该接口单次约 6s 且当日数据盘后才发布, 故采样上限 6 个交易日即可覆盖
    1日/5日份额变化 (失败日期自动跳过, 用前一日兜底)。原始快照按日期缓存,
    多只基金共享。"""
    import akshare as ak
    try:
        idx = fetch_kline("sh000001", datalen=60, scale=240)
    except Exception:
        return pd.DataFrame(columns=["day", "shares"])
    dates = [d.strftime("%Y%m%d") for d in idx["day"].tail(min(days, 7))]

    def _snap(d):
        with SHARE_LOCK:
            c = _SSE_SNAP.get(d)
            if c is not None and time.time() - c[0] < _SNAP_TTL:
                return c[1]
        df = None
        try:
            df = ak.fund_etf_scale_sse(date=d)
        except Exception:
            df = None
        with SHARE_LOCK:
            _SSE_SNAP[d] = (time.time(), df)
        return df

    with ThreadPoolExecutor(max_workers=6) as ex:
        snaps = list(ex.map(_snap, dates))
    rows = []
    for d, df in zip(dates, snaps):
        if df is None or len(df) == 0:
            continue
        sub = df[df["基金代码"].astype(str) == _code(symbol)]
        if len(sub) == 0:
            continue
        rows.append((pd.to_datetime(str(sub["统计日期"].iloc[0])),
                     float(sub["基金份额"].iloc[0])))
    if not rows:
        return pd.DataFrame(columns=["day", "shares"])
    out = pd.DataFrame(rows, columns=["day", "shares"]).sort_values("day")
    return out.reset_index(drop=True)


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def compute_three_factor(symbol: str) -> dict:
    """对单个 ETF 计算三因子, 返回 {symbol, name, price, pct, vol_ratio, share_1d,
    share_5d, etf_ret5, bench_ret5, relative, vol_buy, dir_buy, share_buy,
    buy_prob, sell_prob, direction, strength, signal} 或 None (数据不足)。"""
    base = {"symbol": symbol, "name": symbol, "price": None, "pct": None,
            "vol_ratio": None, "share_1d": None, "share_5d": None,
            "etf_ret5": None, "bench_ret5": None, "relative": None,
            "direction": "none", "strength": 0.0, "signal": "正常",
            "buy_prob": 0.0, "sell_prob": 0.0}
    try:
        sh = fetch_share_history(symbol)
        kl = fetch_kline(symbol, datalen=60, scale=240)
        bench = fetch_kline(BENCH, datalen=60, scale=240)
    except Exception:
        return None
    if len(sh) < 6 or len(kl) < 30 or len(bench) < 30:
        return base
    try:
        kl2 = kl.sort_values("day").copy()
        kl2["day"] = pd.to_datetime(kl2["day"]).astype("datetime64[us]")
        sh2 = sh.sort_values("day").copy()
        sh2["day"] = pd.to_datetime(sh2["day"]).astype("datetime64[us]")
        merged = pd.merge_asof(kl2, sh2, on="day", direction="backward")
        merged = merged.dropna(subset=["shares"]).tail(10)
        if len(merged) < 6:
            return base
    except Exception:
        return base

    cur = merged.iloc[-1]
    prev = merged.iloc[-2]
    d5 = merged.iloc[-6]
    price = float(cur["close"])
    pct = float(cur["close"] / kl["close"].iloc[-2] - 1) * 100

    vol_ratio = float(cur["volume"] / kl["volume"].iloc[-21:-1].mean()) \
        if kl["volume"].iloc[-21:-1].mean() > 0 else 0.0
    share_1d = float(cur["shares"] / prev["shares"] - 1) if prev["shares"] else 0.0
    share_5d = float(cur["shares"] / d5["shares"] - 1) if d5["shares"] else 0.0
    etf_ret5 = float(cur["close"] / d5["close"] - 1)
    bench2 = bench.sort_values("day").copy()
    bench2["day"] = pd.to_datetime(bench2["day"]).astype("datetime64[us]")
    bench_s = bench2.set_index("day")["close"].reindex(merged["day"]).ffill()
    bench_ret5 = float(bench_s.iloc[-1] / bench_s.iloc[-6] - 1) \
        if len(bench_s) >= 6 and bench_s.iloc[-6] else 0.0
    relative = etf_ret5 - bench_ret5

    # ── 三因子 (买入侧 0~1) ──
    vol_buy = _clip((vol_ratio - 1.0) / 1.5)
    share_buy = _clip(share_5d / 0.03)
    dir_buy = _clip((relative + 0.01) / 0.03)
    # 护盘特征: 市场下跌而 ETF 逆势走强 → 疑似护盘/承接
    if etf_ret5 > 0.005 and bench_ret5 < -0.005:
        dir_buy = _clip(dir_buy + 0.15)
    buy = 0.5 * vol_buy + 0.2 * dir_buy + 0.3 * share_buy

    # ── 卖出侧 ──
    share_sell = _clip(-share_5d / 0.03)
    dir_sell = _clip((-relative + 0.01) / 0.03)
    sell = 0.5 * vol_buy + 0.2 * dir_sell + 0.3 * share_sell

    direction = "buy" if buy >= sell else "sell"
    strength = max(buy, sell)
    # 确认门槛: 无任何实质信号时 (量能不高 + 份额无实质变化) 归为正常
    if strength < 0.5 or (vol_ratio < 1.2 and abs(share_5d) < 0.005 and abs(relative) < 0.01):
        signal = "正常"
        direction = "none"
    elif strength >= 0.7:
        signal = "高确信买入" if direction == "buy" else "高确信卖出"
    else:
        signal = "中等关注·买入" if direction == "buy" else "中等关注·卖出"

    base.update({
        "name": symbol, "price": price, "pct": pct, "vol_ratio": round(vol_ratio, 2),
        "share_1d": share_1d * 100, "share_5d": share_5d * 100,
        "etf_ret5": etf_ret5 * 100, "bench_ret5": bench_ret5 * 100,
        "relative": relative * 100, "buy_prob": buy, "sell_prob": sell,
        "direction": direction, "strength": round(strength, 2), "signal": signal,
    })
    return base


def monitor_etfs(etfs=None) -> list:
    """批量监测国家队宽基 ETF, 返回按信号强度排序的结果列表。"""
    etfs = etfs or NTEAM_ETFS
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_monitor_one, symbol, name) for symbol, name in etfs]
        out = [f.result() for f in futures]
    out = [r for r in out if r is not None]
    _ORDER = {"高确信买入": 0, "高确信卖出": 0, "中等关注·买入": 1, "中等关注·卖出": 1,
              "正常": 2}
    out.sort(key=lambda r: (_ORDER.get(r["signal"], 3),
                            -abs(r.get("strength") or 0)))
    return out


def _monitor_one(symbol, name):
    base = {"symbol": symbol, "name": name, "price": None, "pct": None,
            "signal": "数据不足", "strength": 0.0, "vol_ratio": None,
            "share_5d": None, "buy_prob": 0.0, "sell_prob": 0.0}
    r = compute_three_factor(symbol)
    if not r:
        return base
    base.update(r)
    base["name"] = name
    return base
