# -*- coding: utf-8 -*-
"""国家队 ETF 跟踪: 用主力资金流/量价代理监测汇金证金常用宽基 ETF 的买卖异动。

说明: 无法直接获取国家队持仓明细, 本模块以"异常资金流入/流出"作为代理信号。
  - 主数据源: 东财主力资金流 (fetch_main_flow)。该接口偶发断连 (工具已 fail-soft),
    此时自动切换量价代理 (实体额×量比)。
  - 异动判定 (资金流可用时): 当日主力净流入绝对额 ≥ 近20日均值的 SPIKE_RATIO 倍
    且 ≥ MIN_FLOW_YI 亿 → 方向为正标"疑似买入", 为负标"疑似减仓"。
  - 量价代理: 当日量比 ≥ VOL_RATIO 且实体额同向 → 标"(量价)"。

返回每条: {code, name, symbol, price, pct, y1, y5, y20, ratio, verdict, source}
  y1/y5/y20: 近1/5/20日主力净流入 (亿元); proxy/断连时部分为 None。
  verdict: 疑似买入/净流入/正常/净流出/疑似减仓/疑似买入(量价)/疑似减仓(量价)/数据断连
"""
from concurrent.futures import ThreadPoolExecutor

from .datasource import fetch_realtime, fetch_kline
from .fundamental import fetch_main_flow
from .indicators import add_indicators

# 汇金/证金历史上公开使用过的宽基 ETF 买入载体
NTEAM_ETFS = [
    ("sh510300", "沪深300ETF华泰柏瑞"),
    ("sh510050", "上证50ETF华夏"),
    ("sh510500", "中证500ETF南方"),
    ("sh510310", "沪深300ETF易方达"),
    ("sz159919", "沪深300ETF嘉实"),
    ("sh510330", "沪深300ETF华夏"),
    ("sh588000", "科创50ETF华夏"),
    ("sz159915", "创业板ETF易方达"),
    ("sh512100", "中证1000ETF南方"),
    ("sz159949", "创业板50ETF华安"),
    ("sh510880", "上证红利ETF"),
]

SPIKE_RATIO = 2.5     # 单日主力净流入相对近20日均值放大倍数 → 异动
MIN_FLOW_YI = 1.0     # 异动判定所需最小绝对额 (亿元)
VOL_RATIO = 2.0       # 量价代理: 量比阈值

_ORDER = {"疑似买入": 0, "疑似买入(量价)": 0, "净流入": 1, "正常": 2,
          "净流出": 3, "疑似减仓": 4, "疑似减仓(量价)": 4, "数据断连": 5}


def em_flow_available(timeout=3):
    """快速探针: 东财主力资金流接口是否可用 (避免断连时逐只卡12s×3重试)。"""
    import requests
    try:
        r = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={"secid": "1.510300", "fields1": "f1,f2,f3,f7",
                    "fields2": ("f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,"
                                "f61,f62,f63,f64,f65"),
                    "klt": "101", "lmt": "3"},
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://quote.eastmoney.com/"},
            timeout=timeout)
        if r.status_code != 200:
            return False
        kl = ((r.json().get("data") or {}).get("klines")) or []
        return bool(kl)
    except Exception:
        return False


def track_nteam(force=False):
    """跟踪全部国家队 ETF, 返回按信号强度排序的结果列表。"""
    codes = [s[2:] for s, _ in NTEAM_ETFS]
    rt = fetch_realtime(codes)
    flow_ok = em_flow_available()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_track_one, symbol, name, rt.get(symbol[2:], {}),
                             flow_ok)
                   for symbol, name in NTEAM_ETFS]
        out = [f.result() for f in futures]
    out.sort(key=lambda r: (_ORDER.get(r["verdict"], 5), -abs(r.get("y1") or 0)))
    return out


def _track_one(symbol, name, info, flow_ok=True):
    code = symbol[2:]
    base = {"code": code, "name": name, "symbol": symbol,
            "price": info.get("price"), "pct": info.get("pct"),
            "y1": None, "y5": None, "y20": None, "ratio": None,
            "verdict": "正常", "source": "none", "detail": ""}
    flow = None
    if flow_ok:
        try:
            flow = fetch_main_flow(symbol, 120)
        except Exception:
            flow = None
    if flow is not None and len(flow) >= 21:
        f1 = float(flow.iloc[-1]["main"])
        f5 = float(flow.tail(5)["main"].sum())
        f20 = float(flow.tail(20)["main"].sum())
        hist = flow.iloc[-21:-1]["main"].abs()
        avg_abs = float(hist.mean()) if len(hist) else 0.0
        base["y1"], base["y5"], base["y20"] = f1 / 1e8, f5 / 1e8, f20 / 1e8
        base["ratio"] = round(abs(f1) / avg_abs, 2) if avg_abs > 0 else 0.0
        base["source"] = "flow"
        if base["ratio"] >= SPIKE_RATIO and abs(f1) >= MIN_FLOW_YI * 1e8:
            base["verdict"] = "疑似买入" if f1 > 0 else "疑似减仓"
        elif f20 > 0.5e8:
            base["verdict"] = "净流入"
        elif f20 < -0.5e8:
            base["verdict"] = "净流出"
        return base
    # 东财资金流断连 → 量价代理
    try:
        df = add_indicators(fetch_kline(symbol, datalen=60, scale=240))
        if df is not None and len(df) >= 21:
            body = (df["close"] - df["open"]) * df["volume"]
            f1 = float(body.iloc[-1])
            prev = float(df["volume"].iloc[-21:-1].mean())
            vol_ratio = float(df["volume"].iloc[-1]) / prev if prev > 0 else 0.0
            base["y1"] = f1 / 1e8
            base["ratio"] = round(vol_ratio, 2)
            base["source"] = "proxy"
            base["detail"] = f"量比{vol_ratio:.1f}"
            if vol_ratio >= VOL_RATIO:
                base["verdict"] = ("疑似买入(量价)" if f1 > 0
                                   else "疑似减仓(量价)")
            else:
                base["verdict"] = "正常"
            return base
    except Exception:
        pass
    base["verdict"] = "数据断连"
    return base


def nteam_summary(results):
    """汇总: {total, buy, sell, inflow, outflow, proxy, down}"""
    buy = sum(1 for r in results if "买入" in r["verdict"])
    sell = sum(1 for r in results if "减仓" in r["verdict"])
    inflow = sum(1 for r in results if r["verdict"] == "净流入")
    outflow = sum(1 for r in results if r["verdict"] == "净流出")
    proxy = sum(1 for r in results if r.get("source") == "proxy")
    down = sum(1 for r in results if r["verdict"] == "数据断连")
    return {"total": len(results), "buy": buy, "sell": sell,
            "inflow": inflow, "outflow": outflow, "proxy": proxy, "down": down}
