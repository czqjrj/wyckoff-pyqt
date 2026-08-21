# -*- coding: utf-8 -*-
"""因果式回测、稳健性检查与自选股信号扫描。"""
import time
from collections import defaultdict
import threading

import numpy as np

from .datasource import fetch_kline, fetch_name
from .indicators import add_indicators, find_pivots
from .events import detect_all
from .config import event_dir, VSA_BULL, VSA_BEAR
from .phases import judge_phase
from .vsa import vsa_classify
from .utils import normalize_symbol
from ._shared import http_session
from .fundamental import (fetch_fundamental, fetch_main_flow, fetch_sector_flow,
                          build_confirm_section, fetch_all_board_stats,
                          fetch_board_constituents)

# 扫描用 EM 健康熔断: 连续 N 只确认抓取全失败 → 本次扫描跳过确认 (快速失败)
_EM_FAIL_STREAK = 0
_EM_FAIL_LIMIT = 3
_EM_LOCK = threading.Lock()

# classify_phase 按自然日缓存: 自选股 30s 定时刷新只更新实时价, 阶段流水线
# (K线+指标+枢轴+事件+判段) 每天最多跑一次, 避免 N 只自选股每 30s 全量重算。
_PHASE_CACHE = {}
_PHASE_CACHE_LOCK = threading.Lock()


def _phase_cache_key(code, datalen):
    return f"{code}:{datalen}:{time.strftime('%Y-%m-%d')}"


def reset_scan_confirm():
    """每次扫描开始调用, 重置 EM 熔断计数 (重探一次 EM 健康)。"""
    global _EM_FAIL_STREAK
    with _EM_LOCK:
        _EM_FAIL_STREAK = 0

# 全市场风格扫描宇宙: 跨行业流动性较好的个股 + 宽基指数/ETF
MARKET_UNIVERSE = [
    # 银行/保险/券商
    "sh600036", "sh601398", "sh601988", "sh601328", "sh600000",
    "sh601318", "sh601628", "sh600030", "sh601211", "sz300059",
    # 消费/白酒/家电
    "sh600519", "sz000858", "sz000333", "sz000651", "sh601888",
    "sz002415", "sh600690",
    # 医药
    "sh600276", "sz300760", "sh603259", "sz000538",
    # 新能源/汽车
    "sz002594", "sh601012", "sz300750", "sh600104", "sz002466", "sh600438",
    # 科技/半导体
    "sh688981", "sh603986", "sz002371", "sh600745", "sz300308",
    # 能源/资源/公用
    "sh601857", "sh600900", "sh601899", "sh601088", "sh600028",
    # 基建/交运/通信
    "sh601006", "sz000001", "sh600018", "sh600009", "sh601668",
    "sh600941", "sh600050",
    # 指数/ETF (验证 fail-soft, 只出资金流, 不出估值)
    "sh000001", "sz399001", "sz399006", "sz159915", "sh510300",
    "sh510050", "sh588000",
]

# 实证权重 (backtest_signals.py, 20根胜率): Spring 82.6% > ST 75.0% > SC 58.3%
# > SOS 45.7% > JOC 43.5%。JOC/SOS 胜率低于50%不直接加分, 仅作结构确认。
# LPS/BU 是 Phase D 标准买点 (修复 detect_joc_lps_bu 后已能生成), 权重对标 ST。
_BUY_PTS = {"Spring": 22, "Shakeout": 20, "ST": 11, "LPS": 11, "BU": 11,
            "SC": 8, "PSY": 2}
_SELL_PTS = {"UTAD": -13, "BC": -3, "LPSY": -10, "UT": -6, "SOW": -15}


def signal_score(r):
    """扫描排序评分: 买入信号 + 阶段方向 + 确认机制。
    高置信买 +5 / 需谨慎 -3 / 高置信空头 -5 (实证: 确认式卖信号更可信)。
    SC/AR/PSY 作为中性/语境信号, 不直接参与评分(通过阶段间接体现)。"""
    sigs = r.get("signals") or []
    score = max([_BUY_PTS.get(s, 0) for s in sigs] + [0])
    score += sum(_SELL_PTS.get(s, 0) for s in sigs)
    base = (r.get("phase") or "").split(" ")[0]
    if base in ("底部整固", "上升趋势"):
        score += 10
    elif base in ("顶部构筑", "下跌趋势"):
        score -= 10
    q = r.get("conf_q")
    if q == "high":
        score += 5 if base in ("底部整固", "上升趋势") else -5
    elif q == "caution":
        score -= 3
    s20 = r.get("sector20")
    if s20 is not None and s20 != 0:
        score += 6 if s20 > 0 else -6
    return score


def backtest_events(df, events, horizon=20, min_n=3, cost=0.004):
    """因果式回测: 在每个历史时点只用当时已见数据重算枢轴/事件, 消除前瞻偏差。
    返回 {"by_type": {type: stats}, "benchmark": 同期买入持有均值%, "cost": 单边成本}。
    stats 字段 (均为费后%): n/win/avg/med/best/worst/pl_ratio(盈亏比)/vs_bh。

    方向约定 (与 config.event_dir 同向, 同 backtest_vsa):
      多头事件 → 买入持有一期 (ret = close[end]/close[i+1]-1);
      空头事件 → 反向做空等效 (ret = close[i+1]/close[end]-1, 下跌才盈利);
      中性事件 → 按买入持有统计 (无方向含义)。
    因此 win 恒为"方向命中盈利占比", 多头记涨、空头记跌。"""
    n = len(df)
    if n < 150:
        return {"by_type": {}, "benchmark": 0.0, "cost": cost, "note": "样本过短"}
    close = df["close"].values
    locked = df["locked"].values if "locked" in df.columns else np.zeros(n, bool)
    # 采样回测窗口终点 (因果: 只用截至 t 的数据重算信号)
    ends = list(range(90, n - horizon))
    if len(ends) > 60:
        ends = ends[::max(1, len(ends) // 60)]
    per_type = defaultdict(list)
    per_type_ok = defaultdict(list)  # 仅已跟进确认的事件 (研究: 只在确认后进场更准)
    bh = []
    for t in ends:
        wdf = df.iloc[:t + 1]
        wpivots = find_pivots(wdf, order=6)
        wevents = detect_all(wdf, wpivots)
        for e in wevents:
            i = e["idx"]
            if i + 1 >= n or locked[i] or locked[i + 1]:
                continue
            end = min(n - 1, i + horizon)
            d = event_dir(e.get("type", ""))
            if d < 0:
                ret = (close[i + 1] / close[end] - 1) - cost   # 空头: 下跌才盈利
            else:
                ret = (close[end] / close[i + 1] - 1) - cost   # 多头/中性: 买入持有
            per_type[e["type"]].append(ret)
            if e.get("confirmed") is True:
                per_type_ok[e["type"]].append(ret)
        if not locked[t + 1]:
            bh.append(close[min(n - 1, t + horizon)] / close[t + 1] - 1)
    bench = float(np.mean(bh)) * 100 if bh else 0.0
    stats = {}
    for typ, rets in per_type.items():
        if len(rets) < min_n:
            continue
        arr = np.asarray(rets)
        wins = arr[arr > 0]
        losses = arr[arr <= 0]
        if len(wins) and len(losses):
            pl = float(wins.mean()) / float(abs(losses.mean()))
        elif len(wins):
            pl = float("inf")
        else:
            pl = 0.0
        stats[typ] = {
            "n": int(len(arr)),
            "win": float((arr > 0).mean() * 100),
            "avg": float(arr.mean() * 100),
            "med": float(np.median(arr) * 100),
            "best": float(arr.max() * 100),
            "worst": float(arr.min() * 100),
            "pl_ratio": pl,
            "vs_bh": float(arr.mean() * 100) - bench,
        }
        # 已跟进确认子集 (研究: 确认后进场命中率更高) — 样本足才报告
        ok = np.asarray(per_type_ok[typ])
        if len(ok) >= 2:
            stats[typ]["n_confirmed"] = int(len(ok))
            stats[typ]["win_confirmed"] = float((ok > 0).mean() * 100)
            stats[typ]["avg_confirmed"] = float(ok.mean() * 100)
    return {"by_type": stats, "benchmark": bench, "cost": cost, "note": "",
            "horizon": horizon}


def backtest_vsa(df, horizon=20, min_n=3, cost=0.004, scale=240):
    """VSA 标签滚动回测: 在每个历史时点只用当时已见数据重算 VSA 标签,
    消除前瞻偏差 (与 backtest_events 同构)。按标签分组统计未来 horizon 根
    收益 (费后%), 返回 {"by_label": {label: stats}, "benchmark", ...}。

    方向约定 (与 config.vsa_dir 同向, 同 _BUY_PTS/_SELL_PTS 语义):
      看涨标签 (config.VSA_BULL) → 买入持有一期;
      看跌标签 (config.VSA_BEAR) → 反向做空等效 (close[i]/close[end] - 1);
      中性标签 (config.VSA_NEUTRAL, ER/EF/ABS/CHOC/EVR 等) 不统计 (避免噪声)。"""
    from .vsa import vsa_classify
    n = len(df)
    if n < 150:
        return {"by_label": {}, "benchmark": 0.0, "cost": cost, "note": "样本过短"}
    close = df["close"].values
    locked = df["locked"].values if "locked" in df.columns else np.zeros(n, bool)
    BULL = VSA_BULL
    BEAR = VSA_BEAR
    ends = list(range(90, n - horizon))
    if len(ends) > 60:
        ends = ends[::max(1, len(ends) // 60)]
    per = defaultdict(list)
    bh = []
    for t in ends:
        wdf = df.iloc[:t + 1]
        vs = vsa_classify(wdf, scale=scale)
        for s in vs:
            lb = s["label"]
            if lb not in BULL and lb not in BEAR:
                continue
            i = s["idx"]
            if i + 1 >= n or locked[i] or locked[i + 1]:
                continue
            end = min(n - 1, i + horizon)
            if lb in BULL:
                r = close[end] / close[i + 1] - 1
            else:
                r = close[i + 1] / close[end] - 1
            per[lb].append(r - cost)
        if not locked[t + 1]:
            bh.append(close[min(n - 1, t + horizon)] / close[t + 1] - 1)
    bench = float(np.mean(bh)) * 100 if bh else 0.0
    stats = {}
    for lb, rets in per.items():
        if len(rets) < min_n:
            continue
        arr = np.asarray(rets)
        wins = arr[arr > 0]
        losses = arr[arr <= 0]
        if len(wins) and len(losses):
            pl = float(wins.mean()) / float(abs(losses.mean()))
        elif len(wins):
            pl = float("inf")
        else:
            pl = 0.0
        stats[lb] = {
            "n": int(len(arr)),
            "win": float((arr > 0).mean() * 100),
            "avg": float(arr.mean() * 100),
            "med": float(np.median(arr) * 100),
            "best": float(arr.max() * 100),
            "worst": float(arr.min() * 100),
            "pl_ratio": pl,
            "vs_bh": float(arr.mean() * 100) - bench,
        }
    return {"by_label": stats, "benchmark": bench, "cost": cost, "note": "",
            "horizon": horizon}


def robustness_check(df, order6=None):
    """参数微小扰动下的稳定性检查: 换枢轴 order(5/6/7), 看阶段判断与近期事件是否稳定。
    用于警示过拟合 —— 若换参数结论剧烈变化, 说明当前判断脆弱。

    order6: 主链路已算好的 order=6 结果 (pivots, events, phase), 传入时跳过
    重复计算 order=6 的整条流水线 (与主分析默认档 normal 完全一致)。
    """
    phases = []
    recent_sets = []
    try:
        for order in (5, 6, 7):
            if order == 6 and order6 is not None:
                p, ev, ph = order6[0], order6[1], order6[2]
            else:
                p = find_pivots(df, order=order)
                ev = detect_all(df, p)
                ph, _ = judge_phase(df, p, ev)
            phases.append(ph.split(" ")[0])
            recent_sets.append({e["type"] for e in ev if e["idx"] >= len(df) - 60})
    except Exception:
        return {"verdict": "一般", "lines": ["稳健性检查失败"]}
    stable_phase = len(set(phases)) == 1
    if recent_sets:
        inter = set.intersection(*recent_sets)
        union = set.union(*recent_sets)
        overlap = len(inter) / max(1, len(union)) * 100
    else:
        inter, union = set(), set()
        overlap = 0.0
    if stable_phase and overlap >= 50:
        verdict = "稳健"
    elif overlap >= 30:
        verdict = "一般"
    else:
        verdict = "脆弱"
    lines = [f"  枢轴order 5/6/7 阶段: {' | '.join(phases)}",
             f"  近60根事件重合度: {overlap:.0f}% ({len(inter)}/{len(union) or len(inter)})"]
    return {"verdict": verdict, "lines": lines}


def classify_phase(code, datalen=500, use_cache=True):
    """轻量阶段分类 (仅K线, 不抓基本面/资金流/板块), 供自选股差异化高亮。
    返回 {"base": 阶段名, "phase": 完整阶段文本} 或 None。

    use_cache=True 时按自然日缓存结果: 盘中阶段结论变化极慢, 自选股定时刷新
    只更新实时价即可, 不必每 30s 重跑 K线+指标+枢轴+事件+判段全流水线。
    """
    if use_cache:
        with _PHASE_CACHE_LOCK:
            cached = _PHASE_CACHE.get(_phase_cache_key(code, datalen))
            if cached is not None:
                return cached
    try:
        symbol = normalize_symbol(code)
        df = add_indicators(fetch_kline(symbol, datalen=datalen, scale=240), symbol=symbol)
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _ = judge_phase(df, pivots, events)
        out = {"base": phase.split(" ")[0], "phase": phase}
        if use_cache:
            with _PHASE_CACHE_LOCK:
                _PHASE_CACHE[_phase_cache_key(code, datalen)] = out
        return out
    except Exception:
        return None


def scan_stock_signals(code, datalen=500, confirm_enabled=True, on_result=None):
    """扫描单只股票近20根K线的威科夫信号, 供自选股扫描器调用。
    附带确认机制标记 (真实资金流+基本面, fail-soft: 断源时 conf_q="")。
    confirm_enabled=False 时不抓基本面/资金流/板块 (离线快速模式)。
    on_result(df, symbol, code, scale, datalen, name, phase_label, conf_q) 供
    调用方在扫描时顺便记录 accuracy 快照 (避免 backtest↔accuracy 循环导入)。"""
    global _EM_FAIL_STREAK
    try:
        symbol = normalize_symbol(code)
        name = fetch_name(symbol)
        df = add_indicators(fetch_kline(symbol, datalen=datalen, scale=240), symbol=symbol)
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _ = judge_phase(df, pivots, events)
        recent = [e for e in events if e["idx"] >= len(df) - 20]
        priority = ["Spring", "Shakeout", "SOS", "JOC", "SC", "ST", "LPS", "BU", "AR",
                    "PSY", "UTAD", "BC", "LPSY", "UT", "SOW"]
        recent.sort(key=lambda e: (-e.get("conf", 50),
                                   priority.index(e["type"]) if e["type"] in priority else 99))
        # 近期 VSA 标签 (供高命中滚动头条/明细, 免重复计算)
        recent_vsa = [s for s in vsa_classify(df, scale=240)
                      if s["idx"] >= len(df) - 20]
        # 阶段×资金信号 (供预警/扫描列): 仅K线量价资金口径, 全环境可用。
        # 资金背离 = 阶段偏多但近5日量价资金净流出 (诱多/下跌中继, 谨慎);
        # 资金回流 = 阶段偏空但近5日资金净流入 (止跌/反抽, 关注)。
        # 用户可在 自选股预警 里选"出现信号"并填这两个名字, 触发弹窗+语音。
        try:
            from .phases import flow_confirmed
            _fc = flow_confirmed(df)
        except Exception:
            _fc = True
        _base_phase = phase.split(" ")[0]
        _flow_sigs = []
        if _base_phase in ("底部整固", "上升趋势") and not _fc:
            _flow_sigs.append("资金背离")
        elif _base_phase in ("下跌趋势", "顶部构筑") and _fc:
            _flow_sigs.append("资金回流")
        # ── 确认机制 (高置信/需谨慎 + 20日主力 + 板块) ──
        # 熔断生效时跳过确认抓取 (阶段+信号仍由新浪K线给出, 只缺确认列)
        conf_q, flow20, pe = "", None, None
        sector_name, sector20 = None, None
        if confirm_enabled:
            with _EM_LOCK:
                fail_streak = _EM_FAIL_STREAK
            if fail_streak < _EM_FAIL_LIMIT:
                try:
                    fund = fetch_fundamental(symbol)
                    flow = fetch_main_flow(symbol, 120)
                    if flow is not None and len(flow):
                        flow20 = float(flow.tail(20)["main"].sum()) / 1e8
                    sector = None
                    try:
                        s_name, s_flow = fetch_sector_flow(symbol)
                        if s_flow is not None and len(s_flow) >= 20:
                            s_main = float(s_flow.tail(20)["main"].sum())
                            sector_name = s_name
                            sector20 = s_main / 1e8
                            sector = {"name": s_name, "main20": s_main}
                    except Exception:
                        pass
                    conf_q, _items = build_confirm_section(phase, df, fund, flow, None, sector,
                                                           events=events)
                    if fund:
                        pe = fund.get("pe_ttm")
                    with _EM_LOCK:
                        if flow is None and sector is None:
                            _EM_FAIL_STREAK += 1
                        else:
                            _EM_FAIL_STREAK = 0
                except Exception:
                    with _EM_LOCK:
                        _EM_FAIL_STREAK += 1
        if on_result is not None:
            try:
                on_result(df, symbol, str(code)[-6:], 240, datalen, name,
                          phase_label=phase, conf_q=conf_q,
                          precomputed={"pivots": pivots, "events": events,
                                       "phase": phase, "pnf_t": None,
                                       "vsa_signals": None, "fusion": None,
                                       "targets": None, "trade_plan": None})
            except Exception as e:
                from ._log import log_exc
                log_exc(f"scan_stock_signals({code}) on_result 失败", e)
        return {"code": str(code)[-6:], "name": name, "phase": phase,
                "signals": [e["type"] for e in recent] + _flow_sigs,
                "events": [{"type": e["type"], "date": str(e["date"].date()),
                            "price": float(e["price"]), "conf": int(e.get("conf", 50))}
                           for e in recent],
                "vsa": [{"label": s["label"], "date": str(s["date"].date()),
                         "desc": s["desc"]} for s in recent_vsa],
                "details": [f"{e['date'].date()} {e['type']} {e['price']:.2f}(置信{e.get('conf', 50)})"
                            for e in recent[:3]],
                "last": float(df["close"].iloc[-1]),
                "conf_q": conf_q, "flow20": flow20, "pe": pe,
                "sector": sector_name, "sector20": sector20}
    except Exception:
        return None


def scan_sectors():
    """扫描全市场行业板块, 返回板块级统计 (快速模式: 仅用板块指数+资金流)。
    返回 [{name, bk_code, price, pct, flow20, flow20_yi, tone, score, live}, ...] 按 score 降序。
    tone: bullish(流入+上涨)/bearish(流出+下跌)/mixed(方向矛盾)/neutral
    live=False 表示 push2 不可达, 兜底离线列表。"""
    stats = fetch_all_board_stats()
    results = []
    for s in stats:
        flow_raw = s.get("flow20", 0)
        pct = s.get("pct", 0)
        live = s.get("live", False)
        if not live:
            results.append({"name": s["name"], "bk_code": s["bk_code"],
                            "price": 0, "pct": 0, "flow20": 0,
                            "flow20_yi": 0, "tone": "neutral", "score": 0, "live": False})
            continue
        # 单位检测: EM 源(元) > 1e6, THS 源(亿) < 1e4
        if abs(flow_raw) > 1e6:
            flow = flow_raw / 1e8  # 元 → 亿
        elif abs(flow_raw) < 1e4:
            flow = flow_raw  # 已是亿
        else:
            flow = flow_raw / 1e4  # 万元 → 亿
        if flow > 0.5 and pct > 0:
            tone = "bullish"
            score = min(100, abs(flow) * 5 + pct * 3 + 40)
        elif flow < -0.5 and pct < 0:
            tone = "bearish"
            score = min(100, abs(flow) * 5 + abs(pct) * 3 + 40)
        elif flow > 0.5:
            tone = "mixed"
            score = min(100, abs(flow) * 5 + 30)
        elif flow < -0.5:
            tone = "mixed"
            score = min(100, abs(flow) * 5 + 30)
        elif pct > 1:
            tone = "mixed"
            score = min(100, abs(pct) * 3 + 25)
        elif pct < -1:
            tone = "mixed"
            score = min(100, abs(pct) * 3 + 25)
        else:
            tone = "neutral"
            score = 10
        results.append({"name": s["name"], "bk_code": s["bk_code"],
                        "price": s["price"], "pct": pct, "flow20": s["flow20"],
                        "flow20_yi": flow, "tone": tone, "score": round(score, 1),
                        "live": True})
    results.sort(key=lambda x: (-x["score"], x["name"]))
    return results


def scan_sector_stocks(bk_code: str, board_name: str, limit: int = 30,
                       confirm_enabled: bool = True, on_result=None):
    """扫描单个板块成份股。
    主源: 东财 push2 fetch_board_constituents → 后备: 全市场活跃股 + 板块名过滤 → 兜底: 空列表。
    返回 [{code, name, last, phase, conf_q, flow20, sector, score, signals}, ...]。"""
    stocks = fetch_board_constituents(bk_code, limit=limit)
    if not stocks:
        # 后备: 从全市场活跃股中按板块名匹配 (Sina 数据源可用)
        stocks = _fallback_constituents(board_name, limit)
    results = []
    for prefix_code, sname, sprice in stocks:
        code = prefix_code[-6:]
        try:
            r = scan_stock_signals(code, datalen=500, confirm_enabled=confirm_enabled,
                                   on_result=on_result)
            if r is None:
                r = {"code": code, "name": sname, "phase": "抓取失败", "signals": [],
                     "last": sprice, "conf_q": "", "flow20": None, "pe": None,
                     "sector": board_name, "sector20": None}
            else:
                r["sector"] = board_name
            r["score"] = signal_score(r)
            results.append(r)
        except Exception:
            results.append({"code": code, "name": sname, "phase": "错误", "signals": [],
                            "last": sprice, "conf_q": "", "flow20": None, "pe": None,
                            "sector": board_name, "sector20": None, "score": 0})
    results.sort(key=lambda x: (-x.get("score", 0), x["code"]))
    return results


def _fallback_constituents(board_name: str, limit: int = 30):
    """后备成份股: THS 领涨股(Sina代码查找) → akshare EM 接口 → 空列表。
    返回 [(prefix_code, name, price), ...]。"""
    # 1) THS 领涨股 → Sina 搜代码 (最可靠后备)
    try:
        leaders = _get_ths_leader_stocks(board_name)
        if leaders:
            return leaders[:limit]
    except Exception:
        pass
    # 2) akshare EM 接口 (自带重试)
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=board_name)
        if df is not None and len(df) > 0:
            stocks = []
            for _, row in df.head(limit).iterrows():
                code = str(row.get("代码", "")).strip()
                name = str(row.get("名称", "")).strip()
                price = float(row.get("最新价", 0) or 0)
                if not code:
                    continue
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                if code.startswith(("8", "4")):
                    prefix = "bj"
                stocks.append((f"{prefix}{code}", name, price))
            if stocks:
                return stocks
    except Exception:
        pass
    return []


def _get_ths_leader_stocks(board_name: str):
    """从 THS 行业板块摘要中提取领涨股, 用 Sina 搜索API 查找代码。
    返回 [(prefix_code, name, price), ...] 或 []。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_summary_ths()
        row = df[df["板块"] == board_name]
        if len(row) == 0:
            return []
        leader_name = str(row.iloc[0].get("领涨股", ""))
        leader_price = float(row.iloc[0].get("领涨股-最新价", 0) or 0)
        if not leader_name or leader_name == "nan":
            return []
        # Sina suggest API 搜代码
        url = f"https://suggest3.sinajs.cn/suggest/type=11&key={leader_name}"
        r = http_session().get(url, headers={"User-Agent": "Mozilla/5.0",
                                        "Referer": "https://finance.sina.com.cn/"},
                         timeout=8)
        if r.status_code != 200 or not r.text:
            return []
        # 格式: var suggestvalue="名称,市场,代码,完整代码,...";
        import re
        m = re.search(r'"([^"]*)"', r.text)
        if not m:
            return []
        parts = m.group(1).split(",")
        if len(parts) < 4:
            return []
        full_code = parts[3].strip()  # e.g. sz301040
        if not full_code or len(full_code) < 8:
            return []
        prefix = full_code[:2]
        code6 = full_code[2:]
        return [(f"{prefix}{code6}", leader_name, leader_price)]
    except Exception:
        return []
