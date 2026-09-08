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


# ──────────────────────────── 市场情绪 ────────────────────────────

def fetch_emotion_data():
    """市场情绪快照 (最近交易日): 涨停/跌停/炸板池 + 连板梯队 + 涨停行业分布。

    返回 dict or None:
        date:       交易日 YYYY-MM-DD
        zt:         涨停明细 [{code,name,pct,limit_times,open_cnt,sector,
                             first_time,last_time,amount_yi,seal_yi}]
        dt_cnt:     跌停家数
        zb_cnt:     炸板家数 (今日曾涨停未封住)
        ladder:     {连板数: 家数} 1..max
        max_board:  最高连板
        zt_by_sector: [(行业, 家数), ...] 涨停家数 Top8
        prev_zt:    上一个交易日涨停家数
        premium:    昨日涨停今日平均涨跌幅 % (赚钱效应, None=不可算)
    """
    ak = _ak()
    if ak is None:
        return None
    today = _dt.date.today()
    # ── 今日涨停池 (往前容错最多 4 个自然日) ──
    zt_map = None
    for d in range(4):
        ds = (today - _dt.timedelta(days=d)).strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=ds)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "code": _code6(r.get("代码")),
                "name": str(r.get("名称") or ""),
                "pct": _num(r.get("涨跌幅")),
                "limit_times": int(_num(r.get("连板数")) or 1),
                "open_cnt": int(_num(r.get("炸板次数")) or 0),
                "sector": str(r.get("所属行业") or ""),
                "first_time": str(r.get("首次封板时间") or ""),
                "last_time": str(r.get("最后封板时间") or ""),
                "amount_yi": (_num(r.get("成交额")) or 0) / 1e8,
                "seal_yi": (_num(r.get("封板资金")) or 0) / 1e8,
            })
        zt_map = {"date": (today - _dt.timedelta(days=d)).strftime("%Y-%m-%d"),
                  "rows": rows}
        break
    if not zt_map:
        return None
    date = zt_map["date"]
    zt_rows = zt_map["rows"]
    # ── 跌停池 / 炸板池 (同日期, fail-soft) ──
    dt_cnt = zb_cnt = 0
    ds = date.replace("-", "")
    for name, fn in (("dt", "stock_zt_pool_dtgc_em"),
                     ("zb", "stock_zt_pool_zbgc_em")):
        try:
            df = getattr(ak, fn)(date=ds)
            if df is not None:
                if name == "dt":
                    dt_cnt = int(len(df))
                else:
                    zb_cnt = int(len(df))
        except Exception:
            pass
    # ── 连板梯队 / 最高板 / 行业分布 ──
    ladder = {}
    for z in zt_rows:
        n = z["limit_times"] or 1
        ladder[n] = ladder.get(n, 0) + 1
    max_board = max(ladder) if ladder else 0
    by_sector = {}
    for z in zt_rows:
        s = z.get("sector") or "其他"
        by_sector[s] = by_sector.get(s, 0) + 1
    zt_by_sector = sorted(by_sector.items(), key=lambda kv: -kv[1])[:8]
    # ── 昨日涨停家数 + 今日溢价 (赚钱效应) ──
    prev_zt = None
    premium = None
    ddate = _dt.date.fromisoformat(date)
    for d in range(1, 8):
        pday = ddate - _dt.timedelta(days=d)
        try:
            pdf = ak.stock_zt_pool_em(date=pday.strftime("%Y%m%d"))
        except Exception:
            continue
        if pdf is None or len(pdf) == 0:
            continue
        prev_zt = int(len(pdf))
        codes = [_code6(r.get("代码")) for _, r in pdf.iterrows()]
        try:
            from .datasource import fetch_realtime
            rt = fetch_realtime(codes) or {}
            pcts = [e["pct"] for c in codes
                    for e in [rt.get(c) or {}] if e.get("pct") is not None]
            if pcts:
                premium = round(sum(pcts) / len(pcts), 2)
        except Exception:
            premium = None
        break
    return {
        "date": date,
        "zt": zt_rows,
        "dt_cnt": dt_cnt, "zb_cnt": zb_cnt,
        "ladder": ladder, "max_board": max_board,
        "zt_by_sector": zt_by_sector,
        "prev_zt": prev_zt, "premium": premium,
    }
