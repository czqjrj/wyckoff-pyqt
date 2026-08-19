# -*- coding: utf-8 -*-
"""A股补充数据源 (可选依赖 akshare): 北向/龙虎榜/两融/解禁/业绩预告。

全部 fail-soft: akshare 未安装、接口变动、网络不可达 → 返回 [] / 抛异常,
由调用方 (scan_adv) 兜底。接口变动时只改本文件中 fetch_* 的解析, 上层扫描逻辑不变。
"""
import datetime as _dt

_ak_available = None


def _ak():
    """惰性导入 akshare; 不可用返回 None。"""
    global _ak_available
    if _ak_available is None:
        try:
            import akshare as ak  # noqa: PLC0415
            _ak_available = ak
        except Exception:
            _ak_available = False
    return _ak_available or None


def _cell(row, *keys, default=None):
    """从 dict/Series 取第一个存在的 key (兼容 iterrows 的 Series)。"""
    getter = row.get if hasattr(row, "get") else (lambda k, d=default: d)
    for k in keys:
        v = getter(k)
        if v is not None and not (isinstance(v, float) and v != v):  # 剔除 NaN
            return v
    return default


def _num(x):
    try:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.replace(",", "").replace("%", "")
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _code6(code):
    s = str(code or "").strip()
    return s[-6:] if len(s) >= 6 else s


# ──────────────────────────── 龙虎榜 ────────────────────────────

def fetch_lhb_detail(start_date=None, end_date=None, lookback_days=7):
    """龙虎榜明细。返回 [{code,name,date,net,reason,last}, ...] (net 单位: 元)。"""
    ak = _ak()
    if ak is None:
        return []
    end = end_date or _dt.date.today()
    start = start_date or (end - _dt.timedelta(days=lookback_days))
    df = ak.stock_lhb_detail_em(start_date=start.strftime("%Y%m%d"),
                                end_date=end.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        net = _num(r.get("龙虎榜净买额"))
        out.append({
            "code": _code6(r.get("代码")),
            "name": str(r.get("名称") or ""),
            "date": str(r.get("上榜日") or ""),
            "net": net,
            "reason": str(r.get("上榜原因") or ""),
            "last": _num(r.get("收盘价")),
            "pct": _num(r.get("涨跌幅")),
            "inst": _num(r.get("解读")),
        })
    return out


def fetch_lhb_stats(symbol="近一月"):
    """龙虎榜个股统计 (近一月)。返回 [{code,name,times,net,inst_net,last,pct_1m}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    df = ak.stock_lhb_stock_statistic_em(symbol=symbol)
    out = []
    for _, r in df.iterrows():
        out.append({
            "code": _code6(r.get("代码")),
            "name": str(r.get("名称") or ""),
            "times": int(_num(r.get("上榜次数")) or 0),
            "net": _num(r.get("龙虎榜净买额")),
            "inst_net": _num(r.get("机构买入净额")),
            "last": _num(r.get("收盘价")),
            "pct_1m": _num(r.get("近1个月涨跌幅")),
        })
    return out


# ──────────────────────────── 两融 ────────────────────────────

def fetch_margin(days_ago=0):
    """融资融券明细 (沪深主板 BSE 合并)。返回 [{code,name,mrg_bal,sec_bal,last}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    day = (_dt.date.today() - _dt.timedelta(days=days_ago))
    d = day.strftime("%Y%m%d")
    out = {}
    try:
        df = ak.stock_margin_detail_sse(date=d)
        for _, r in df.iterrows():
            code = _code6(r.get("标的证券代码"))
            if not code:
                continue
            out[code] = {
                "code": code,
                "name": str(r.get("标的证券简称") or ""),
                "mrg_bal": _num(r.get("融资余额")),
                "sec_bal": _num(r.get("融券余额")),
                "date": d,
            }
    except Exception:
        pass
    try:
        df = ak.stock_margin_detail_szse(date=d)
        for _, r in df.iterrows():
            code = _code6(r.get("证券代码"))
            if not code:
                continue
            out[code] = {
                "code": code,
                "name": str(r.get("证券简称") or ""),
                "mrg_bal": _num(r.get("融资余额")),
                "sec_bal": _num(r.get("融券余额")),
                "date": d,
            }
    except Exception:
        pass
    return list(out.values())


# ──────────────────────────── 解禁 ────────────────────────────

def fetch_restricted(days=60, min_ratio=1.0):
    """未来 N 日限售解禁明细。返回 [{code,name,date,value,ratio,type,last,pct20}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    start = _dt.date.today()
    end = start + _dt.timedelta(days=days)
    df = ak.stock_restricted_release_detail_em(
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        ratio = _num(r.get("占解禁前流通市值比例"))
        if ratio is None or ratio * 100 < min_ratio:
            continue
        out.append({
            "code": _code6(r.get("股票代码")),
            "name": str(r.get("股票简称") or ""),
            "date": str(r.get("解禁时间") or ""),
            "value": _num(r.get("实际解禁市值")),
            "ratio": ratio * 100,
            "type": str(r.get("限售股类型") or ""),
            "last": _num(r.get("解禁前一交易日收盘价")),
            "pct20": _num(r.get("解禁前20日涨跌幅")),
        })
    return out


# ──────────────────────────── 业绩预告 ────────────────────────────

def fetch_yjyg():
    """本期业绩预告 (以当前季度为报告期)。返回 [{code,name,kind,ampl,msg,date,last}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    today = _dt.date.today()
    q = (today.month - 1) // 3
    period = _dt.date(today.year, 3, 31) if q == 0 else _dt.date(today.year, 6, 30)
    if (today - period).days < 30:  # 距报告期<1月时回溯上一期
        q = (q - 1) % 4
        period = {"0": (today.year - 1, 12, 31), "1": (today.year, 3, 31),
                  "2": (today.year, 6, 30), "3": (today.year, 9, 30)}[str(q)]
        period = _dt.date(*period)
    df = ak.stock_yjyg_em(date=period.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        ampl = _num(r.get("业绩变动幅度"))
        out.append({
            "code": _code6(r.get("股票代码")),
            "name": str(r.get("股票简称") or ""),
            "kind": str(r.get("预告类型") or ""),
            "ampl": ampl,
            "msg": str(r.get("业绩变动") or ""),
            "date": str(r.get("公告日期") or ""),
            "last": None,
        })
    return out


# ──────────────────────────── 大宗交易 ────────────────────────────

def fetch_dzjy(lookback_days=10):
    """大宗交易明细 (近 N 日)。返回 [{code,name,date,price,close,premium,amount_yi,vol_ratio}, ...]。

    premium 单位: % (akshare 折溢率为小数, 此处 ×100); amount_yi 单位: 亿 (成交总额为万元)。"""
    ak = _ak()
    if ak is None:
        return []
    end = _dt.date.today()
    start = end - _dt.timedelta(days=lookback_days)
    df = ak.stock_dzjy_mrtj(start_date=start.strftime("%Y%m%d"),
                            end_date=end.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        out.append({
            "code": _code6(r.get("证券代码")),
            "name": str(r.get("证券简称") or ""),
            "date": str(r.get("交易日期") or ""),
            "price": _num(r.get("成交价")),
            "close": _num(r.get("收盘价")),
            "premium": (_num(r.get("折溢率")) or 0) * 100,
            "amount_yi": (_num(r.get("成交总额")) or 0) / 1e4,
            "vol_ratio": _num(r.get("成交总额/流通市值")),
        })
    return out


# ──────────────────────────── 机构调研 ────────────────────────────

def fetch_jgdy(lookback_days=7):
    """机构调研 (近 N 日, 取最新一个非空交易日)。返回 [{code,name,last,pct,inst_num,way,date}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    for d in range(lookback_days):
        day = _dt.date.today() - _dt.timedelta(days=d)
        try:
            df = ak.stock_jgdy_tj_em(date=day.strftime("%Y%m%d"))
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        out = []
        for _, r in df.iterrows():
            out.append({
                "code": _code6(r.get("代码")),
                "name": str(r.get("名称") or ""),
                "last": _num(r.get("最新价")),
                "pct": _num(r.get("涨跌幅")),
                "inst_num": int(_num(r.get("接待机构数量")) or 0),
                "way": str(r.get("接待方式") or ""),
                "date": str(r.get("接待日期") or ""),
            })
        return out
    return []


# ──────────────────────────── 涨停池 ────────────────────────────

def fetch_ztpool(lookback_days=5):
    """涨停池 (最近一个交易日)。返回 [{code,name,last,pct,amount_yi,open_cnt,limit_times,sector,first_time,last_time,date}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    for d in range(lookback_days):
        day = _dt.date.today() - _dt.timedelta(days=d)
        try:
            df = ak.stock_zt_pool_em(date=day.strftime("%Y%m%d"))
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        out = []
        for _, r in df.iterrows():
            out.append({
                "code": _code6(r.get("代码")),
                "name": str(r.get("名称") or ""),
                "last": _num(r.get("最新价")),
                "pct": _num(r.get("涨跌幅")),
                "amount_yi": (_num(r.get("成交额")) or 0) / 1e8,
                "open_cnt": int(_num(r.get("炸板次数")) or 0),
                "limit_times": int(_num(r.get("连板数")) or 1),
                "sector": str(r.get("所属行业") or ""),
                "first_time": str(r.get("首次封板时间") or ""),
                "last_time": str(r.get("最后封板时间") or ""),
                "date": day.strftime("%Y-%m-%d"),
            })
        return out
    return []


# ──────────────────────────── 股权质押 ────────────────────────────

def fetch_gpzy(lookback_days=5):
    """股权质押比例 (最近披露, 质押数据约每周五更新)。返回 [{code,name,ratio,market_value,industry,pct_y1,date}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    cands = [_dt.date.today()]
    d = _dt.date.today()
    while len(cands) < lookback_days:
        d -= _dt.timedelta(days=1)
        if d.weekday() == 4:  # 周五: 质押披露日
            cands.append(d)
    for day in cands:
        try:
            df = ak.stock_gpzy_pledge_ratio_em(date=day.strftime("%Y%m%d"))
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        out = []
        for _, r in df.iterrows():
            out.append({
                "code": _code6(r.get("股票代码")),
                "name": str(r.get("股票简称") or ""),
                "ratio": _num(r.get("质押比例")),
                "market_value": (_num(r.get("质押市值")) or 0) / 1e8,
                "industry": str(r.get("所属行业") or ""),
                "last": None,
                "pct_y1": _num(r.get("近一年涨跌幅")),
                "date": str(r.get("交易日期") or ""),
            })
        return out
    return []


# ──────────────────────────── 北向资金 ────────────────────────────

def fetch_north():
    """北向资金数据。优先个股持仓 (受限则空), 落到大盘净流入汇总。

    2024-08 起沪深港通不再逐日披露个股北向持仓明细, 该接口大概率返回空列表;
    兜底返回市场级净流入 (沪股通/深股通), 顶层扫描据实展示。
    返回 [{market,net,msg}, ...] 或个股 [{code,name,hold_chg,msg}, ...]。"""
    ak = _ak()
    if ak is None:
        return []
    # 1) 个股持仓 (优先, 若接口仍可用)
    per = []
    for market, ind in (("沪股通", "当日排行"), ("深股通", "当日排行")):
        try:
            df = ak.stock_hsgt_hold_stock_em(market=market, indicator=ind)
            if df is None or len(df) == 0:
                continue
            for _, r in df.iterrows():
                per.append({
                    "code": _code6(_cell(r, "代码", "股票代码")),
                    "name": str(_cell(r, "名称", "股票简称") or ""),
                    "hold_chg": _num(_cell(r, "较昨日变化", "持股变动", "成交净买额")),
                    "last": _num(_cell(r, "最新价", "收盘价")),
                    "market": market,
                    "msg": "",
                })
        except Exception:
            continue
    if per:
        return per
    # 2) 大盘净流入汇总
    out = []
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        desc = {
            "沪股通": "沪股通", "深股通": "深股通", "港股通(沪)": "港股通(沪)",
            "港股通(深)": "港股通(深)"}
        for _, r in df.iterrows():
            mkt = str(r.get("板块") or "")
            out.append({
                "market": mkt,
                "net": _num(r.get("资金净流入")),
                "code": "", "name": "", "hold_chg": None, "last": None,
                "msg": f"{mkt} 净流入{(_num(r.get('资金净流入')) or 0) / 1e8:.2f}亿",
            })
    except Exception:
        pass
    return out