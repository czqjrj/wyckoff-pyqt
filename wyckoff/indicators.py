# -*- coding: utf-8 -*-
"""技术指标与 ZigZag 枢轴点。"""
import numpy as np
import pandas as pd


def _limit_pct(symbol) -> float:
    """按板块返回近似涨跌停阈值 (含阈值内回旋余量):
    创业板(30x)/科创板(68x) 20%, 北交所(8x/4x) 30%, 其余主板 10%。"""
    code6 = (symbol or "")[-6:]
    if code6.startswith(("30", "68")):
        return 0.199
    if code6.startswith(("8", "4")):
        return 0.299
    return 0.099


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    """使用前缀和的 O(n) 滑动平均, 避免 pandas rolling 开销。

    NaN 感知: 窗口内计入有效值个数, 需凑满 window 个有效值 (与 pandas
    rolling(window).mean() 的 min_periods=window 行为一致), 避免 cumsum
    把 NaN 传染整列 (如 ATR 首根 TR 为 NaN)。
    """
    n = len(arr)
    if n < window:
        return np.full(n, np.nan)
    out = np.full(n, np.nan)
    clean = np.nan_to_num(arr, nan=0.0)
    cs = np.concatenate(([0.0], np.cumsum(clean)))
    csm = np.concatenate(([0.0], np.cumsum(np.isfinite(arr).astype(float))))
    win_sum = cs[window:] - cs[:-window]
    win_n = csm[window:] - csm[:-window]
    ok = win_n >= window
    out[window - 1:] = np.where(ok, win_sum / np.where(win_n > 0, win_n, 1.0), np.nan)
    return out


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    """使用前缀和的 O(n) 滑动标准差 (NaN 感知, 语义同 _rolling_mean)。"""
    n = len(arr)
    if n < window:
        return np.full(n, np.nan)
    out = np.full(n, np.nan)
    clean = np.nan_to_num(arr, nan=0.0)
    cs = np.concatenate(([0.0], np.cumsum(clean)))
    cs2 = np.concatenate(([0.0], np.cumsum(clean * clean)))
    csm = np.concatenate(([0.0], np.cumsum(np.isfinite(arr).astype(float))))
    win_sum = cs[window:] - cs[:-window]
    win_sum2 = cs2[window:] - cs2[:-window]
    win_n = csm[window:] - csm[:-window]
    ok = win_n >= window
    denom = np.where(win_n > 0, win_n, 1.0)
    mean = win_sum / denom
    var = win_sum2 / denom - mean * mean
    np.maximum(var, 0, out=var)
    out[window - 1:] = np.where(ok, np.sqrt(var), np.nan)
    return out


def _ewma(arr: np.ndarray, span: int, adjust: bool = False) -> np.ndarray:
    """指数加权移动平均的纯 NumPy 实现。"""
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def add_indicators(df: pd.DataFrame, symbol: str = None) -> pd.DataFrame:
    """向量化指标计算: 单次遍历计算所有指标, 避免多次 DataFrame 复制与 rolling 开销。"""
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    open_ = df["open"].values
    volume = df["volume"].values
    
    out = pd.DataFrame(index=df.index)
    out["day"] = df["day"]
    out["open"] = open_
    out["high"] = high
    out["low"] = low
    out["close"] = close
    out["volume"] = volume
    
    # 基础衍生量
    ret = np.empty(n)
    ret[0] = np.nan
    ret[1:] = np.diff(close) / close[:-1]
    out["ret"] = ret
    
    rng = high - low
    out["range"] = rng
    out["body"] = np.abs(close - open_)
    out["upper_wick"] = high - np.maximum(open_, close)
    out["lower_wick"] = np.minimum(open_, close) - low
    out["direction"] = np.where(close >= open_, 1, -1)
    
    # 成交量均线 (前缀和 O(n))
    out["vol_ma5"] = _rolling_mean(volume, 5)
    out["vol_ma10"] = _rolling_mean(volume, 10)
    vol_ma20 = _rolling_mean(volume, 20)
    out["vol_ma20"] = vol_ma20
    out["vol_ma50"] = _rolling_mean(volume, 50)
    out["vol_ratio_20"] = volume / vol_ma20
    
    # 量 Z-score
    vol_std20 = _rolling_std(volume, 20)
    vol_z_20 = np.full_like(volume, np.nan, dtype=float)
    np.divide(volume - vol_ma20, vol_std20, out=vol_z_20, where=vol_std20 > 0)
    out["vol_z_20"] = vol_z_20
    
    # 价格均线
    out["price_ma5"] = _rolling_mean(close, 5)
    out["price_ma10"] = _rolling_mean(close, 10)
    price_ma20 = _rolling_mean(close, 20)
    out["price_ma20"] = price_ma20
    out["price_ma50"] = _rolling_mean(close, 50)
    out["price_ma200"] = _rolling_mean(close, 200)
    
    # ATR(14)
    pc = np.roll(close, 1)
    pc[0] = np.nan
    tr = np.maximum.reduce([rng, np.abs(high - pc), np.abs(low - pc)])
    out["atr"] = _rolling_mean(tr, 14)
    
    # BOLL (20, 2)
    out["boll_mid"] = price_ma20
    boll_std = _rolling_std(close, 20)
    out["boll_std"] = boll_std
    out["boll_up"] = price_ma20 + 2 * boll_std
    out["boll_dn"] = price_ma20 - 2 * boll_std
    
    # MACD (12, 26, 9) - EWMA
    e12 = _ewma(close, 12)
    e26 = _ewma(close, 26)
    macd_dif = e12 - e26
    out["macd_dif"] = macd_dif
    macd_dea = _ewma(macd_dif, 9)
    out["macd_dea"] = macd_dea
    out["macd_hist"] = (macd_dif - macd_dea) * 2
    
    # KDJ (9, 3, 3)
    low9_min = np.full(n, np.nan)
    high9_max = np.full(n, np.nan)
    # 滑动窗口 min/max 使用 deque 优化
    from collections import deque
    min_dq = deque()
    max_dq = deque()
    for i in range(n):
        while min_dq and min_dq[0] <= i - 9:
            min_dq.popleft()
        while min_dq and low[min_dq[-1]] >= low[i]:
            min_dq.pop()
        min_dq.append(i)
        low9_min[i] = low[min_dq[0]]
        
        while max_dq and max_dq[0] <= i - 9:
            max_dq.popleft()
        while max_dq and high[max_dq[-1]] <= high[i]:
            max_dq.pop()
        max_dq.append(i)
        high9_max[i] = high[max_dq[0]]
    
    rsv = np.full_like(close, np.nan, dtype=float)
    np.divide((close - low9_min) * 100, high9_max - low9_min, out=rsv, where=high9_max > low9_min)
    kdj_k = _ewma(np.nan_to_num(rsv, nan=50.0), 3, adjust=False)  # com=2 → span=3
    out["kdj_k"] = kdj_k
    kdj_d = _ewma(kdj_k, 3, adjust=False)
    out["kdj_d"] = kdj_d
    out["kdj_j"] = 3 * kdj_k - 2 * kdj_d
    
    # RSI (6, 12, 24)
    diff = np.empty(n)
    diff[0] = np.nan
    diff[1:] = np.diff(close)
    for period in (6, 12, 24):
        up = np.where(diff > 0, diff, 0)
        dn = np.where(diff < 0, -diff, 0)
        up_ma = _rolling_mean(up, period)
        dn_ma = _rolling_mean(dn, period)
        rs = np.full_like(up_ma, np.nan, dtype=float)
        np.divide(up_ma, dn_ma, out=rs, where=dn_ma > 0)
        out[f"rsi_{period}"] = 100 - 100 / (1 + rs)
    
    # OBV
    sign = np.sign(diff)
    sign[0] = 0
    out["obv"] = np.cumsum(sign * volume)
    
    # 涨跌停/一字板标记
    pct = ret
    lim = _limit_pct(symbol)
    out["limit_up"] = (close >= high - 1e-9) & (pct >= lim - 0.005)
    out["limit_dn"] = (close <= low + 1e-9) & (pct <= -(lim - 0.005))
    out["locked"] = out["limit_up"] | out["limit_dn"]
    out["is_new_stock"] = n < 120
    
    return out


# 枢轴灵敏度档位 → ZigZag order (对称邻域半径)。
# 参考 TradingView "Wyckoff Schematic Auto-Detect" 的 Pivot Detection Speed
# (fast/normal/safe): order 越大要求越高, 枢轴越少越稳 (safe 档假信号少);
# order 越小捕捉越细 (fast 档更激进, 适合日内/短窗口)。
PIVOT_SENSITIVITY = {
    "fast": 3,
    "normal": 6,
    "safe": 9,
}


def pivot_order(sensitivity: str = "normal") -> int:
    """灵敏度档位 → find_pivots 的 order 参数。未知档位回退 normal(6)。"""
    return PIVOT_SENSITIVITY.get(sensitivity, PIVOT_SENSITIVITY["normal"])


def find_pivots(df: pd.DataFrame, order: int = None, sensitivity: str = "normal"):
    """ZigZag 枢轴点(交替高低点), 返回 [{idx,date,type,price}]

    sensitivity 为档位 ("fast"/"normal"/"safe", 见 PIVOT_SENSITIVITY),
    决定默认邻域半径; order 显式传入时优先 (兼容旧调用, 如周/月线 order=3)。
    后续调用可传 find_pivots(df) 或 find_pivots(df, sensitivity="safe")。
    纯 NumPy 实现, 替代 scipy.signal.argrelextrema, 去除 scipy 依赖。"""
    if order is None:
        order = pivot_order(sensitivity)
    n = len(df)
    if n < order * 3 + 3:
        return []
    high_v = df["high"].values
    low_v = df["low"].values
    day_v = df["day"].values
    
    # 纯 NumPy 局部极值检测: 滑动窗口比较
    max_idx = []
    min_idx = []
    for i in range(order, n - order):
        is_max = high_v[i] == high_v[i - order:i + order + 1].max()
        is_min = low_v[i] == low_v[i - order:i + order + 1].min()
        if is_max:
            max_idx.append(i)
        if is_min:
            min_idx.append(i)
    
    max_set = set(max_idx)
    min_set = set(min_idx)
    pivots = []
    for i in sorted(max_set | min_set):
        is_high = i in max_set
        pivots.append({
            "idx": int(i),
            "date": pd.Timestamp(day_v[i]),
            "type": "high" if is_high else "low",
            "price": float(high_v[i]) if is_high else float(low_v[i]),
        })
    # 交替过滤
    pf = [pivots[0]]
    for p in pivots[1:]:
        if p["type"] == pf[-1]["type"]:
            if (p["type"] == "high" and p["price"] > pf[-1]["price"]) or \
               (p["type"] == "low" and p["price"] < pf[-1]["price"]):
                pf[-1] = p
        else:
            pf.append(p)
    # 追加最新虚拟枢轴, 让最新行情参与阶段判断与支撑阻力
    last_close, prev_close = df["close"].values[-1], df["close"].values[-2]
    t = "high" if last_close >= prev_close else "low"
    pf.append({
        "idx": len(df) - 1,
        "date": pd.Timestamp(day_v[-1]),
        "type": t,
        "price": float(high_v[-1]) if t == "high" else float(low_v[-1]),
    })
    return pf
