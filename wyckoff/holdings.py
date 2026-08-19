# -*- coding: utf-8 -*-
"""国家队持仓透视: 十大股东 → 汇金/证金/社保/养老 识别 → 季度增减持 → 建仓成本估算。

数据源: akshare stock_gdfx_top_10_em (东财数据中心, 季报十大股东/十大流通股东)。
思路: 逐季抓取十大股东, 按机构名关键词识别国家队; 对比相邻季度持股数判定
加仓/减仓/新进/退出; 用"建仓季度前 90 日 VWAP × 0.95"估算建仓成本 (与
nt_project 口径一致)。全部 fail-soft, 单源/单季失败不影响其它期。

局限 (与 nt_project 相同):
  - 季度数据, 滞后: 只能看到季报披露的时点持仓快照, 看不到季度内的买卖。
  - 持股跌出前十即失联 (系统只追踪前十大股东)。
  - 成本为估算值, 未计入分红与高抛低吸, 参考意义为主。
"""
import threading
import time
from datetime import datetime

import pandas as pd

from .datasource import fetch_kline, fetch_realtime

# 国家队机构关键词 → 分类 (顺序敏感: 先匹配更具体的机构)
NATL_KEYWORDS = (
    ("中央汇金", ("中央汇金", "汇金资管", "中国汇金")),
    ("证金", ("中国证券金融", "证金公司", "中证金融")),
    ("社保", ("全国社会保障基金", "社保基金")),
    ("养老", ("基本养老保险基金", "养老基金")),
    ("外管局", ("外汇管理局", "梧桐树")),
)

# 季报披露股东名单的报表期 (季度末)。当前季度未披露时接口会失败, 自动跳过。
_REPORT_CACHE = {}
_REPORT_TTL = 3600  # 股东快照缓存 1 小时
_REPORT_LOCK = threading.Lock()


def classify_holder(name: str):
    """按机构名识别国家队类型, 返回 '中央汇金'/'证金'/'社保'/'养老'/'外管局' 或 None。"""
    if not name:
        return None
    for cat, kws in NATL_KEYWORDS:
        if any(kw in name for kw in kws):
            return cat
    return None


def report_dates(n: int = 6) -> list:
    """最近 n 个季度末报表期 (YYYYMMDD), 从当前季度向前回溯。"""
    now = datetime.now()
    out = []
    q = (now.month - 1) // 3
    y = now.year
    while len(out) < n:
        out.append(f"{y}{{}}".format("0331" if q == 0 else "0630" if q == 1
                                     else "0930" if q == 2 else "1231"))
        q -= 1
        if q < 0:
            q = 3
            y -= 1
    return out


def fetch_top10(symbol: str, report_date: str) -> list:
    """抓取某报表期十大股东, 返回 [{rank, name, shares, pct, change, change_ratio}] 或 []。"""
    key = (symbol, report_date)
    now = time.time()
    cached = _REPORT_CACHE.get(key)
    if cached and now - cached[0] < _REPORT_TTL:
        return cached[1]
    out = []
    try:
        import akshare as ak
        df = ak.stock_gdfx_top_10_em(symbol=symbol, date=report_date)
        for _, r in df.iterrows():
            out.append({
                "rank": int(r["名次"]),
                "name": str(r["股东名称"]),
                "shares": _to_float(r.get("持股数")),
                "pct": _to_float(r.get("占总股本持股比例")),
                "change": _to_float(r.get("增减")),
                "change_ratio": _to_float(r.get("变动比率")),
            })
    except Exception:
        out = []
    with _REPORT_LOCK:
        _REPORT_CACHE[key] = (now, out)
    return out


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s and s not in ("nan", "-") else None
    except (ValueError, TypeError):
        return None


def fetch_nt_holdings(symbol: str, max_quarters: int = 6) -> dict:
    """逐季抓十大股东, 识别国家队机构并追踪增减持。

    返回 {
      latest_report: 最近有国家队持仓的报表期,
      report_dates: 成功抓到的报表期列表,
      holders: 最新一期国家队持仓明细 [{name, category, shares, pct, change,
               change_ratio, status, first_report, cost, price, pnl_pct}],
      exited: 曾持有但最新一期跌出前十/退出的机构 [{name, category, last_report}],
    }"""
    rows = []
    for rd in report_dates(max_quarters):
        holders = fetch_top10(symbol, rd)
        if not holders:
            continue
        natl = {}
        for h in holders:
            cat = classify_holder(h["name"])
            if cat:
                natl[h["name"]] = {
                    "category": cat, "shares": h["shares"], "pct": h["pct"],
                    "change": h["change"], "change_ratio": h["change_ratio"],
                }
        rows.append({"report": rd, "national": natl})
    # rows 按新→旧排列; 取最新的、且含国家队持仓的一期
    latest_idx = next((i for i, r in enumerate(rows) if r["national"]), None)
    if latest_idx is None:
        return {"latest_report": None, "report_dates": [r["report"] for r in rows],
                "holders": [], "exited": []}
    latest = rows[latest_idx]
    older = rows[latest_idx + 1:]  # 更早的报表期 (新→旧)

    # 首见期: 从最旧扫到最新, 记录每个机构首次出现在十大股东的报表期
    first_seen = {}
    for r in reversed(rows):
        for name in r["national"]:
            first_seen.setdefault(name, r["report"])

    holders = []
    for name, info in latest["national"].items():
        first = first_seen[name]
        if first == latest["report"]:
            status = "新进"
        elif (info["change"] or 0) > 0:
            status = "加仓"
        elif (info["change"] or 0) < 0:
            status = "减仓"
        else:
            status = "维持"
        cost = estimate_build_cost(symbol, first)
        cur_price = _current_price(symbol)
        pnl = None
        if cost and cost["cost"] and cur_price:
            pnl = (cur_price / cost["cost"] - 1) * 100
        holders.append({
            "name": name, "category": info["category"],
            "shares": info["shares"], "pct": info["pct"],
            "change": info["change"], "change_ratio": info["change_ratio"],
            "status": status, "first_report": first,
            "cost": (cost["cost"] if cost else None),
            "price": cur_price, "pnl_pct": pnl,
        })

    exited = []
    for r in older:
        for n in r["national"]:
            if n not in latest["national"] and not any(e["name"] == n for e in exited):
                exited.append({"name": n, "category": r["national"][n]["category"],
                               "last_report": r["report"]})

    return {
        "latest_report": latest["report"],
        "report_dates": [r["report"] for r in rows],
        "holders": holders,
        "exited": exited,
    }


def estimate_build_cost(symbol: str, report_date: str, window: int = 90,
                        discount: float = 0.95):
    """建仓成本估算: 报表期前 window 个交易日 VWAP × discount (nt_project 口径)。"""
    try:
        end = pd.to_datetime(report_date)
    except (ValueError, TypeError):
        return None
    try:
        df = fetch_kline(symbol, datalen=700, scale=240)
    except Exception:
        return None
    seg = df[df["day"] <= end].tail(window)
    if len(seg) < 20:
        return None
    vwap = float((seg["close"] * seg["volume"]).sum() / seg["volume"].sum())
    return {"vwap": vwap, "cost": vwap * discount,
            "start": str(seg["day"].iloc[0])[:10], "end": str(seg["day"].iloc[-1])[:10]}


def _current_price(symbol: str):
    try:
        info = fetch_realtime([symbol]).get(symbol[2:], {})
        if info.get("price"):
            return float(info["price"])
    except Exception:
        pass
    try:
        df = fetch_kline(symbol, datalen=60, scale=240)
        return float(df["close"].iloc[-1])
    except Exception:
        return None


def format_shares(n):
    """持股数格式化为可读字符串 (亿/万)。"""
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1e8:
        return f"{n / 1e8:,.2f}亿股"
    if a >= 1e4:
        return f"{n / 1e4:,.2f}万股"
    return f"{n:,.0f}"
