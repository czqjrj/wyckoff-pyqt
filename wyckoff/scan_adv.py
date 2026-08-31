"""高级扫描引擎: 在传统"信号扫描"之上的专项扫描集合。

与 screener (多维综合选股) / backtest (信号扫描) 互补, 各自面向一个具体问题:
  - scan_pullback        回踩买点: 强买点(Spring/SC/ST)后回调未破位, 此刻可低吸
  - scan_pnf_breakout    P&F 突破: 点数图刷新前高 / 三重顶突破 + 计数目标
  - scan_portfolio_risk  持仓风险: 我的持仓里出现派发/减仓信号
  - scan_volume_divergence 量价背离: 放量滞涨 / 缩量过峰 / 缩量回踩 / 布林蓄势
  - scan_sector_driven   板块联动: 强势板块 → 板块内信号股
  - scan_candidates_status 候选池巡检: 待观察池里股票是走强/破位/继续等
  - scan_volume_surge    量能异动: 量比/量Z大幅放大 (突破 vs 出货)
  - scan_wave_proximity  波浪亲密度: 现价贴近斐波那契关键支撑/目标位
  - scan_lhb             龙虎榜: 游资/机构净买入 (需 akshare)
  - scan_margin          两融异动: 融资余额显著增加 (需 akshare)
  - scan_restricted      解禁预警: 未来解禁市值/占比高 (需 akshare)
  - scan_yjyg            业绩预告: 预增 / 预亏 (需 akshare)
  - scan_north           北向资金: 北向净流入/个股持仓 (需 akshare, 数据受限)

全部 fail-soft: 单股失败/数据源不可达 → 跳过该股, 不阻塞。扫描结果统一结构:
  {code, name, last, score, <扫描专属列>, msg}
"""
import datetime as _dt

import numpy as np

from .backtest import MARKET_UNIVERSE
from .datasource import fetch_kline
from .events import detect_all
from .fundamental import fetch_market_universe
from .indicators import add_indicators, find_pivots
from .phases import judge_phase
from .utils import normalize_symbol

try:
    from .flow_extra import (
        fetch_dzjy,
        fetch_gpzy,
        fetch_jgdy,
        fetch_lhb_stats,
        fetch_margin,
        fetch_north,
        fetch_restricted,
        fetch_yjyg,
        fetch_ztpool,
    )
except Exception:  # pragma: no cover - flow_extra 惰性 import akshare
    fetch_dzjy = fetch_gpzy = fetch_jgdy = fetch_lhb_stats = None
    fetch_margin = fetch_north = fetch_restricted = fetch_yjyg = fetch_ztpool = None

# 阶段加分 (吸筹/上升 > 0, 派发/下跌 < 0)
_PHASE_BONUS = {
    "底部整固": 8,
    "上升趋势": 5,
    "顶部构筑": -5,
    "下跌趋势": -8,
}

# 买点信号 → 加分基数
_BULL_SIG = {"Spring": 6, "Shakeout": 5, "SC": 5, "ST": 4, "PSY": 3, "SOS": 4,
             "JOC": 4, "LPS": 3, "AR": 2, "BU": 2}
_BEAR_SIG = {"UTAD", "BC", "UT", "TRU", "SUP", "ND", "ER", "LPSY", "SOW"}


def _load_df(code, datalen=500):
    """抓 K线+指标, 返回 (df, phase, events, pivots) 或 None。"""
    try:
        symbol = normalize_symbol(code)
        df = add_indicators(fetch_kline(symbol, datalen=datalen, scale=240),
                            symbol=symbol)
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _ = judge_phase(df, pivots, events)
        return df, phase, events, pivots
    except Exception:
        return None


def _row_base(code, df, phase):
    """统一行头: code/name/last/phase/score 初始化。"""
    name = ""
    try:
        from .screener import _get_stock_name
        name = _get_stock_name(str(code)[-6:] if len(str(code)) > 6 else str(code))
    except Exception:
        pass
    return {
        "code": str(code)[-6:] if len(str(code)) > 6 else str(code),
        "name": name,
        "last": round(float(df["close"].iloc[-1]), 2),
        "phase": phase,
        "score": 0,
    }


def _batch(codes, fn, workers=6, cancel_event=None):
    out = []
    if not codes:
        return out
    if workers <= 1 or len(codes) <= 1:
        for c in codes:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                out.append(fn(c))
            except Exception:
                out.append(None)
        return out
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fn, c): c for c in codes}
            for fut in as_completed(futs):
                if cancel_event is not None and cancel_event.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    out.append(fut.result())
                except Exception:
                    out.append(None)
    except Exception:
        for c in codes:
            try:
                out.append(fn(c))
            except Exception:
                out.append(None)
    return out


def _market_codes():
    """全市场活跃股列表 (成交额Top / 内置兜底), 供需要 universe 的扫描用。"""
    try:
        codes = fetch_market_universe(100) or MARKET_UNIVERSE
    except Exception:
        codes = MARKET_UNIVERSE
    return codes


# ──────────────────────────── 1. 回踩买点 ────────────────────────────

def scan_pullback(codes, workers=6, cancel_event=None, datalen=500):
    """强买点后回调未破位 → 此刻可低吸的股票。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        n = len(df)
        closes = df["close"].values
        recent = [e for e in events if e["idx"] >= n - 60]
        bull = [e for e in recent if e["type"] in _BULL_SIG]
        if not bull:
            return None
        last = float(closes[-1])
        best = None
        for e in bull:
            lo = int(e["idx"])
            area_low = float(np.min(closes[lo:min(lo + 5, n)]))  # 信号区最低 (买点)
            peak = float(np.max(closes[lo:]))                     # 信号后高点
            if peak <= 0:
                continue
            # 只保留"谷底型"买点: 买点须明显低于其后高点, 排除里程碑/反弹高点事件
            if area_low >= peak * 0.97:
                continue
            pull = (peak - last) / peak * 100
            intact = last >= area_low * 0.995
            if not intact or not (0.8 <= pull <= 16.0):
                continue
            if best is None or e["idx"] > best["idx"]:
                best = {"type": e["type"], "low": area_low, "peak": peak,
                        "pull": pull, "conf": e.get("conf", 50), "idx": e["idx"]}
        if best is None:
            return None
        row = _row_base(code, df, phase)
        phase_base = phase.split(" ")[0] if phase else ""
        damp = 0.8 if best["pull"] < 3.0 else 1.0  # 回调过浅意义有限, 微降权
        score = (_PHASE_BONUS.get(phase_base, 0) + _BULL_SIG.get(best["type"], 3)
                 + min(6, int(best["conf"] / 18)) + (5 if 2 <= best["pull"] <= 8 else 3))
        row["score"] = round(max(1, score * damp), 1)
        row["sig"] = best["type"]
        row["sig_price"] = round(best["low"], 2)
        row["pull"] = round(best["pull"], 1)
        row["peak"] = round(best["peak"], 2)
        row["break"] = "未破位"
        row["msg"] = (f"{best['type']}@{best['low']:.2f} 后自高点回踩{best['pull']:.1f}%, 未破买点")
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────────────── 2. P&F 突破 ────────────────────────────

def scan_pnf_breakout(codes, workers=6, cancel_event=None, datalen=500):
    """点数图新高二重顶/三重顶突破 + 计数目标。"""
    from .pnf import build_pnf, pnf_targets

    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        cols, box = build_pnf(df)
        if len(cols) < 6 or cols[-1]["type"] != "X":
            return None
        x_highs = [c["hi"] for c in cols[:-1] if c["type"] == "X"]
        if not x_highs:
            return None
        prev = max(x_highs[-20:])
        bh = cols[-1]["hi"]
        if bh <= prev:
            return None
        last = float(df["close"].iloc[-1])
        near = sum(1 for h in x_highs[-20:] if prev - h <= 1.5 * box)
        tgt = pnf_targets(df, cols, box) or {}
        tgt_up = (tgt.get("近端上方目标") or tgt.get("横向计数上方目标")
                  or tgt.get("纵向计数上方目标") or round(bh + 3 * box, 2))
        tgt_dist = (tgt_up - bh) / bh * 100 if tgt_up else None
        lead = (last - prev) / prev * 100 if prev else 0
        overext = max(0, (last - bh) / bh * 100 - 6)  # 追高惩罚
        row = _row_base(code, df, phase)
        shape = ("三重顶突破" if near >= 3 else "二重顶突破" if near >= 2 else "刷新新高")
        score = 18 + near * 5 + (10 if bh - prev >= box else 4) - overext
        row["score"] = round(max(1, min(40, score)), 1)
        row["break"] = shape
        row["res"] = round(prev, 2)
        row["lead"] = round(lead, 1)
        row["target"] = round(tgt_up, 2)
        row["tgt_dist"] = round(tgt_dist, 1) if tgt_dist is not None else None
        row["msg"] = f"{shape} {prev:.2f} → 目标 {tgt_up:.2f}"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────────────── 3. 持仓风险 ────────────────────────────

def scan_portfolio_risk(workers=6, cancel_event=None, datalen=500):
    """扫描"我的持仓": 派发信号 / 破位 / 浮盈亏, 给减仓离场指引 (长多框架)。"""
    from .storage import load_portfolio

    recs = load_portfolio()
    out = []
    for rec in recs:
        if cancel_event is not None and cancel_event.is_set():
            break
        code = rec.get("code", "")
        if not code:
            continue
        loaded = _load_df(code, datalen)
        if loaded is None:
            continue
        df, phase, events, _piv = loaded
        last = float(df["close"].iloc[-1])
        n = len(df)
        bear = [e["type"] for e in events
                if e["idx"] >= n - 12 and e["type"] in _BEAR_SIG]
        low60 = float(df["low"].iloc[-60:].min())
        stop = rec.get("stop")
        cost = rec.get("cost")
        pnl = (last - cost) / cost * 100 if cost else None
        broke = (stop is not None and last < float(stop) * 1.01) or last < low60 * 1.005
        row = _row_base(code, df, phase)
        row["cost"] = round(cost, 2) if cost else None
        row["pnl"] = round(pnl, 1) if pnl is not None else None
        row["signals"] = "+".join(sorted(set(bear))) if bear else "-"
        row["stop"] = round(float(stop), 2) if stop else None
        row["low60"] = round(low60, 2)
        row["broke"] = "是" if broke else "否"
        if bear and broke:
            row["advice"] = "减仓/离场"
            row["score"] = 30 + len(bear) * 4
        elif bear:
            row["advice"] = "逢高减仓"
            row["score"] = 18 + len(bear) * 3
        elif broke:
            row["advice"] = "破位警戒"
            row["score"] = 15
        else:
            row["advice"] = "持稳"
            row["score"] = 5
        row["msg"] = row["advice"] + (f" 信号:{'/'.join(sorted(set(bear)))}" if bear else "")
        out.append(row)
    out.sort(key=lambda r: (-r["score"], r["code"]))
    return out


# ──────────────────────────── 4. 量价背离 ────────────────────────────

def scan_volume_divergence(codes, workers=6, cancel_event=None, datalen=500):
    """量价背离: 放量滞涨 / 缩量过峰 / 缩量回踩 / 布林蓄势。"""

    def classify(df):
        c = df["close"].values
        last, prev = float(c[-1]), float(c[-2])
        pct = (last / prev - 1) * 100 if prev else 0
        vr20 = float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df else np.nan
        if not np.isfinite(vr20):
            vr20 = np.nan
        hi20 = float(df["high"].iloc[-20:].max())
        lo20 = float(df["low"].iloc[-20:].min())
        new_high = last >= hi20 * 0.995
        # 布林收口百分位 (同 screener 口径)
        bw_pct = None
        if {"boll_up", "boll_dn", "boll_mid"}.issubset(df.columns):
            up = float(df["boll_up"].iloc[-1])
            dn = float(df["boll_dn"].iloc[-1])
            mid = float(df["boll_mid"].iloc[-1])
            if mid > 0 and up > dn:
                bws = (df["boll_up"] - df["boll_dn"]) / df["boll_mid"].replace(0, np.nan)
                bws = bws.dropna()
                if len(bws) > 0:
                    bw_pct = float((bws < ((up - dn) / mid * 100)).mean()) * 100
        if vr20 >= 1.8 and new_high is False and pct <= 1.5 and float(c[-1] <= float(df["open"].iloc[-1])):
            return "放量滞涨", f"量比{vr20:.1f}, 收于开盘下方"
        if vr20 >= 1.8 and pct <= -2:
            return "放量下跌", f"量比{vr20:.1f}, 跌{pct:.1f}%"
        if new_high and vr20 < 0.7:
            return "缩量过峰", f"创20日新高但量比{vr20:.1f}, 上涨动能存疑"
        if last < hi20 * 0.97 and vr20 < 0.7:
            return "缩量回踩", f"较20日高回调, 量比{vr20:.1f} (缩量健康)"
        if bw_pct is not None and bw_pct < 30 and vr20 < 1.2:
            return "布林蓄势", f"带宽处于近{int(bw_pct)}%分位, 低波动蓄势"
        return None, None

    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        kind, note = classify(df)
        if kind is None:
            return None
        last = float(df["close"].iloc[-1])
        prev_c = float(df["close"].iloc[-2])
        pct = (last / prev_c - 1) * 100 if prev_c else 0
        vr20 = float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df else np.nan
        row = _row_base(code, df, phase)
        phase_base = phase.split(" ")[0] if phase else ""
        score = (_PHASE_BONUS.get(phase_base, 0) + 8
                 + (min(6, int((vr20 or 0) * 3))) + (4 if pct > 2 else 0))
        row["score"] = round(max(1, score), 1)
        row["kind"] = kind
        row["vr"] = round(vr20, 1) if np.isfinite(vr20) else None
        row["pct"] = round(pct, 1)
        row["bw_pct"] = _bw_of(df)
        row["msg"] = note
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


def _bw_of(df):
    if not {"boll_up", "boll_dn", "boll_mid"}.issubset(df.columns):
        return None
    try:
        up = float(df["boll_up"].iloc[-1])
        dn = float(df["boll_dn"].iloc[-1])
        mid = float(df["boll_mid"].iloc[-1])
        if not (mid > 0 and up > dn):
            return None
        bws = (df["boll_up"] - df["boll_dn"]) / df["boll_mid"].replace(0, np.nan)
        bws = bws.dropna()
        if len(bws) == 0:
            return None
        return int((bws < ((up - dn) / mid * 100)).mean() * 100)
    except Exception:
        return None


# ──────────────────────────── 5. 板块联动 ────────────────────────────

def scan_sector_driven(workers=6, cancel_event=None, top=6, per=15):
    """强势板块 (资金流入+上涨) → 板块内信号股联动。"""
    try:
        from .backtest import scan_sector_stocks, scan_sectors
    except Exception:
        return []
    try:
        secs = scan_sectors()
    except Exception:
        return []
    live = [s for s in secs if s.get("live") and s.get("tone") in ("bullish", "mixed")
            and s.get("flow20_yi", 0) > 0]
    live.sort(key=lambda s: -s.get("score", 0))
    top_secs = live[:top]
    rows = []
    for s in top_secs:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            stocks = scan_sector_stocks(s["bk_code"], s["name"], limit=per)
        except Exception:
            continue
        for st in stocks:
            if cancel_event is not None and cancel_event.is_set():
                break
            if st.get("score", 0) < 50:
                continue
            rows.append({
                "code": st.get("code", ""), "name": st.get("name", ""),
                "last": st.get("last"), "sector": s["name"],
                "sec_score": round(s.get("score", 0), 0),
                "sec_flow": round(s.get("flow20_yi", 0), 1),
                "phase": st.get("phase", ""),
                "signals": "+".join((st.get("signals") or [])[:4]),
                "score": round(st.get("score", 0), 0),
                "msg": f"板块[{s['name']}] 资金{round(s.get('flow20_yi', 0), 1)}亿",
            })
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────────────── 6. 候选池巡检 ────────────────────────────

def scan_candidates_status(workers=6, cancel_event=None, datalen=500):
    """复盘待观察候选池: 已走强 / 已破位 / 继续观察。"""
    from .storage import load_candidates

    recs = load_candidates()

    def one(rec):
        code = rec.get("code", "")
        if not code:
            return None
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        last = float(df["close"].iloc[-1])
        hi20 = float(df["high"].iloc[-20:].max())
        lo20 = float(df["low"].iloc[-20:].min())
        near_hi = (last / hi20 - 1) * 100 if hi20 else 0
        phase_base = phase.split(" ")[0] if phase else ""
        strong = phase_base in ("底部整固", "上升趋势") and near_hi >= -2
        broken = (phase_base in ("下跌趋势", "顶部构筑")) or last < lo20 * 1.01
        status = ("已走强" if strong else "已破位" if broken else "继续观察")
        days = None
        try:
            d = rec.get("date", "")
            dt = _dt.datetime.strptime(d, "%Y-%m-%d %H:%M")
            days = (_dt.datetime.now() - dt).days
        except Exception:
            pass
        row = _row_base(code, df, phase)
        base_bonus = _PHASE_BONUS.get(phase_base, 0)
        row["score"] = round(max(1, 6 + base_bonus + (-4 if status == "已破位" else 4)), 1)
        row["origin"] = rec.get("signals", "").replace("+", "/") or "-"
        row["status"] = status
        row["near_hi"] = round(near_hi, 1)
        row["days"] = days
        row["msg"] = status + (f", 距20日高{near_hi:.1f}%" if status != "已破位" else ", 已跌破近期低点")
        return row

    out = [r for r in _batch(recs, one, workers, cancel_event) if r]
    out.sort(key=lambda r: (-r["score"], r["code"]))
    return out


# ──────────────────────────── 7. 量能异动 ────────────────────────────

def scan_volume_surge(codes, workers=6, cancel_event=None, datalen=500):
    """量比/量Z大幅放大: 判断放量突破 vs 放量下跌 vs 放量滞涨。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        c = df["close"].values
        last, prev_c = float(c[-1]), float(c[-2])
        pct = (last / prev_c - 1) * 100 if prev_c else 0
        vr20 = float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df else np.nan
        z20 = float(df["vol_z_20"].iloc[-1]) if "vol_z_20" in df else np.nan
        if not (np.isfinite(vr20) and vr20 >= 2.0) and not (np.isfinite(z20) and z20 >= 2.2):
            return None
        hi20 = float(df["high"].iloc[-20:].max())
        lo20 = float(df["low"].iloc[-20:].min())
        band = (last - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0
        vr = vr20 if np.isfinite(vr20) else (z20 or 0)
        if pct >= 3 and band >= 70:
            kind = "放量突破"
        elif pct <= -3:
            kind = "放量下跌"
        elif abs(pct) < 1.2:
            kind = "放量滞涨"
        else:
            kind = "放量异动"
        row = _row_base(code, df, phase)
        row["score"] = round(max(1, min(40, 10 + vr * 3 + (8 if kind == "放量突破" else 0)
                                        + (4 if band >= 70 else 0))), 1)
        row["kind"] = kind
        row["vr"] = round(vr20, 1) if np.isfinite(vr20) else (round(z20, 1) if np.isfinite(z20) else None)
        row["pct"] = round(pct, 1)
        row["band"] = round(band, 0)
        row["msg"] = f"{kind}, 量比{vr20:.1f}" if np.isfinite(vr20) else f"{kind}, 量Z{z20:.1f}"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────────────── 8. 波浪亲密度 ────────────────────────────

def scan_wave_proximity(codes, workers=6, cancel_event=None, datalen=500):
    """现价贴近斐波那契关键支撑/目标位。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, piv = loaded
        from .wavecount import count_waves
        wc = count_waves(piv)
        levels = list(wc.fib_confluence or []) or []
        last = float(df["close"].iloc[-1])
        best = None
        for lv in levels:
            px = lv.get("price")
            if not px:
                continue
            d = abs(last - px) / last * 100
            best = (lv.get("level"), px, d) if best is None or d < best[2] else best
        if best is None or best[2] > 4.0:
            return None
        level, px, dist = best
        row = _row_base(code, df, phase)
        phase_base = phase.split(" ")[0] if phase else ""
        row["score"] = round(max(1, 10 + _PHASE_BONUS.get(phase_base, 0)
                                 + max(0, 8 - dist * 2)), 1)
        row["level"] = str(level)
        row["kp"] = round(px, 2)
        row["dist"] = round(dist, 1)
        row["target"] = round(wc.next_target, 2) if wc.next_target else None
        row["wave"] = wc.position or ""
        row["msg"] = f"贴近{level}@{px:.2f} ({dist:.1f}%), 结构:{wc.position}"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────────────── P1: 需 akshare 的源扫描 ────────────────────────────

def scan_lhb(lookback_days=30):
    """龙虎榜: 近一月净买入/机构抢筹的股票。"""
    if fetch_lhb_stats is None:
        return []
    try:
        stats = fetch_lhb_stats()
    except Exception:
        return []
    rows = []
    for r in stats:
        net = float(r.get("net") or 0)
        inst = float(r.get("inst_net") or 0)
        times = int(r.get("times") or 0)
        if net <= 0 and inst <= 0:
            continue
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"), "times": times,
            "net": round(net / 1e8, 2), "inst_net": round(inst / 1e8, 2),
            "pct_1m": round(r.get("pct_1m") or 0, 1),
            "score": round(min(100, net / 1e8 * 10 + inst / 1e8 * 15 + times * 2 + 20), 1),
            "msg": f"近一月上榜{times}次, 龙虎榜净买{net/1e8:.2f}亿, 机构净买{inst/1e8:.2f}亿",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def _latest_margin():
    """取最近一个已发布的两融快照 (0..5 日历日回退)。"""
    for d in range(6):
        items = fetch_margin(days_ago=d)
        if items:
            return items
    return []


def scan_margin(bench_days=5):
    """两融: 融资余额较 N 日前显著放大。

    两融数据 T+1 晚才发布, 盘中当天取不到 → 自动回退最近一个已发布交易日。
    """
    if fetch_margin is None:
        return []
    try:
        cur = _latest_margin()
        prev = fetch_margin(days_ago=bench_days)
    except Exception:
        return []
    by_code = {x["code"]: x for x in cur}
    rows = []
    for p in prev:
        c = by_code.get(p["code"])
        if not c:
            continue
        bal0 = p.get("mrg_bal") or 0
        bal1 = c.get("mrg_bal") or 0
        if bal0 <= 0:
            continue
        chg = (bal1 / bal0 - 1) * 100
        if chg < 5 or (bal1 - bal0) < 5e7:
            continue
        rows.append({
            "code": c.get("code", ""), "name": c.get("name", ""),
            "last": c.get("last"),
            "mrg_bal": round(bal1 / 1e8, 2) if bal1 else None,
            "mrg_chg": round(chg, 1),
            "mrg_delta": round((bal1 - bal0) / 1e8, 2),
            "sec_bal": round(c.get("sec_bal") or 0, 2) if c.get("sec_bal") else None,
            "date": str(c.get("date") or ""),
            "score": round(min(100, chg * 3 + (bal1 - bal0) / 1e8 * 8), 1),
            "msg": f"融资余额{bench_days}日+{chg:.1f}% (+{(bal1-bal0)/1e8:.2f}亿)",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def scan_restricted(days=60):
    """解禁预警: 未来 N 日限售解禁市值/占比高的股票。"""
    if fetch_restricted is None:
        return []
    try:
        items = fetch_restricted(days=days)
    except Exception:
        return []
    rows = []
    for r in items:
        ratio = float(r.get("ratio") or 0)
        value = float(r.get("value") or 0)
        if ratio < 1.0:
            continue  # 占比过低, 无压力
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "unlock_date": r.get("date", ""),
            "value_yi": round(value / 1e8, 2),
            "ratio": round(ratio, 1),
            "type": r.get("type", ""),
            "pct20": round(r.get("pct20") or 0, 1),
            "score": round(min(100, ratio * 4 + value / 1e8 * 2 + 20), 1),
            "msg": f"{r.get('date','')} 解禁{value/1e8:.1f}亿, 占流通{ratio:.1f}%",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def scan_yjyg():
    """业绩预告: 预增/扭亏 与 预减/首亏/续亏 快扫。"""
    if fetch_yjyg is None:
        return []
    try:
        items = fetch_yjyg()
    except Exception:
        return []
    rows = []
    for r in items:
        ampl = r.get("ampl")
        try:
            ampl = float(ampl)
        except (TypeError, ValueError):
            ampl = 0.0
        kinds = r.get("kind", "")
        up = kinds in ("预增", "略增", "扭亏")
        if not up and kinds not in ("预减", "首亏", "续亏", "略减"):
            continue
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "kind": r.get("kind", ""),
            "ampl": round(ampl, 1),
            "date": r.get("date", ""),
            "score": round(min(100, (25 + ampl * 0.4) if up else max(5, 25 - ampl * 0.3)), 1),
            "msg": r.get("msg", ""),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def scan_north():
    """北向资金: 个股持仓增持 / 大盘净流入 (数据源自2024-08 起个股持仓受限)。"""
    if fetch_north is None:
        return []
    try:
        rows = fetch_north()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "hold_chg": r.get("hold_chg"),
            "net": r.get("net"),
            "market": r.get("market", ""),
            "score": round(min(100, max(float(r.get("net") or 0) / 1e8 * 20,
                                        float(r.get("hold_chg") or 0) * 15) + 20
                               if r.get("net") or r.get("hold_chg") else 10), 1),
            "msg": r.get("msg", ""),
        })
    out.sort(key=lambda r: -r["score"])
    return out


# ──────────────────── 9. 派发风险 (全市场) ────────────────────

def scan_distribution(codes, workers=6, cancel_event=None, datalen=500):
    """派发风险: 顶部构筑/下跌趋势 + 派发信号(UTAD/BC) → 减仓预警 (全市场)。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        n = len(df)
        recent = [e for e in events if e["idx"] >= n - 60]
        bear = [e for e in recent if e["type"] in _BEAR_SIG]
        phase_base = phase.split(" ")[0] if phase else ""
        if not bear and phase_base not in ("顶部构筑", "下跌趋势"):
            return None
        row = _row_base(code, df, phase)
        row["kind"] = "派发信号" if bear else "趋势走弱"
        sigs = "/".join(e["type"] for e in bear) or phase_base
        row["signals"] = sigs
        score = 10 + (8 if phase_base == "下跌趋势" else 5 if phase_base == "顶部构筑" else 0)
        score += len(bear) * 3
        row["score"] = round(min(100, score), 1)
        row["msg"] = f"{sigs} 出现, {phase_base} 阶段, 留意减仓/离场"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────── 10. 平台突破 (全市场) ────────────────────

def scan_platform(codes, workers=6, cancel_event=None, datalen=500):
    """平台突破: 低波动平台(40日振幅收缩) + 放量上破前高 → 突破启动。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, _events, _piv = loaded
        c = df["close"].values
        n = len(df)
        if n < 41:
            return None
        last, prev = float(c[-1]), float(c[-2])
        pct = (last / prev - 1) * 100 if prev else 0
        hi20 = float(np.max(c[-21:-1]))          # 前20日高点 (不含今日)
        if hi20 <= 0:
            return None
        amp = (np.max(c[-40:]) / hi20 - 1) * 100
        if amp > 25 or last <= hi20:
            return None
        vr = float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df else np.nan
        if not np.isfinite(vr):
            vr = 0.0
        if vr < 1.8:
            return None
        phase_base = phase.split(" ")[0] if phase else ""
        row = _row_base(code, df, phase)
        row["score"] = round(max(1, _PHASE_BONUS.get(phase_base, 0) + 10
                                 + min(6, int(vr * 2))), 1)
        row["vr"] = round(vr, 1)
        row["band"] = round(amp, 1)
        row["pct"] = round(pct, 1)
        row["high20"] = round(hi20, 2)
        row["msg"] = f"放量{vr:.1f}倍突破前高{hi20:.2f}, 40日平台振幅{amp:.0f}%"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────── 11. 吸筹完成度 (全市场) ────────────────────

def scan_absorption(codes, workers=6, cancel_event=None, datalen=500):
    """吸筹完成度: SC/PSY 起始 + Spring/AR/SOS 确认, 吸筹放量换手完毕待主升。"""
    def one(code):
        loaded = _load_df(code, datalen)
        if loaded is None:
            return None
        df, phase, events, _piv = loaded
        if not events:
            return None
        ev = sorted(events, key=lambda e: e["idx"])
        types = [e["type"] for e in ev]
        if types[-1] not in ("Spring", "Shakeout", "AR", "SOS", "ST", "JOC"):
            return None
        had_acc = ("SC" in types or "PSY" in types) and \
            any(t in ("Spring", "AR", "SOS") for t in types)
        if not had_acc:
            return None
        phase_base = phase.split(" ")[0] if phase else ""
        row = _row_base(code, df, phase)
        row["chain"] = "→".join(types[-6:])
        row["score"] = round(max(1, _PHASE_BONUS.get(phase_base, 0) + 12), 1)
        row["msg"] = f"吸筹链 {row['chain']}, 当前 {phase_base or '整固'} 阶段, 待主升"
        return row

    rows = [r for r in _batch(codes, one, workers, cancel_event) if r]
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# ──────────────────── 12. 大宗交易 ────────────────────

def scan_dzjy(lookback_days=10, min_amount=0.5):
    """大宗交易淘金: 折价接盘 / 溢价买入 (亿元以上有参考意义)。"""
    if fetch_dzjy is None:
        return []
    try:
        items = fetch_dzjy(lookback_days=lookback_days)
    except Exception:
        return []
    rows = []
    for r in items:
        prem = float(r.get("premium") or 0)
        amount = float(r.get("amount_yi") or 0)
        close = r.get("close")
        if amount < min_amount or not close:
            continue
        if prem < 0:
            score = min(60, 15 + (-prem) * 4 + amount * 6)   # 折价 → 场内筹码易手
        else:
            score = min(70, 25 + prem * 3 + amount * 5)      # 溢价 → 大资金做多意愿
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": close,
            "date": r.get("date", ""),
            "price": r.get("price"),
            "premium": round(prem, 2),
            "amount_yi": round(amount, 2),
            "score": round(score, 1),
            "msg": f"{r.get('date','')} 大宗{amount:.2f}亿, 折溢{prem:+.1f}% (收盘{close})",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


# ──────────────────── 13. 机构调研 ────────────────────

def scan_jgdy(min_inst=5):
    """机构调研热度: 近期被密集调研 (接待机构多) 的股票。"""
    if fetch_jgdy is None:
        return []
    try:
        items = fetch_jgdy()
    except Exception:
        return []
    rows = []
    for r in items:
        num = int(r.get("inst_num") or 0)
        if num < min_inst:
            continue
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "inst_num": num,
            "date": r.get("date", ""),
            "way": r.get("way", ""),
            "score": round(min(100, 25 + num * 2.5), 1),
            "msg": f"{r.get('date','')} 接待{num}家机构, {r.get('way','')[:20]}",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


# ──────────────────── 14. 涨停池 ────────────────────

def scan_ztpool(min_lt=2):
    """涨停池观察: 连板高度与炸板确定性 → 题材强度/承接力。"""
    if fetch_ztpool is None:
        return []
    try:
        items = fetch_ztpool()
    except Exception:
        return []
    rows = []
    for r in items:
        lt = int(r.get("limit_times") or 1)
        if lt < min_lt:
            continue
        oc = int(r.get("open_cnt") or 0)
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "pct": r.get("pct"),
            "amount_yi": round(float(r.get("amount_yi") or 0), 2),
            "limit_times": lt,
            "open_cnt": oc,
            "sector": r.get("sector", ""),
            "date": r.get("date", ""),
            "score": round(min(100, 25 + lt * 15 - oc * 3), 1),
            "msg": f"{r.get('date','')} {lt}连板, 炸板{oc}次" + (f", {r.get('sector','')}" if r.get("sector") else ""),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


# ──────────────────── 15. 股权质押 ────────────────────

def scan_gpzy(min_ratio=50):
    """股权质押风险: 质押比例过高 → 补仓/平仓/减持隐患 (风险提示, 规避为主)。"""
    if fetch_gpzy is None:
        return []
    try:
        items = fetch_gpzy()
    except Exception:
        return []
    rows = []
    for r in items:
        ratio = float(r.get("ratio") or 0)
        if ratio < min_ratio:
            continue
        rows.append({
            "code": r.get("code", ""), "name": r.get("name", ""),
            "last": r.get("last"),
            "ratio": round(ratio, 1),
            "market_value": round(float(r.get("market_value") or 0), 1),
            "industry": r.get("industry", ""),
            "pct_y1": r.get("pct_y1"),
            "date": r.get("date", ""),
            "score": round(min(100, 15 + ratio * 0.8), 1),
            "msg": f"{r.get('date','')} 质押{ratio:.0f}% (≥{min_ratio}% 高质押风险, 注意规避)",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


# ──────────────────── 扫描注册表 (供 UI 使用 + run_scan 单一数据源) ────────────────────

SCAN_REGISTRY = [
    {"key": "pullback", "title": "回踩买点", "fn": scan_pullback,
     "desc": "强买点(Spring/SC/ST)后回调未破位, 此刻可低吸", "need_universe": True},
    {"key": "pnf_breakout", "title": "P&F 突破", "fn": scan_pnf_breakout,
     "desc": "点数图刷新前高/三重顶突破 + 计数目标", "need_universe": True},
    {"key": "volume_surge", "title": "量能异动", "fn": scan_volume_surge,
     "desc": "量比/量Z大幅放大 → 突破 vs 出货", "need_universe": True},
    {"key": "volume_divergence", "title": "量价背离", "fn": scan_volume_divergence,
     "desc": "放量滞涨/缩量过峰/缩量回踩/布林蓄势", "need_universe": True},
    {"key": "wave_proximity", "title": "波浪亲密度", "fn": scan_wave_proximity,
     "desc": "现价贴近斐波那契关键支撑/目标位", "need_universe": True},
    {"key": "portfolio_risk", "title": "持仓风险",
     "fn": lambda workers=6, cancel_event=None: scan_portfolio_risk(
         workers=workers, cancel_event=cancel_event),
     "desc": "我的持仓中的派发/减仓/破位信号", "need_universe": False},
    {"key": "candidates_status", "title": "候选池巡检",
     "fn": lambda workers=6, cancel_event=None: scan_candidates_status(
         workers=workers, cancel_event=cancel_event),
     "desc": "待观察池股票: 已走强/已破位/继续观察", "need_universe": False},
    {"key": "sector_driven", "title": "强势板块联动",
     "fn": lambda workers=6, cancel_event=None: scan_sector_driven(
         workers=workers, cancel_event=cancel_event),
     "desc": "资金流入强势板块 → 板块内信号股", "need_universe": False},
    {"key": "lhb", "title": "龙虎榜", "fn": lambda *_a, **_k: scan_lhb(),
     "desc": "近一月游资/机构净买入 (需 akshare)", "need_universe": False},
    {"key": "margin", "title": "两融异动", "fn": lambda *_a, **_k: scan_margin(),
     "desc": "融资余额显著放大 (需 akshare)", "need_universe": False},
    {"key": "restricted", "title": "解禁预警", "fn": lambda *_a, **_k: scan_restricted(),
     "desc": "未来60日限售解禁压力 (需 akshare)", "need_universe": False},
    {"key": "yjyg", "title": "业绩预告", "fn": lambda *_a, **_k: scan_yjyg(),
     "desc": "业绩预增/预亏快扫 (需 akshare)", "need_universe": False},
    {"key": "north", "title": "北向资金", "fn": lambda *_a, **_k: scan_north(),
     "desc": "北向净流入/个股持仓 (数据源受限)", "need_universe": False},
    {"key": "distribution", "title": "派发风险", "fn": scan_distribution,
     "desc": "全市场顶部构筑/派发信号(UTAD/BC) → 减仓预警", "need_universe": True},
    {"key": "platform", "title": "平台突破", "fn": scan_platform,
     "desc": "低波动平台 + 放量上破前高, 突破启动", "need_universe": True},
    {"key": "absorption", "title": "吸筹完成度", "fn": scan_absorption,
     "desc": "SC→PSY→Spring/AR/SOS 完整吸筹链, 待主升", "need_universe": True},
    {"key": "dzjy", "title": "大宗交易", "fn": lambda *_a, **_k: scan_dzjy(),
     "desc": "大宗折价接盘 / 溢价买入 (需 akshare)", "need_universe": False},
    {"key": "jgdy", "title": "机构调研", "fn": lambda *_a, **_k: scan_jgdy(),
     "desc": "近期被机构密集调研 (需 akshare)", "need_universe": False},
    {"key": "ztpool", "title": "涨停池", "fn": lambda *_a, **_k: scan_ztpool(),
     "desc": "连板高度与炸板承接 (需 akshare)", "need_universe": False},
    {"key": "gpzy", "title": "股权质押", "fn": lambda *_a, **_k: scan_gpzy(),
     "desc": "高质押比例风险提示 (需 akshare)", "need_universe": False},
]

# need_universe=True 的扫描统一签名 (codes, workers=, cancel_event=, datalen=)
_UNIVERSE_SCAN_KEYS = frozenset(k["key"] for k in SCAN_REGISTRY
                                if k.get("need_universe"))

# 每个扫描默认展示列 (代码/名称/现价/评分 由窗口统一前置)
SCAN_COLUMNS = {
    "pullback": ["sig", "sig_price", "pull", "break", "phase", "score", "msg"],
    "pnf_breakout": ["break", "res", "lead", "target", "tgt_dist", "score", "msg"],
    "volume_surge": ["kind", "vr", "pct", "band", "phase", "score", "msg"],
    "volume_divergence": ["kind", "vr", "pct", "phase", "score", "msg"],
    "wave_proximity": ["level", "kp", "dist", "target", "wave", "score", "msg"],
    "portfolio_risk": ["cost", "pnl", "signals", "stop", "low60", "broke", "advice", "score"],
    "candidates_status": ["origin", "status", "near_hi", "days", "phase", "score", "msg"],
    "sector_driven": ["sector", "sec_score", "sec_flow", "phase", "signals", "score", "msg"],
    "lhb": ["times", "net", "inst_net", "pct_1m", "score", "msg"],
    "margin": ["mrg_bal", "mrg_chg", "mrg_delta", "sec_bal", "date", "score", "msg"],
    "restricted": ["unlock_date", "value_yi", "ratio", "type", "pct20", "score", "msg"],
    "yjyg": ["kind", "ampl", "date", "score", "msg"],
    "north": ["hold_chg", "net", "market", "score", "msg"],
    "distribution": ["kind", "signals", "score", "msg"],
    "platform": ["vr", "band", "pct", "high20", "phase", "score", "msg"],
    "absorption": ["chain", "phase", "score", "msg"],
    "dzjy": ["date", "premium", "amount_yi", "score", "msg"],
    "jgdy": ["inst_num", "date", "way", "score", "msg"],
    "ztpool": ["limit_times", "open_cnt", "pct", "amount_yi", "sector", "date", "score", "msg"],
    "gpzy": ["ratio", "market_value", "industry", "pct_y1", "date", "score", "msg"],
}


def run_scan(key, codes=None, workers=6, cancel_event=None, **kw):
    """按注册表 key 执行扫描。返回行列表 (统一含 code/name/last/score)。

    由 SCAN_REGISTRY 单一数据源生成: 注册表里存了 fn 与 need_universe,
    这里只负责按 need_universe 决定是否传入 codes (universe 扫描需全市场列表,
    非 universe 扫描忽略 codes)。新增扫描只需改注册表, 不再双份维护 fn_map。
    """
    for entry in SCAN_REGISTRY:
        if entry["key"] == key:
            fn = entry["fn"]
            break
    else:
        return []
    if entry.get("need_universe"):
        return fn(codes or [], workers=workers, cancel_event=cancel_event)
    return fn(workers=workers, cancel_event=cancel_event)
