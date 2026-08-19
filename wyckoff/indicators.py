# -*- coding: utf-8 -*-
"""技术指标与 ZigZag 枢轴点。"""
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


def _limit_pct(symbol) -> float:
    """按板块返回近似涨跌停阈值 (含阈值内回旋余量):
    创业板(30x)/科创板(68x) 20%, 北交所(8x/4x) 30%, 其余主板 10%。"""
    code6 = (symbol or "")[-6:]
    if code6.startswith(("30", "68")):
        return 0.199
    if code6.startswith(("8", "4")):
        return 0.299
    return 0.099


def add_indicators(df: pd.DataFrame, symbol: str = None) -> pd.DataFrame:
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["direction"] = np.where(df["close"] >= df["open"], 1, -1)
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma10"] = df["volume"].rolling(10).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ma50"] = df["volume"].rolling(50).mean()
    df["vol_ratio_20"] = df["volume"] / df["vol_ma20"]
    # 量 Z-score: 成交量相对 20 根滚动均值/标准差的标准化偏离。
    # 比"量>均量×k"更稳健的量异常度量 (对量级/噪声自适应), 供 VSA 量异常
    # 检测与 CHOC 高潮判定使用; 均量恒定时退化为 0 (无异常)。
    _vol_std20 = df["volume"].rolling(20).std()
    df["vol_z_20"] = (df["volume"] - df["vol_ma20"]) / _vol_std20.replace(0, np.nan)
    df["price_ma5"] = df["close"].rolling(5).mean()
    df["price_ma10"] = df["close"].rolling(10).mean()
    df["price_ma20"] = df["close"].rolling(20).mean()
    df["price_ma50"] = df["close"].rolling(50).mean()
    df["price_ma200"] = df["close"].rolling(200).mean()
    # ATR(14): 用于止损/仓位参考
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    # BOLL (20, 2): 中轨=MA20, 上下轨=±2σ
    df["boll_mid"] = df["price_ma20"]
    df["boll_std"] = df["close"].rolling(20).std()
    df["boll_up"] = df["price_ma20"] + 2 * df["boll_std"]
    df["boll_dn"] = df["price_ma20"] - 2 * df["boll_std"]
    # MACD (12, 26, 9)
    _e12 = df["close"].ewm(span=12, adjust=False).mean()
    _e26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"] = _e12 - _e26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd_dif"] - df["macd_dea"]) * 2
    # KDJ (9, 3, 3)
    _low9 = df["low"].rolling(9).min()
    _high9 = df["high"].rolling(9).max()
    _rsv = (df["close"] - _low9) / (_high9 - _low9).replace(0, np.nan) * 100
    df["kdj_k"] = _rsv.ewm(com=2, adjust=False).mean()
    df["kdj_d"] = df["kdj_k"].ewm(com=2, adjust=False).mean()
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
    # RSI (6, 12, 24)
    _diff = df["close"].diff()
    for _n in (6, 12, 24):
        _up = _diff.clip(lower=0).rolling(_n).mean()
        _dn = (-_diff.clip(upper=0)).rolling(_n).mean()
        _rs = _up / _dn.replace(0, np.nan)
        df[f"rsi_{_n}"] = 100 - 100 / (1 + _rs)
    # OBV 能量潮
    df["obv"] = (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()
    # 涨跌停/一字板近似标记 (无法正常成交的 bar, 供回测剔除与风控提示)。
    # 阈值按板块区分, 避免把创业板/科创板 10%~20% 涨幅的非涨停 bar 误标。
    pct = df["close"].pct_change()
    lim = _limit_pct(symbol)
    df["limit_up"] = (df["close"] >= df["high"] - 1e-9) & (pct >= lim - 0.005)
    df["limit_dn"] = (df["close"] <= df["low"] + 1e-9) & (pct <= -(lim - 0.005))
    df["locked"] = df["limit_up"] | df["limit_dn"]
    # 上市时长标记: 次新股 (<120根历史) 结构未成型, 阶段/枢轴判断不可靠。
    # 日线约半年, 提示"次新股勿按长期结构交易"; 供结论/风控降权参考。
    # 注意: 若调用方只请求短窗口 (datalen<120), 无法区分"次新"与"窗口短",
    # 此标记仅当总历史足够长时才有意义, 故用请求窗口长度近似。
    df["is_new_stock"] = len(df) < 120
    return df


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
    """
    if order is None:
        order = pivot_order(sensitivity)
    n = len(df)
    if n < order * 3 + 3:
        return []
    max_idx = argrelextrema(df["high"].values, np.greater, order=order)[0]
    min_idx = argrelextrema(df["low"].values, np.less, order=order)[0]
    max_set = set(max_idx.tolist())
    min_set = set(min_idx.tolist())
    high_v = df["high"].values
    low_v = df["low"].values
    day_v = df["day"].values
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
