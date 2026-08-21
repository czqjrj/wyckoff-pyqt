# -*- coding: utf-8 -*-
"""VSA 量价分析。

整合来源 (见 docs/vsa_sources/):
  - FibAlgo - Wyckoff Volume Spread Analysis (闭源, 按公开算法实现)
  - VSA Volume Spread Analysis [Advanced] (开源, 逐条移植)
  - Wyckoff-Pro (吸筹/派发结构已由 phases.py/events.py 覆盖, 不重复)

在原有 9 类标签 (SC/BC/SV/UT/SPR/ER/EF/ND/NS) 基础上新增:
  DEM 强势需求 / SUP 强势供给 / ABS 吸收 / CHOC 性质变化 / EVR 努力结果背离
  UPT 上冲量(诱多) / TEST 二次测试 / ETR 努力上涨 / ETF 努力下跌
  TRU 诱多陷阱 / TRD 诱空陷阱
一根 K 线仅输出优先级最高的一个标签 (信号优先级去重)。

量异常门: 除"量>均量×k"式量比分级外, 新增量 Z-score (vol_z_20, 成交量相对
20根均量/标准差的标准化偏离) 作为统计意义上的量异常/量高潮补充门 —— 对量级
与噪声自适应, 避免低波动窗口把普通放量误标成高潮; vsa_volume_anomaly 输出
独立量异常事件 (放量/缩量, anomaly/climax), 供结论与滚动回测使用。

阈值校准: 硬编码默认值见 DEFAULT_THRESHOLDS; 若存在校准配置
~/.wyckoff/wx_vsa_thresholds.json (由 calibrate_vsa.py --write 生成),
则按周期读取覆盖默认值 (缺失字段回退默认)。
"""
import json
import os

import numpy as np
import pandas as pd

from .config import VSA_COLOR
from .paths import DATA_DIR

VSA_THRESHOLDS_FILE = os.path.join(DATA_DIR, "wx_vsa_thresholds.json")

# 量级/幅宽阈值硬编码默认 (日线 240 / 120分钟 / 60分钟)。
# 校准锚点 (见 calibrate_vsa.py TARGET_PCT): vr_hi≈P90, vr_mid≈P75, v_high≈P85,
# v_ultra≈P97, v_low≈P15, v_low05≈P5, spr_wide≈P85, spr_narrow≈P15。
# z_anom/z_climax: 量 Z-score (vol_z_20) 档位 —— 统计意义上的量异常/量高潮,
# 比"量>均量×k"更稳健 (对量级/噪声自适应), 用作量级分级与 CHOC 高潮的补充门。
DEFAULT_THRESHOLDS = {
    240: {
        "vr_hi": 1.8, "vr_mid": 1.4,          # 高潮量/中量 (原 c1/c2 阈值)
        "v_high": 1.5, "v_ultra": 2.5, "v_low": 0.7,      # FibAlgo 量级分级
        "v_high15": 1.5, "v_low05": 0.5,      # VSA Advanced 量级
        "spr_wide_coef": 1.5, "spr_narrow_coef": 0.7,     # FibAlgo 宽幅/窄幅系数
        "cpos_hi": 0.75, "cpos_lo": 0.25,     # 原 SC/BC 收盘位置
        "extreme_hi": 0.75, "extreme_lo": 0.25,           # CHOC 极端收盘位置
        "z_anom": 2.0, "z_climax": 3.0,       # 量 Z-score 异常/高潮门
    },
    120: {
        "vr_hi": 2.0, "vr_mid": 1.6,
        "v_high": 1.5, "v_ultra": 2.5, "v_low": 0.7,
        "v_high15": 1.5, "v_low05": 0.5,
        "spr_wide_coef": 1.5, "spr_narrow_coef": 0.7,
        "cpos_hi": 0.75, "cpos_lo": 0.25,
        "extreme_hi": 0.75, "extreme_lo": 0.25,
        "z_anom": 2.0, "z_climax": 3.0,
    },
    60: {
        "vr_hi": 2.1, "vr_mid": 1.7,
        "v_high": 1.5, "v_ultra": 2.5, "v_low": 0.7,
        "v_high15": 1.5, "v_low05": 0.5,
        "spr_wide_coef": 1.5, "spr_narrow_coef": 0.7,
        "cpos_hi": 0.75, "cpos_lo": 0.25,
        "extreme_hi": 0.75, "extreme_lo": 0.25,
        "z_anom": 2.0, "z_climax": 3.0,
    },
}

_loaded = {}


def thresholds(scale: int) -> dict:
    """返回该周期的量级/幅宽阈值 dict。加载一次校准配置后缓存。"""
    if scale in _loaded:
        return _loaded[scale]
    base = dict(DEFAULT_THRESHOLDS.get(scale, DEFAULT_THRESHOLDS[240]))
    try:
        if os.path.exists(VSA_THRESHOLDS_FILE):
            with open(VSA_THRESHOLDS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            over = ((payload.get("scales") or {}).get(str(scale)) or {}).get("thresholds") or {}
            base.update(over)
    except Exception:
        pass
    _loaded[scale] = base
    return base

# 信号优先级 (数值越大越优先; 见 FibAlgo "Signal Prioritization" 设计)
_PRIORITY = {
    "CHOC": 100,   # 性质变化 = 阶段转换, 最强
    "UPT": 95,     # 上冲量 (诱多)
    "TRU": 90, "TRD": 90,   # 陷阱
    "SC": 85, "BC": 85,     # 高潮
    "DEM": 80, "SUP": 80,   # 强势需求/供给
    "SV": 75,               # 停止量
    "ABS": 70,              # 吸收
    "ETR": 65, "ETF": 65,   # 努力
    "TEST": 60,             # 二次测试
    "UT": 55, "SPR": 55,    # 原上冲量/弹簧量
    "EVR": 50,              # 努力/结果背离
    "ND": 45, "NS": 45,     # 无需求/无供给
    "ER": 40, "EF": 40,     # 宽幅无结果
}

_DESC = {
    "SC": "收于低位 → 卖出高潮", "BC": "收于高位 → 买入高潮",
    "SV": "宽幅中收 → 停止量", "UT": "上冲后收弱 → 上冲量",
    "SPR": "下探后收回 → 弹簧量", "ER": "幅宽但无结果",
    "EF": "幅宽但无结果", "ND": "无量上涨 → 无需求",
    "NS": "无量下跌 → 无供给",
    "DEM": "高量宽幅阳线收于近高 → 强势需求",
    "SUP": "高量宽幅阴线收于近低 → 强势供给",
    "ABS": "高量窄幅 → 努力无结果(吸收)",
    "CHOC": "最宽幅+超高量+逆势+收极端 → 性质变化(阶段转换)",
    "EVR": "低量宽幅 → 低努力高结果(努力/结果背离)",
    "UPT": "高量宽幅阴线突破前高后收于低端 → 上冲量(诱多)",
    "TEST": "低量阴线收于高端 → 二次测试",
    "ETR": "高量宽幅阳线收于最高 → 努力上涨",
    "ETF": "高量宽幅阴线收于最低 → 努力下跌",
    "TRU": "突破前10根高点但收弱 → 诱多陷阱",
    "TRD": "跌破前10根低点但收强 → 诱空陷阱",
}


def _trend_up(df, n=20):
    """20 根线性回归斜率判定上升趋势 (FibAlgo 趋势上下文)。向量化滚动最小二乘。"""
    close = df["close"].values.astype(float)
    j = np.arange(len(close), dtype=float)
    jj = j * j
    tjy = j * close
    s_j = pd.Series(j).rolling(n).sum().values
    s_y = pd.Series(close).rolling(n).sum().values
    s_jj = pd.Series(jj).rolling(n).sum().values
    s_tjy = pd.Series(tjy).rolling(n).sum().values
    denom = n * s_jj - s_j * s_j
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(np.abs(denom) > 1e-12, (n * s_tjy - s_j * s_y) / denom, np.nan)
    return slope > 0


def vsa_volume_anomaly(df: pd.DataFrame, scale: int = 240,
                       z_anom: float = None, z_climax: float = None) -> list:
    """量 Z-score 异常检测: 基于 vol_z_20 的统计量异常 (非"量>均量×k")。

    返回 [{idx,date,level,vol,z,dir,desc}]:
      level: "anomaly"(|z|≥z_anom) / "climax"(|z|≥z_climax, 量高潮)
    z>0 放量异常, z<0 缩量异常 (缩量吸筹/供应枯竭也具威科夫含义)。
    默认 z_anom=2.0 / z_climax=3.0, 可由校准配置覆盖 (thresholds())。"""
    th = thresholds(scale)
    za = th["z_anom"] if z_anom is None else z_anom
    zc = th["z_climax"] if z_climax is None else z_climax
    z = df["vol_z_20"].values
    n = len(df)
    out = []
    for i in range(n):
        v = float(z[i])
        if np.isnan(v) or abs(v) < za:
            continue
        row = df.iloc[i]
        level = "climax" if abs(v) >= zc else "anomaly"
        direc = "放量" if v > 0 else "缩量"
        out.append({
            "idx": i,
            "date": row["day"],
            "level": level,
            "vol": float(row["volume"]),
            "z": round(v, 2),
            "dir": "up" if v > 0 else "dn",
            "desc": f"量Z={v:.1f} {direc}异常",
        })
    return out


def vsa_classify(df: pd.DataFrame, scale: int = 240) -> list:
    """逐根K线 VSA 分类。基于量比/量Z-score、影线、收盘位置与趋势上下文。返回
    [{idx,date,label,color,desc}], 每根 K 线仅保留优先级最高的一个标签。
    scale: 周期(分钟)。日内周期量比分布更集中(短窗口均量噪声大), 需相应放宽
    阈值: 60分钟/120分钟在日线阈值上加量比下限 (2.0/1.9 vs 日线1.8), 避免把
    日内普通放量误标成高潮; 宽幅判定系数保持 (range 均值是短窗口自适应)。
    df 需含 vol_z_20 列 (add_indicators 提供); 缺失时按 z=0 处理 (不触发
    Z-score 门, 退化为纯量比分级, 保持旧行为)。
    """
    n = len(df)
    vr = df["vol_ratio_20"].values
    rng = np.where(df["range"].values > 1e-9, df["range"].values, 1e-9)
    cpos = (df["close"].values - df["low"].values) / rng
    body = df["body"].values
    direction = df["direction"].values
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    roll = df["range"].rolling(20).mean().shift(1).values
    roll = np.where(roll > 1e-9, roll, 1e-9)
    wide = df["range"].values > roll * 1.5

    # ── 周期校准: 日内量比更易上冲, 抬高高潮/冲击量阈值; 日线维持原值 ──
    # 阈值可被校准配置覆盖 (见 thresholds()); 缺失时回退硬编码默认。
    th = thresholds(scale)
    vr_hi, vr_mid = th["vr_hi"], th["vr_mid"]

    # ── 通用分类特征 ──
    up_bar = direction == 1
    dn_bar = direction == -1

    # ── FibAlgo: 量级/幅宽/收盘位置分级 (相对20根均量/均幅) ──
    # 量 Z-score 作为补充门: 统计意义上偏离均值越多越可信 (对量级/噪声自适应),
    # 避免仅靠"量>均量×k"在低波动窗口把普通放量误标成高潮。
    z = df["vol_z_20"].values if "vol_z_20" in df.columns \
        else np.zeros(n, dtype=float)
    z_up_hi = z >= th["z_anom"]      # 放量异常
    z_up_climax = z >= th["z_climax"]  # 放量高潮
    z_dn_low = z <= -th["z_anom"]    # 缩量异常 (供应枯竭/吸筹, VSA 亦关注)
    v_low = (vr <= th["v_low"]) | z_dn_low
    v_high = (vr >= th["v_high"]) | z_up_hi
    v_ultra = (vr >= th["v_ultra"]) | z_up_climax
    spread_ma = df["range"].rolling(20).mean().values  # 20根均幅 (ta.sma(spread,20))
    spread_ma = np.where(spread_ma > 1e-9, spread_ma, 1e-9)
    spr_narrow = rng < spread_ma * th["spr_narrow_coef"]
    spr_wide = rng > spread_ma * th["spr_wide_coef"]
    near_high = cpos >= 0.7
    near_low = cpos <= 0.3
    trend_up = _trend_up(df)
    trend_dn = ~trend_up
    v_huge = v_high | v_ultra

    # ── VSA Advanced 量级/幅宽阈值 (与 FibAlgo 同源均量/均幅, 阈值略不同) ──
    v_high15 = vr >= th["v_high15"]
    v_low05 = vr <= th["v_low05"]
    spr_wide_adv = rng > spread_ma  # spread > ta.sma(spread, length)

    # ── 各来源标签候选 (布尔掩码) ──
    cand = {}

    # 原有 9 类 (行为保持; 与新增候选按优先级统一去重)
    cpos_hi, cpos_lo = th["cpos_hi"], th["cpos_lo"]
    c1 = (vr >= vr_hi) & wide
    cand["SC"] = c1 & (cpos <= cpos_lo)
    cand["BC"] = c1 & (cpos >= cpos_hi)
    cand["SV"] = c1 & (cpos > cpos_lo) & (cpos < cpos_hi)
    c2 = (vr >= vr_mid) & wide & ~c1
    cand["UT"] = c2 & up_bar & (cpos <= 0.35)
    cand["SPR"] = c2 & dn_bar & (cpos >= 0.65)
    cand["ER"] = c2 & ~cand["UT"] & ~cand["SPR"] & (body < rng * 0.4) & up_bar
    cand["EF"] = c2 & ~cand["UT"] & ~cand["SPR"] & (body < rng * 0.4) & dn_bar
    c3 = (vr <= 0.6) & ~c1 & ~c2
    cand["ND"] = c3 & up_bar
    cand["NS"] = c3 & dn_bar

    # FibAlgo 5 类
    cand["DEM"] = up_bar & spr_wide & v_huge & near_high
    cand["SUP"] = dn_bar & spr_wide & v_huge & near_low
    cand["ABS"] = v_huge & spr_narrow
    cand["EVR"] = v_low & spr_wide          # 低努力高结果 (高努力低结果=ABS)
    # Stopping Volume (FibAlgo): 高量 + 收盘方向与趋势相反 + 已确立趋势
    cand["SV"] = v_huge & ((up_bar & trend_dn) | (dn_bar & trend_up))
    # Change of Character: 20根最宽幅 + 量接近20根最高 + 逆势 + 收极端
    rng_max20 = pd.Series(rng).rolling(20).max().values
    vol_max20 = pd.Series(vol).rolling(20).max().values
    widest20 = rng >= rng_max20 * 0.999
    near_peak_vol = (vol >= vol_max20 * 0.9) | z_up_climax
    extreme = (cpos <= th["extreme_lo"]) | (cpos >= th["extreme_hi"])
    cand["CHOC"] = (widest20 & near_peak_vol & extreme
                    & ((up_bar & trend_dn) | (dn_bar & trend_up)))

    # VSA Advanced 10 类 (逐条移植)
    # UPT (上冲量/诱多): 在 VSA Advanced 原始"高量宽幅阴线收于低端"基础上,
    # 补充"突破前10根高点"约束 —— 真正意义的上冲量是突破阻力失败 (诱多),
    # 与普通强势供给 SUP / 努力下跌 ETF 区分, 避免被高优先级覆盖成为死标签。
    prev_hi_max = pd.Series(high).rolling(10).max().shift(1).values  # ta.highest(high[1],10)
    prev_lo_min = pd.Series(low).rolling(10).min().shift(1).values   # ta.lowest(low[1],10)
    cand["UPT"] = (v_high15 & dn_bar & (close <= low + rng * 0.3) & spr_wide_adv
                   & (high > prev_hi_max))
    cand["ND"] = cand.get("ND") | (v_low05 & up_bar & (close >= high - rng * 0.3)
                                   & (rng < spread_ma))
    cand["SV"] = cand["SV"] | (dn_bar & v_high15 & (close > low + rng * 0.3))
    cand["TEST"] = dn_bar & v_low05 & (close >= high - rng * 0.3)
    cand["BC"] = cand["BC"] | (up_bar & v_high15 & (close < high - rng * 0.3)
                               & spr_wide_adv)
    cand["NS"] = cand.get("NS") | (v_low05 & dn_bar & (close <= low + rng * 0.3)
                                   & (rng < spread_ma))
    cand["ETR"] = up_bar & v_high15 & (close >= high - rng * 0.2) & spr_wide_adv
    cand["ETF"] = dn_bar & v_high15 & (close <= low + rng * 0.2) & spr_wide_adv
    cand["TRU"] = (high > prev_hi_max) & (close < high - rng * 0.3) & v_high15
    cand["TRD"] = (low < prev_lo_min) & (close > low + rng * 0.3) & v_high15

    # ── 信号优先级去重: 每根 K 线取优先级最高的标签 ──
    labels = np.full(n, "N", dtype=object)
    best = np.full(n, -1, dtype=float)
    for k, mask in cand.items():
        p = _PRIORITY[k]
        upd = mask & (p > best)
        labels[upd] = k
        best[upd] = p

    out = []
    for i in range(30, n):
        lb = labels[i]
        if lb == "N":
            continue
        row = df.iloc[i]
        # features: 量比/波幅比/收盘位置/趋势 (供准确度分析)
        ma20 = df["price_ma20"].values if "price_ma20" in df.columns else None
        ma50 = df["price_ma50"].values if "price_ma50" in df.columns else None
        trend = 0
        if ma20 is not None and ma50 is not None and i < len(ma20) and i < len(ma50):
            trend = 1 if (np.isfinite(ma20[i]) and np.isfinite(ma50[i])
                          and ma20[i] > ma50[i] and close[i] > ma50[i]) else 0
        rw_val = float(rng[i] / roll[i]) if np.isfinite(roll[i]) and roll[i] > 1e-9 else 1.0
        out.append({"idx": i, "date": row["day"], "label": lb,
                    "color": VSA_COLOR[lb],
                    "desc": f"量{vr[i]:.1f}x {_DESC.get(lb, lb)}",
                    "features": {
                        "vr": round(float(vr[i]), 4),
                        "rw": round(rw_val, 4),
                        "cpos": round(float(cpos[i]), 4),
                        "trend": int(trend),
                        "dir": int(1 if up_bar[i] else (-1 if dn_bar[i] else 0)),
                    }})
    return out
