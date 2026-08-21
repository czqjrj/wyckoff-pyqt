# -*- coding: utf-8 -*-
"""综合选股引擎: 威科夫阶段 + 基本面 + 资金流 + 技术指标多维评分。

筛选流程:
  1. 获取选股宇宙 (全市场活跃股 / 自定义列表)
  2. 基本面预筛 (PE/PB/市值, 逐只调用, 有缓存时较快)
  3. 逐只威科夫分析 + 资金流 + 技术指标 (需K线, 秒级)
  4. 多维加权评分 → 排序返回

评分维度 (总分 100):
  威科夫阶段/信号  40分   — 吸筹/上升加分, Spring/SC等强信号加分
  技术面          25分   — 多头排列/低波动/MACD金叉/RSI适中
  资金流          20分   — 20日主力净流入占比 + 趋势
  基本面          15分   — PE合理/低PB/净利增长

全部 fail-soft: 数据不足/网络异常 → 该项 0 分, 不阻塞。
"""
import json
import os
import threading
import time

import numpy as np

from .backtest import signal_score as _wyckoff_signal_score
from .datasource import fetch_kline
from ._log import log_exc
from .indicators import add_indicators
from .phases import judge_phase
from .events import detect_all
from .indicators import find_pivots
from .paths import ALL_STOCKS_FILE, STOCK_NAMES_FILE
from .utils import normalize_symbol

# 基本面/资金流依赖 (延迟导入, 避免循环)
_fundamental = None
_filters = None


def _ensure_fund():
    global _fundamental
    if _fundamental is None:
        from . import fundamental
        _fundamental = fundamental
    return _fundamental


def _ensure_filters():
    global _filters
    if _filters is None:
        from . import filters
        _filters = filters
    return _filters


# 本地股票名称缓存 (代码→名称)
_STOCK_NAME_CACHE = None


def _load_stock_names():
    """加载本地股票名称缓存, 返回 {code: name} 字典。"""
    global _STOCK_NAME_CACHE
    if _STOCK_NAME_CACHE is not None:
        return _STOCK_NAME_CACHE
    names = {}
    for f in [STOCK_NAMES_FILE, ALL_STOCKS_FILE]:
        try:
            if os.path.exists(f):
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    for k, v in data.items():
                        if isinstance(v, dict) and "name" in v:
                            names[k] = v["name"]
                        elif isinstance(v, str):
                            names[k] = v
        except Exception:
            pass
    _STOCK_NAME_CACHE = names
    return names


def _get_stock_name(code):
    """从本地缓存获取股票名称, 失败返回空串。

    仅对真实个股做前缀回退: 指数 (sh000001/399xxx) 与 ETF (51/15/58 开头) 的
    6位码恰好与个股撞号 (如 000001=平安银行), 一旦误用会张冠李戴, 故直接跳过。
    """
    names = _load_stock_names()
    name = names.get(code, "")
    if name:
        return name
    # 前缀回退只对真实个股: 6位码的市场前缀须与代码前缀一致
    # (sh000001 是上证指数, 不能命中 sz000001 平安银行)
    if len(code) == 8 and code[:2] in ("sh", "sz", "bj"):
        c6 = code[2:]
        expect = ("sh" if c6[0] in "65" else "sz" if c6[0] in "023"
                  else "bj" if c6[0] in "489" else "")
        if expect == code[:2]:
            name = names.get(c6, "")
    return name


def _run_parallel(items, fn, workers=6, cancel_event=None):
    """并行执行 fn(x) → 返回 [(x, result), ...]。

    网络IO密集任务用线程池显著提速; 单任务异常 → result=None (不抛出)。
    cancel_event: threading.Event, set 后不再派发新任务 (进行中的会自然结束)。
    """
    if not items:
        return []
    if cancel_event is not None and cancel_event.is_set():
        return []
    if workers <= 1 or len(items) <= 1:
        return [(x, fn(x)) for x in items]
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return [(x, fn(x)) for x in items]
    out = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fn, x): x for x in items}
            for fut in as_completed(futures):
                if cancel_event is not None and cancel_event.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                x = futures[fut]
                try:
                    out.append((x, fut.result()))
                except Exception:
                    out.append((x, None))
    except Exception as e:
        log_exc("并行扫描框架异常", e)
    return out


# ──────────────────────────── 评分权重 ────────────────────────────

# 威科夫阶段基础分
_PHASE_BASE = {
    "底部整固": 25,   # 吸筹阶段, 最具潜力
    "上升趋势": 15,   # 已在上升, 追高风险
    "顶部构筑": -5,   # 派发阶段
    "下跌趋势": -10,  # 下跌阶段
}

# 威科夫信号加分 (静态表: 仅在信号库无实证数据时回退使用)
_SIGNAL_BONUS = {
    "Spring": 15,     # 最强买入信号 (弹簧效应)
    "SC": 10,         # 抛售高潮
    "ST": 8,          # 二次测试
    "PSY": 5,         # 初始支撑
    "SOS": 5,         # 力量彰显
    "JOC": 5,         # 跳跃穿越
    "LPS": 3,         # 最后回踩
    "AR": 3,          # 自动反弹
    "UTAD": -8,       # 派发试探
    "BC": -5,         # 买入高潮 (顶部)
    "BU": 3,          # 回测支撑
}

# ── 实证有效信号门槛: 综合选股只认实测胜率超过该值的信号 ──
# 实测 (20根方向化口径): Spring/Shakeout/ST/LPS/SC >60% 有效;
# SOS/JOC/BU/AR/PSY/BC ≈ 随机 → 不再参与选股加分与筛选。
VALID_WINRATE = 0.60


def empirical_signal_rates(horizon=20, threshold=VALID_WINRATE):
    """从本机信号准确度库计算实测有效的威科夫事件集合。

    返回 {"long": {类型: 收缩胜率}, "bear": {类型: 收缩胜率}}:
      long: 多头事件收缩胜率>threshold; 中性事件需胜率>threshold 且均收益>0
      bear: 空头事件收缩胜率>threshold (方向正确但利空 → 选股扣分)
    信号库无数据返回 {} (调用方回退静态 _SIGNAL_BONUS)。
    """
    from .config import event_dir
    from .signal_accuracy import load_win_rates
    rates = load_win_rates(horizon)
    if not rates:
        return {}
    out = {"long": {}, "bear": {}}
    for (kind, typ), rec in rates.items():
        if kind != "event":
            continue
        shrunk = float(rec.get("shrunk") or 0.0)
        if shrunk <= threshold:
            continue
        d = event_dir(typ)
        if d > 0:
            out["long"][typ] = shrunk
        elif d < 0:
            out["bear"][typ] = shrunk
        elif float(rec.get("mean") or 0.0) > 0:
            # 中性事件 (SC 等): 命中口径即上涨占比, 均收益为正才视为多头有效
            out["long"][typ] = shrunk
    return out


def _signal_points(signals, emp_rates):
    """实证信号加分: 只统计实测胜率>阈值的信号。

    多头有效信号按优势幅度加分 ((胜率-50%)×100÷2, 与静态表量级对齐:
    Spring≈+17 / Shakeout≈+11 / ST≈+9); 空头有效信号同幅扣分;
    胜率≤阈值或无实证的信号一律 0 分。emp_rates 为空 dict → 返回 None
    (调用方回退静态表)。
    """
    if not emp_rates:
        return None

    def _best(mapping):
        pts = 0
        for s in set(signals or []):
            r = mapping.get(s)
            if r is not None:
                pts = max(pts, int(round((r - 0.5) * 100 / 2)))
        return pts

    return _best(emp_rates["long"]) - _best(emp_rates["bear"])

# 市值区间偏好 (中小盘略加分, 大盘中性)
_CAP_RANGE = {
    "micro": (0, 50, 3),       # <50亿 小盘
    "small": (50, 200, 2),     # 50-200亿
    "mid": (200, 1000, 1),     # 200-1000亿
    "large": (1000, 5000, 0),  # 1000-5000亿
    "mega": (5000, 999999, -1),  # >5000亿 超大盘
}


# ──────────────────────────── 快速预筛 ────────────────────────────

def quick_fundamental_filter(codes, filters, workers=6, cancel_event=None):
    """阶段一: 并行基本面预筛 (有缓存时较快, 首次较慢)。

    filters: {
        "mcap_min": 最小市值(亿), "mcap_max": 最大市值(亿),
        "pe_min": 最小PE, "pe_max": 最大PE,
        "pb_min": 最小PB, "pb_max": 最大PB,
    }
    workers: 并行线程数 (网络IO密集)。cancel_event: threading.Event, set 后
    不再派发新任务。
    返回通过预筛的 code 列表。
    """
    if not filters or not codes:
        return codes
    fund = _ensure_fund()
    # 预先判断是否有筛选条件, 避免循环内重复计算
    has_cap = filters.get("mcap_min") or filters.get("mcap_max")
    has_pe = filters.get("pe_min") is not None or filters.get("pe_max") is not None
    has_pb = filters.get("pb_min") is not None or filters.get("pb_max") is not None
    if not (has_cap or has_pe or has_pb):
        return codes

    def _check(c):
        sym = ""
        try:
            sym = normalize_symbol(c)
        except ValueError:
            return c  # 无法归一化 → 放行
        # 逐只快速取基本面 (有缓存, 首次 ~200ms, 后续 ~0ms)
        try:
            f = fund.fetch_fundamental(sym)
            if f is None:
                return c  # 无法获取 → 放行 (不误杀)
            mcap = f.get("mcap_yi", 0)
            pe = f.get("pe_ttm", 0)
            pb = f.get("pb", 0)
            if has_cap:
                lo = filters.get("mcap_min", 0) or 0
                hi = filters.get("mcap_max", 999999) or 999999
                if not (lo <= mcap <= hi):
                    return None
            if has_pe and pe > 0:
                lo = filters.get("pe_min", 0) if filters.get("pe_min") is not None else 0
                hi = filters.get("pe_max", 9999) if filters.get("pe_max") is not None else 9999
                if not (lo <= pe <= hi):
                    return None
            if has_pb and pb > 0:
                lo = filters.get("pb_min", 0) if filters.get("pb_min") is not None else 0
                hi = filters.get("pb_max", 999) if filters.get("pb_max") is not None else 999
                if not (lo <= pb <= hi):
                    return None
            return c
        except Exception:
            return c  # 异常 → 放行

    out = _run_parallel(codes, _check, workers=workers, cancel_event=cancel_event)
    return [c for c, ok in out if ok]


# ──────────────────────────── 单只评分 ────────────────────────────

def score_stock(code, datalen=500):
    """对单只股票做完整分析并返回多维评分。

    返回 {
        "code", "name", "last", "phase", "phase_base",
        "signals", "signals_valid", "signal_bonus", "conf_q",
        "flow20", "flow20_pct", "flow_trend", "flow_score",
        "pe", "pb", "mcap_yi", "net_growth", "fund_score",
        "ma_arrangement", "vol_state", "rsi", "macd_hist", "tech_score",
        "total_score", "sector", "sector20",
        "error": None 或 错误信息
    }
    """
    fund = _ensure_fund()
    flt = _ensure_filters()
    result = {
        "code": code, "name": "", "last": None, "phase": "",
        "phase_base": "", "signals": [], "signals_valid": [],
        "signal_bonus": 0, "conf_q": "",
        "flow20": None, "flow20_pct": None, "flow_trend": "", "flow_score": 0,
        "pe": None, "pb": None, "mcap_yi": None, "net_growth": None,
        "fund_score": 0,
        "ma_arrangement": "", "vol_state": "", "rsi": None, "macd_hist": None,
        "tech_score": 0, "total_score": 0, "sector": None, "sector20": None,
        "error": None,
    }
    try:
        symbol = normalize_symbol(code)
    except ValueError as e:
        result["error"] = str(e)
        return result

    # ── 1. K线 + 威科夫 ──
    try:
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        df = add_indicators(df, symbol=symbol)
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _ = judge_phase(df, pivots, events)
        base = phase.split(" ")[0] if phase else ""
        result["phase"] = phase
        result["phase_base"] = base
        result["last"] = float(df["close"].iloc[-1])
        # 近20根信号
        recent = [e for e in events if e["idx"] >= len(df) - 20]
        result["signals"] = [e["type"] for e in recent]
        # 威科夫评分: 只认实测胜率>阈值的信号 (无实证数据时回退静态表)
        emp = empirical_signal_rates()
        pts = _signal_points(result["signals"], emp)
        if pts is None:
            phase_pts = _PHASE_BASE.get(base, 0)
            sig_pts = max([_SIGNAL_BONUS.get(s, 0) for s in result["signals"]] + [0])
            sig_pts += sum(_SIGNAL_BONUS.get(s, 0) for s in result["signals"]
                           if _SIGNAL_BONUS.get(s, 0) < 0)
            result["signal_bonus"] = phase_pts + sig_pts
            result["signals_valid"] = []
        else:
            result["signal_bonus"] = _PHASE_BASE.get(base, 0) + pts
            result["signals_valid"] = sorted(
                {s for s in result["signals"] if s in emp["long"]})
    except Exception as e:
        result["error"] = f"K线分析失败: {e}"
        return result

    # ── 2. 技术指标评分 ──
    try:
        tech = _score_technical(df)
        result["tech_score"] = tech["score"]
        result["ma_arrangement"] = tech["arrangement"]
        result["vol_state"] = tech["vol_state"]
        result["rsi"] = tech["rsi"]
        result["macd_hist"] = tech["macd_hist"]
    except Exception:
        result["tech_score"] = 0

    # ── 3. 基本面 ──
    try:
        f = fund.fetch_fundamental(symbol)
        if f:
            result["name"] = f.get("name", "")
            result["pe"] = f.get("pe_ttm")
            result["pb"] = f.get("pb")
            result["mcap_yi"] = f.get("mcap_yi")
            result["net_growth"] = f.get("net_growth")
            result["fund_score"] = _score_fundamental(f)
        # 如果名称为空, 从本地缓存获取
        if not result["name"]:
            result["name"] = _get_stock_name(code)
    except Exception:
        result["fund_score"] = 0
        # 如果名称为空, 从本地缓存获取
        if not result["name"]:
            result["name"] = _get_stock_name(code)

    # ── 4. 资金流 ──
    try:
        flow = fund.fetch_main_flow(symbol, 120)
        if flow is not None and len(flow) >= 20:
            f20 = float(flow.tail(20)["main"].sum())
            result["flow20"] = f20 / 1e8  # 亿
            # 占成交额比
            df20 = df.tail(20)
            amt = float((df20["volume"] * 100 * df20["close"]).sum())
            result["flow20_pct"] = f20 / amt * 100 if amt > 0 else 0
            # 趋势
            f5 = float(flow.tail(5)["main"].sum())
            if f5 > 0 and f20 > 0:
                result["flow_trend"] = "加速流入"
            elif f5 > 0 and f20 < 0:
                result["flow_trend"] = "流出趋缓"
            elif f5 < 0 and f20 < 0:
                result["flow_trend"] = "加速流出"
            elif f5 < 0 and f20 > 0:
                result["flow_trend"] = "流入减缓"
            else:
                result["flow_trend"] = "平稳"
            result["flow_score"] = _score_flow(result["flow20_pct"], result["flow_trend"])
        # 板块资金流
        try:
            s_name, s_flow = fund.fetch_sector_flow(symbol)
            if s_name:
                result["sector"] = s_name
            if s_flow is not None and len(s_flow) >= 20:
                s_main = float(s_flow.tail(20)["main"].sum())
                result["sector20"] = s_main / 1e8
        except Exception:
            pass
    except Exception:
        result["flow_score"] = 0

    # ── 5. 综合评分 ──
    result["total_score"] = (
        max(0, min(40, 20 + result["signal_bonus"])) +  # 威科夫 0~40
        max(0, min(25, result["tech_score"])) +           # 技术 0~25
        max(0, min(20, result["flow_score"])) +            # 资金 0~20
        max(0, min(15, result["fund_score"]))              # 基本面 0~15
    )
    # 负信号惩罚: 高位派发 + 强资金流出 → 降权
    if result["phase_base"] in ("顶部构筑", "下跌趋势"):
        result["total_score"] = max(0, result["total_score"] - 15)
    if result["flow20"] is not None and result["flow20"] < -1:
        result["total_score"] = max(0, result["total_score"] - 5)

    return result


# ──────────────────────────── 评分辅助 ────────────────────────────

def _score_technical(df):
    """技术面评分 (满分25)。"""
    score = 0
    arrangement = ""
    vol_state = ""
    rsi_val = None
    macd_h = None
    try:
        c = df["close"].values
        m5 = df["price_ma5"].values
        m20 = df["price_ma20"].values
        m50 = df["price_ma50"].values
        i = -1
        if np.isfinite(m50[i]):
            if c[i] > m5[i] > m20[i] > m50[i]:
                arrangement = "多头排列"
                score += 8
            elif c[i] < m5[i] < m20[i] < m50[i]:
                arrangement = "空头排列"
                score -= 3
            else:
                arrangement = "均线纠缠"
            # MA20 上行
            slope = m20[i] - m20[max(i - 5, 0)]
            if slope > 0.001:
                score += 3
            elif slope < -0.001:
                score -= 1
    except Exception:
        pass
    # RSI
    if "rsi_12" in df.columns:
        rsi_val = float(df["rsi_12"].iloc[-1]) if np.isfinite(df["rsi_12"].iloc[-1]) else None
        if rsi_val is not None:
            if 30 <= rsi_val <= 60:
                score += 4  # 超卖回升区, 佳
            elif 20 <= rsi_val < 30:
                score += 3  # 超卖
            elif 60 < rsi_val <= 80:
                score += 2  # 偏强
            elif rsi_val > 80:
                score -= 2  # 超买风险
            elif rsi_val < 20:
                score += 1  # 深度超卖
    # MACD 金叉/死叉
    if "macd_dif" in df.columns and "macd_dea" in df.columns:
        dif = df["macd_dif"].values
        dea = df["macd_dea"].values
        macd_h = float(df["macd_hist"].iloc[-1]) if np.isfinite(df["macd_hist"].iloc[-1]) else None
        i = -1
        if len(dif) > 1:
            if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
                score += 5  # 金叉
            elif dif[i] > dea[i]:
                score += 2  # 多头持续
            elif dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
                score -= 3  # 死叉
    # 布林带宽 (低波动蓄势加分)
    if "boll_up" in df.columns and "boll_dn" in df.columns and "boll_mid" in df.columns:
        up = float(df["boll_up"].iloc[-1])
        dn = float(df["boll_dn"].iloc[-1])
        mid = float(df["boll_mid"].iloc[-1])
        if mid > 0 and up > dn:
            bw = (up - dn) / mid * 100
            lookback = df.tail(120)
            bws = (lookback["boll_up"] - lookback["boll_dn"]) / lookback["boll_mid"].replace(0, np.nan) * 100
            bws = bws.dropna()
            if len(bws) > 0:
                pct = float((bws < bw).mean()) * 100
                if pct < 30:
                    vol_state = "低波动蓄势"
                    score += 5
                elif pct > 70:
                    vol_state = "高波动"
                    score -= 2
                else:
                    vol_state = "正常波动"
    return {
        "score": max(0, min(25, score)),
        "arrangement": arrangement,
        "vol_state": vol_state,
        "rsi": rsi_val,
        "macd_hist": macd_h,
    }


def _score_fundamental(fund):
    """基本面评分 (满分15)。"""
    score = 0
    pe = fund.get("pe_ttm", 0)
    pb = fund.get("pb", 0)
    growth = fund.get("net_growth")
    # PE
    if pe and pe > 0:
        if pe < 20:
            score += 5  # 低估
        elif pe < 35:
            score += 3  # 合理
        elif pe < 60:
            score += 1  # 偏高
        else:
            score -= 2  # 高估
    elif pe and pe < 0:
        score -= 3  # 亏损
    # PB
    if pb and pb > 0:
        if pb < 1.5:
            score += 3  # 低PB / 破净
        elif pb < 3:
            score += 2
        elif pb > 6:
            score -= 1  # 高PB
    # 净利增长
    if growth is not None:
        if growth > 0.3:
            score += 4  # 高增长
        elif growth > 0.1:
            score += 3
        elif growth > 0:
            score += 1
        elif growth > -0.2:
            score -= 1
        else:
            score -= 3  # 大幅下滑
    # 市值偏好
    mcap = fund.get("mcap_yi", 0)
    if mcap:
        for _label, (lo, hi, pts) in _CAP_RANGE.items():
            if lo <= mcap < hi:
                score += pts
                break
    return max(0, min(15, score))


def _score_flow(flow_pct, trend):
    """资金流评分 (满分20)。"""
    score = 10  # 基线
    if flow_pct is None:
        return 10
    # 净流入占比
    if flow_pct > 1.0:
        score += 8
    elif flow_pct > 0.5:
        score += 6
    elif flow_pct > 0:
        score += 3
    elif flow_pct > -0.5:
        score -= 1
    elif flow_pct > -1.0:
        score -= 4
    else:
        score -= 7
    # 趋势加成
    if trend == "加速流入":
        score += 3
    elif trend == "流出趋缓":
        score += 1
    elif trend == "加速流出":
        score -= 3
    elif trend == "流入减缓":
        score -= 1
    return max(0, min(20, score))


# ──────────────────────────── 批量选股 ────────────────────────────

def _apply_filters(r, filters):
    """阶段/信号/板块/最低分过滤 + 信号软加分。返回是否入选。"""
    # 阶段白名单
    phase_filter = filters.get("phases")
    if phase_filter and r["phase_base"] not in phase_filter:
        return False
    # 信号白名单: 只对实测胜率>阈值的信号生效 (signals_valid;
    # 无实证数据时回退全部检测信号, 保持旧行为)
    sig_filter = filters.get("signals")
    if sig_filter:
        pool = r.get("signals_valid")
        if pool is None:
            pool = r["signals"]
        matched = [s for s in pool if s in sig_filter]
        if filters.get("signals_mode", "any") == "any":
            if not matched:
                return False  # 硬过滤: 任一命中才入选
        elif matched:
            r["total_score"] += len(matched) * 5  # 软过滤: 每命中一个信号 +5 分
    # 板块白名单
    sector_filter = filters.get("sector")
    if sector_filter and not (r.get("sector") and r["sector"] in sector_filter):
        return False
    # 最低总分
    min_score = filters.get("min_score")
    if min_score and r.get("total_score", 0) < min_score:
        return False
    return True


def screen_stocks(codes, filters=None, on_progress=None, workers=6,
                  cancel_event=None, on_error=None):
    """综合选股: 对 codes 列表做多维评分, 返回按总分降序的结果列表。

    filters: {
        "mcap_min", "mcap_max", "pe_min", "pe_max", "pb_min", "pb_max",
        "phases": ["底部整固", "上升趋势", ...],  # 阶段白名单
        "signals": ["Spring", "SC", ...],  # 信号筛选 (见 signals_mode)
        #   匹配池为 signals_valid (实测胜率>60% 的信号); 无实证数据时
        #   回退全部检测信号。胜率≤阈值的信号 (如 SOS/JOC/PSY) 不再命中。
        "signals_mode": "any"|"soft",  # any=硬过滤(任一命中才入选, 默认) soft=匹配加分
        "sector": ["板块名", ...],  # 板块白名单 (任一命中)
        "min_score": 0,  # 最低总分
        "sort_by": "total_score"|"flow_score"|"tech_score"|"fund_score",
        "limit": 50,  # 最多返回
    }
    on_progress(done, total, current_code) 可选回调, 仅主循环线程触发 (单调递增)。
    on_error(code) 可选回调: 单只分析失败时通知 (用于统计跳过数)。
    workers: 并行工作线程数 (网络IO密集, 默认 6)。
    cancel_event: threading.Event, set 后尽快停止, 返回已完成的 partial 结果。
    返回 [{...score_stock结果...}, ...] 按 total_score 降序。
    """
    if not codes:
        return []
    filters = filters or {}
    limit = filters.get("limit", 50)

    # 阶段一: 并行基本面预筛
    codes = quick_fundamental_filter(codes, filters, workers=workers,
                                     cancel_event=cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        return []
    total = len(codes)
    if total == 0:
        return []

    # 阶段二: 逐只完整分析 (串行 or 并行)
    results = []
    results_lock = threading.Lock()

    def _handle(r):
        if r.get("error"):
            if on_error:
                on_error(r.get("code", ""))
            return
        if _apply_filters(r, filters):
            with results_lock:
                results.append(r)

    if workers <= 1 or total <= 1:
        # 串行路径
        done = 0
        for c in codes:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                r = score_stock(c, datalen=500)
            except Exception:
                r = {"code": c, "error": "异常"}
            done += 1
            _handle(r)
            if on_progress:
                on_progress(done, total, c)
    else:
        # 并行路径: 回调仍只由主循环线程触发, 保证进度单调
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(score_stock, c, 500): c for c in codes}
            for fut in as_completed(futures):
                if cancel_event is not None and cancel_event.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                c = futures[fut]
                try:
                    r = fut.result()
                except Exception:
                    r = {"code": c, "error": "异常"}
                done += 1
                _handle(r)
                if on_progress:
                    on_progress(done, total, c)

    # 排序
    sort_key = filters.get("sort_by", "total_score")
    results.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    return results[:limit]


# ──────────────────────────── 预设策略 ────────────────────────────

PRESET_STRATEGIES = {
    "value_accumulation": {
        "name": "价值吸筹",
        "desc": "低估值+吸筹阶段+资金流入 → 底部布局",
        "filters": {
            "pe_max": 35, "pb_max": 4,
            "phases": ["底部整固"],
            # 只含实测胜率>60%的多头信号 (SOS 48%/PSY 42% 已剔除)
            "signals": ["Spring", "Shakeout", "SC", "ST", "LPS"],
            "sort_by": "total_score", "limit": 30,
        },
    },
    "momentum_breakout": {
        "name": "强势突破",
        "desc": "上升趋势+多头排列+资金加速流入 → 顺势做多",
        "filters": {
            "phases": ["上升趋势", "底部整固"],
            "sort_by": "tech_score", "limit": 30,
        },
    },
    "oversold_bounce": {
        "name": "超跌反弹",
        "desc": "下跌/派发末期+RSI超卖+资金回流 → 抢反弹",
        "filters": {
            "pe_max": 50,
            "phases": ["下跌趋势", "顶部构筑", "底部整固"],
            "sort_by": "total_score", "limit": 30,
        },
    },
    "small_cap_growth": {
        "name": "小盘成长",
        "desc": "市值<200亿+净利高增+吸筹信号 → 成长股挖掘",
        "filters": {
            "mcap_max": 200, "pe_max": 50,
            "sort_by": "fund_score", "limit": 30,
        },
    },
    "fund_flow": {
        "name": "主力抢筹",
        "desc": "20日主力强净流入+吸筹/上升阶段 → 跟随主力",
        "filters": {
            "phases": ["底部整固", "上升趋势"],
            "sort_by": "flow_score", "limit": 30,
        },
    },
}


def get_preset(name):
    """获取预设策略。"""
    return PRESET_STRATEGIES.get(name)


def list_presets():
    """列出所有预设策略。"""
    return [{"key": k, "name": v["name"], "desc": v["desc"]}
            for k, v in PRESET_STRATEGIES.items()]
