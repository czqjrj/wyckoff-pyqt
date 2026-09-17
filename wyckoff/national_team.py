"""国家队资金流入/流出统一判定 (聚合三通道 → 一个结论 + 评分)。

数据通道 (按时效排序, 全部 fail-soft):
  1. ETF 日频主力资金流 (wyckoff.nteam.track_nteam): 近1/5/20日主力净流入
     与异动信号 (疑似买入/减仓)。非官方国家队数据, 属资金流代理。
  2. ETF 三因子份额监测 (wyckoff.etf_factor.monitor_etfs): 量能+方向+份额
     概率信号。份额变化≠一定是国家队, 概率性信号。
  3. 季报十大股东 (wyckoff.holdings.fetch_nt_holdings): 汇金/证金/社保等
     在核心持仓大盘股上的季度加/减仓, 确定性最高但滞后一个季度。

聚合逻辑:
  - 三通道各自产出 [-1, 1] 的分项得分, 权重为 ETF日频 0.5 / 三因子 0.25 /
    季报持仓 0.25; 缺数据的通道自动剔除并按可用权重重新归一。
  - 总分 ≥0.2 → 净流入; ≤-0.2 → 净流出; 其余 → 双向平衡。
  - reasoning 记录每通道的判定依据。

对外接口:
  - aggregate_nt(etf_results, factor_results, holdings_results) -> dict  纯函数可测
  - national_team_flow(max_stocks=6) -> dict  完整管线 (联网)
  - fmt_nt_flow(res) -> str  人类可读摘要
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .etf_factor import monitor_etfs
from .holdings import fetch_nt_holdings
from .nteam import track_nteam

# 季报通道的样本篮子: 汇金/证金历史上持续持有的核心大票 (权重排序)
NT_BASKET = [
    ("sh601398", "工商银行"),
    ("sh601288", "农业银行"),
    ("sh601939", "建设银行"),
    ("sh601988", "中国银行"),
    ("sh601857", "中国石油"),
    ("sh601318", "中国平安"),
    ("sh600519", "贵州茅台"),
    ("sh600028", "中国石化"),
]

# 通道权重
W_DAILY, W_FACTOR, W_HOLD = 0.5, 0.25, 0.25
# 总分判定阈值
INFLOW_T, OUTFLOW_T = 0.2, -0.2
# 归一化量程 (亿)
NET1_SCALE, NET20_SCALE = 10.0, 50.0

# 净值化: 正常买入/减少买入
_ADD_ST = ("加仓", "新进")
_CUT_ST = ("减仓", "退出")

_BASKET_TTL = 3600 * 6  # 季报数据缓存 6 小时
_BASKET_CACHE = {}
_LOCK = threading.Lock()


def aggregate_nt(etf_results, factor_results, holdings_results):
    """聚合三通道为统一判定。参数均为通道返回的原始结果列表, 纯计算不联网。

    返回 {
      verdict, score, bias, levels: {...}, etf: {...}, factor: {...},
      holdings: {...}, reasoning: [...], made_at, channel_states
    }
    """
    lv = {
        "daily": _score_daily(etf_results),
        "factor": _score_factor(factor_results),
        "holdings": _score_holdings(holdings_results),
    }
    _W = {"daily": W_DAILY, "factor": W_FACTOR, "holdings": W_HOLD}
    avail = [(lv[k]["score"], _W[k]) for k in _W
             if lv[k]["avail"]]
    total_w = sum(w for _, w in avail)
    if total_w <= 0 or not avail:
        score = 0.0
        verdict = "数据不足"
        bias = "平衡"
    else:
        score = sum(s * w for s, w in avail) / total_w
        bias = "流入" if score >= INFLOW_T else ("流出" if score <= OUTFLOW_T else "平衡")
        verdict = ("净流入" if score >= INFLOW_T
                   else "净流出" if score <= OUTFLOW_T else "双向平衡")
    reasoning = [lv[k]["reason"] for k in ("daily", "factor", "holdings")
                 if lv[k]["reason"]]
    return {
        "verdict": verdict,
        "score": round(score, 3),
        "bias": bias,
        "levels": lv,
        "etf": _agg_etf(etf_results),
        "factor": _agg_factor(factor_results),
        "holdings": _agg_holdings(holdings_results),
        "reasoning": reasoning,
        "made_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel_states": {k: ("ok" if lv[k]["avail"] else "down")
                           for k in lv},
    }


def _score_daily(results):
    """ETF 日频主力资金流通道 → [-1, 1]。"""
    rows = [r for r in results if isinstance(r, dict) and r.get("y1") is not None]
    if len(rows) < len(results) * 0.5 or not rows:
        return {"score": 0.0, "avail": False,
                "reason": "ETF主力资金流数据断连, 通道剔除"}
    net1 = sum(r.get("y1") or 0.0 for r in rows)
    net20 = sum(r.get("y20") or 0.0 for r in rows)
    buy = sum(1 for r in rows if "买入" in r.get("verdict", ""))
    sell = sum(1 for r in rows if "减仓" in r.get("verdict", ""))
    s = (0.5 * _tanh(net1 / NET1_SCALE)
         + 0.3 * _tanh(net20 / NET20_SCALE)
         + 0.2 * ((buy - sell) / len(rows)))
    return {"score": _clip(s), "avail": True,
            "reason": (f"11只国家队宽基ETF近1日主力净流入 {net1:+.1f}亿, "
                       f"近20日 {net20:+.1f}亿, 疑似买入 {buy} / 减仓 {sell}")}


def _score_factor(results):
    """ETF 三因子通道 → [-1, 1]。信号全中性时视为无贡献。"""
    valid = [r for r in results if isinstance(r, dict)
             and r.get("buy_prob") is not None]
    rows = [r for r in valid
            if r.get("direction") not in ("none", "数据不足")]
    if not valid:
        return {"score": 0.0, "avail": False,
                "reason": "ETF三因子份额数据不足, 通道剔除"}
    if not rows:
        return {"score": 0.0, "avail": False,
                "reason": f"ETF三因子 {len(valid)}只全部信号中性, 通道无贡献"}
    diff = sum((r.get("buy_prob") or 0.0) - (r.get("sell_prob") or 0.0)
               for r in rows) / len(rows)
    buys = sum(1 for r in rows if r.get("direction") == "buy")
    sells = sum(1 for r in rows if r.get("direction") == "sell")
    s = 0.7 * _clip(diff * 2.0) + 0.3 * ((buys - sells) / len(rows))
    return {"score": _clip(s), "avail": True,
            "reason": (f"ETF三因子: 平均买概率-卖概率 {diff:+.2f}, "
                       f"方向买 {buys} / 卖 {sells} ({len(rows)}只有效)")}


def _score_holdings(results):
    """季报十大股东通道 → [-1, 1]。"""
    rows = [r for r in results if isinstance(r, dict) and r.get("holders")]
    if not rows:
        return {"score": 0.0, "avail": False,
                "reason": "季报十大股东持仓无有效数据, 通道剔除"}
    add = sum(1 for r in rows for h in r["holders"]
              if h.get("status") in _ADD_ST)
    cut = sum(1 for r in rows for h in r["holders"]
              if h.get("status") in _CUT_ST)
    esc = sum(1 for r in rows for h in r.get("exited") or [])
    cut += esc
    total = max(1, add + cut)
    return {"score": _clip((add - cut) / total), "avail": True,
            "reason": (f"季报十大股东核心样本: 国家队加仓/新进 {add} 家, "
                       f"减仓/退出 {cut} 家 ({len(rows)}只样本)")}


def _agg_etf(results):
    rows = [r for r in results if isinstance(r, dict)]
    return {
        "total": len(rows),
        "buy": sum(1 for r in rows if "买入" in r.get("verdict", "")),
        "sell": sum(1 for r in rows if "减仓" in r.get("verdict", "")),
        "inflow": sum(1 for r in rows if r.get("verdict") == "净流入"),
        "outflow": sum(1 for r in rows if r.get("verdict") == "净流出"),
        "proxy": sum(1 for r in rows if r.get("source") == "proxy"),
        "down": sum(1 for r in rows if r.get("verdict") == "数据断连"),
        "net_y1": round(sum(r.get("y1") or 0.0 for r in rows), 2),
        "net_y20": round(sum(r.get("y20") or 0.0 for r in rows), 2),
    }


def _agg_factor(results):
    rows = [r for r in results if isinstance(r, dict)
            and r.get("buy_prob") is not None]
    return {
        "valid": len(rows),
        "buy": sum(1 for r in rows if r.get("direction") == "buy"),
        "sell": sum(1 for r in rows if r.get("direction") == "sell"),
        "avg_diff": round(sum((r.get("buy_prob") or 0.0)
                              - (r.get("sell_prob") or 0.0)
                              for r in rows) / len(rows), 3) if rows else 0.0,
    }


def _agg_holdings(results):
    rows = [r for r in results if isinstance(r, dict)]
    add = sum(1 for r in rows for h in r.get("holders") or []
              if h.get("status") in _ADD_ST)
    cut = sum(1 for r in rows for h in r.get("holders") or []
              if h.get("status") in _CUT_ST)
    cut += sum(1 for r in rows for h in r.get("exited") or [])
    sample = sum(1 for r in rows if r.get("holders"))
    return {"sample": sample, "add": add, "cut": cut}


def national_team_flow(max_stocks=6):
    """完整管线: 并发拉取三通道并聚合。全部 fail-soft, 单通道断连不影响其它。"""
    from datetime import datetime
    with ThreadPoolExecutor(max_workers=4) as ex:
        etf_f = ex.submit(track_nteam, False)
        factor_f = ex.submit(monitor_etfs)
        hold_f = ex.submit(_fetch_basket, max_stocks)
        etf_results = _safe(etf_f, "国家队ETF跟踪")
        factor_results = _safe(factor_f, "ETF三因子监测")
        holdings_results = _safe(hold_f, "国家队季报持仓")
    res = aggregate_nt(etf_results, factor_results, holdings_results)
    res["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    res["etf_rows"] = etf_results
    res["factor_rows"] = factor_results
    res["holdings_rows"] = holdings_results
    return res


def _safe(future, tag):
    try:
        return future.result()
    except Exception as e:
        from ._log import log_exc
        log_exc(f"{tag}失败", e)
        return []


def _fetch_basket(max_stocks, force=False):
    """并发抓取样本篮子的季报国家队持仓 (结果按报表期缓存)。"""
    stocks = {s for s, _ in NT_BASKET[:max_stocks]}
    todo = []
    with _LOCK:
        for s in stocks:
            if force or s not in _BASKET_CACHE \
                    or time.time() - _BASKET_CACHE[s][0] > _BASKET_TTL:
                todo.append(s)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_nt_holdings, s, 2): s for s in todo}
        for f in futures:
            s = futures[f]
            try:
                res = f.result()
            except Exception:
                res = None
            if res:
                with _LOCK:
                    _BASKET_CACHE[s] = (time.time(), s, res)
    return [res for s in stocks
            for res in [_BASKET_CACHE.get(s, (None, s, None))[2]]
            if res]


def fmt_nt_flow(res):
    """人类可读摘要 (UI / 日志 / 推送复用)。"""
    head = (f"国家队资金[{res.get('verdict')}]({res.get('bias')}) "
            f"总分 {res.get('score'):+.2f}")
    if not res.get("channel_states"):
        return head
    states = {k: ("有" if v == "ok" else "无") for k, v in
              res["channel_states"].items()}
    one = (f"{head}\n"
           f"ETF日频 {states.get('daily','')} 三因子 {states.get('factor','')} "
           f"季报持仓 {states.get('holdings','')}\n")
    return one + "\n".join(f"· {r}" for r in res.get("reasoning", []))


def _tanh(x):
    import math
    return math.tanh(x)


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))
