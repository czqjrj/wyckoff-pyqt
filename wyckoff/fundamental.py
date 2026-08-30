"""基本面 / 主力资金流抓取与威科夫阶段确认规则。

- 基本面: 腾讯 qt.gtimg.cn 为主源 (PE/PB/市值/换手/外盘内盘, 稳定), 东方财富
  push2 补充 EPS/营收/净利同比 (东财偶发断连, 失败即跳过, 不阻塞分析)。
- 资金流(订单流代理): 东方财富 fflow 主力/超大单/大单/中单/小单 逐日净流入。
- 板块扫描: 东财 push2 批量 API → 同花顺 akshare 后备 (THS 透传)。
- build_confirm_section: 把基本面+资金流证据对齐到当前威科夫阶段, 输出
  确认/背离条目与置信度修饰, 供结论区展示。全部 fail-soft。
"""
import json
import os
import time
from threading import Lock, Semaphore

import pandas as pd

from ._shared import atomic_write_json, http_session
from .paths import ALL_STOCKS_FILE, BOARD_MAP_FILE

# 东财 push2 clist 必需参数/头: 缺少 ut 或 Accept: application/json 时, 东财会
# 拒绝请求并返回 rc:102, data:null (次生: 板块码映射表被写成空 {} → 产业链成分股
# 拿不到 BK 码)。ut 为东财公开行情列表校验令牌, 与 pinyin 全市场索引同源。
_EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_EM_CLIST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json",
}
# 东财行情列表主机 (按顺序回退): 某些网络会屏蔽 push2 而放行 push2delay,
# 与 pinyin 全市场索引同源策略。任一主机拿不到即试下一个。
_EM_CLIST_URLS = (
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
)


def _clist_get(params, timeout=4, retries=1):
    """东财行情列表请求: 依次尝试多个主机, 任一成功 (HTTP 200 且有 data) 即返回。
    全部失败返回 None。"""
    for url in _EM_CLIST_URLS:
        r = _get(url, params, _EM_CLIST_HEADERS, timeout=timeout, retries=retries)
        if r is None:
            continue
        try:
            data = r.json().get("data") or {}
            diff = data.get("diff") or []
        except (ValueError, KeyError):
            continue
        if not diff:
            continue
        return r
    return None

_FUND_CACHE = {}
_FUND_TTL = 900  # 基本面 15分钟
_FLOW_CACHE = {}
_FLOW_TTL = 600  # 资金流 10分钟 (盘中更新)
_SECTOR_NAME_CACHE = {}
_SECTOR_NAME_TTL = 900
_BOARD_CACHE = {}
_BOARD_TTL = 3600  # 板块码映射 1小时
_SECTOR_FLOW_CACHE = {}
_MARKET_CACHE = {}
_MARKET_TTL = 1800  # 全市场宇宙缓存 30 分钟 (盘内成交额排名会变)
_LOCK = Lock()
_FAIL_CACHE = {}
_FAIL_TTL = 30  # 抓取失败负缓存: 30 秒内不重复打同一失败请求 (网络不可达/接口宕机)
# EM push2 个股接口并发信号量: 选股并行评分时该接口极易限流 (实测10并发下
# 85% 失败), 限制为少量并发换取板块名/净利增速等字段的完整度。
_EM_STOCK_SEM = Semaphore(3)
# EM push2his 资金流接口并发信号量 (独立主机, 限制放宽到 6)。
_EM_FLOW_SEM = Semaphore(6)


def fetch_market_universe(n: int = 100):
    """动态全市场宇宙: 东财按成交额排名取 Top-N 活跃A股 (沪深京), 失败返回 []。
    返回带 sh/sz/bj 前缀的代码列表; 成功结果缓存 30 分钟, 失败不缓存
    (接口间歇性失败时, 下次点击可立即重试, 而非长时间拿到空宇宙)。"""
    now = time.time()
    with _LOCK:
        c = _MARKET_CACHE.get("universe")
        if c and now - c[0] < _MARKET_TTL:
            return c[1]
    codes = []
    r = _get("https://push2.eastmoney.com/api/qt/clist/get",
             {"pn": "1", "pz": str(n), "po": "1", "np": "1",
              "fltt": "2", "invt": "2", "fid": "f6",
              "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
              "fields": "f12,f14,f3,f6"},
             {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
             retries=2, cache_fail=False)
    if r is not None:
        try:
            diff = ((r.json().get("data") or {}).get("diff")) or []
            for b in diff:
                c6 = str(b.get("f12") or "")
                if len(c6) == 6 and c6.isdigit():
                    pref = ("sh" if c6[0] in "65" else "sz" if c6[0] in "023"
                            else "bj" if c6[0] in "489" else "")
                    if pref:
                        codes.append(pref + c6)
        except (ValueError, KeyError):
            codes = []
    if len(codes) < 10:
        codes = []
    if codes:
        with _LOCK:
            _MARKET_CACHE["universe"] = (time.time(), codes)
    return codes


def local_universe(n: int = 300):
    """离线兜底宇宙: 从本地全A名单 (ALL_STOCKS_FILE, ~5900只) 等距抽样 n 只。

    动态宇宙 (东财成交额排名) 不可用时使用: 覆盖沪深京个股, 剔除
    ST/*ST/退市/新股 (无成交额排名, 抽样质量次优但可用)。返回带前缀代码列表。
    """
    try:
        with open(ALL_STOCKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    valid = []
    for code, v in data.items():
        if not (code.isdigit() and len(code) == 6):
            continue
        name = (v.get("name") or "") if isinstance(v, dict) else str(v)
        if any(x in name for x in ("ST", "退", "N ", "C ")):
            continue
        pref = ("sh" if code[0] in "65" else "sz" if code[0] in "023"
                else "bj" if code[0] in "489" else "")
        if pref:
            valid.append(pref + code)
    if not valid:
        return []
    valid.sort()
    if len(valid) <= n:
        return valid
    stride = len(valid) / float(n)
    return [valid[int(i * stride)] for i in range(n)]


def _get(url, params, headers, timeout=4, retries=1, cache_fail=True):
    """GET 请求, 失败重试 retries 次 (东财接口偶发 ConnectionError)。

    失败 (网络异常/非200) 会写入负缓存 _FAIL_CACHE: 30 秒内相同请求直接短路
    返回 None, 避免一次接口宕机被放大成每个接口 ~12s 的串行等待。
    cache_fail=False: 不读也不写负缓存 (用于必须尽快重试的关键请求, 如选股宇宙)。
    """
    if params is None:
        params = {}
    key = (url, tuple(sorted((k, str(v)) for k, v in params.items())))
    now = time.time()
    if cache_fail:
        with _LOCK:
            fc = _FAIL_CACHE.get(key)
            if fc and now - fc[0] < _FAIL_TTL:
                return None
    last = None
    for i in range(retries + 1):
        try:
            r = http_session().get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            last = r.status_code
        except Exception as e:
            last = e
        if i < retries:
            time.sleep(0.3 * (i + 1))
    if cache_fail:
        with _LOCK:
            _FAIL_CACHE[key] = (now, last)
    return None


def fetch_fundamental(symbol: str):
    """返回 {name, price, pe_ttm, pb, mcap_yi, float_mcap_yi, turnover,
    buy_vol, sell_vol, eps, revenue, net_growth, ...} 或 None。
    亏损股 PE 为负 (仍保留 PB/市值/订单流); 指数/ETF/场外基金无估值 → None。"""
    now = time.time()
    with _LOCK:
        cached = _FUND_CACHE.get(symbol)
        if cached and now - cached[0] < _FUND_TTL:
            return cached[1]
    out = None
    # ── 主源: 腾讯 (稳定) ──
    r = _get("https://qt.gtimg.cn/q=" + symbol, {}, {"User-Agent": "Mozilla/5.0"})
    if r is not None:
        try:
            r.encoding = "gbk"
            f = r.text.split("~")
            if len(f) > 48 and f[1]:
                price = float(f[3])
                try:
                    pe_ttm = float(f[39])
                except (ValueError, IndexError):
                    pe_ttm = 0.0  # 亏损股 PE 为负; 缺失置 0 (无效标记)
                pb = float(f[46])
                mcap_yi = float(f[45])
                float_mcap = float(f[44])
                turnover = float(f[38]) if len(f) > 38 else 0.0
                buy_vol = int(f[7]) if len(f) > 7 else 0
                sell_vol = int(f[8]) if len(f) > 8 else 0
                # 门槛: 市值+PB 有效即可 (亏损股 PE 为负, 仍保留 PB/订单流等有效字段)
                if mcap_yi > 0 and pb > 0:
                    out = {
                        "name": f[1], "price": price, "pe_ttm": pe_ttm,
                        "pb": pb, "mcap_yi": mcap_yi, "float_mcap_yi": float_mcap,
                        "turnover": turnover, "buy_vol": buy_vol, "sell_vol": sell_vol,
                    }
        except (ValueError, IndexError):
            out = None
    # ── 补充: 东财 EPS / 营收 / 净利同比 (失败跳过) ──
    if out is not None:
        code6 = symbol[-6:]
        secid = f"1.{code6}" if symbol.startswith("sh") else f"0.{code6}"
        with _EM_STOCK_SEM:
            r = _get("https://push2.eastmoney.com/api/qt/stock/get",
                     {"secid": secid, "fields": "f55,f162,f167,f116,f183,f184"},
                     {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        if r is not None:
            try:
                d = (r.json().get("data") or {})
                if d.get("f55") is not None:
                    out["eps"] = float(d["f55"])
                if d.get("f183") is not None:
                    out["revenue"] = float(d["f183"])
                if d.get("f184") is not None and d["f184"] is not None:
                    out["net_growth"] = float(d["f184"])
            except (ValueError, TypeError):
                pass
    with _LOCK:
        _FUND_CACHE[symbol] = (now, out)
    return out


def fetch_main_flow(symbol: str, n: int = 120):
    """东方财富主力资金流 (日线), 返回 DataFrame(day, main, super, large, mid, small)
    或 None。列单位为元。东财 fflow 接口历史上限约 120 行。"""
    now = time.time()
    with _LOCK:
        cached = _FLOW_CACHE.get((symbol, n))
        if cached and now - cached[0] < _FLOW_TTL:
            return cached[1]
    code6 = symbol[-6:]
    secid = f"1.{code6}" if symbol.startswith("sh") else f"0.{code6}"
    with _EM_FLOW_SEM:
        r = _get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                 {"secid": secid, "fields1": "f1,f2,f3,f7",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                  "klt": "101", "lmt": str(n)},
                 {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    out = None
    if r is not None:
        try:
            kl = ((r.json().get("data") or {}).get("klines")) or []
            rows = []
            for k in kl:
                f = k.split(",")
                if len(f) < 11:
                    continue
                rows.append([f[0], float(f[1]), float(f[5]), float(f[4]),
                             float(f[3]), float(f[2])])
            if rows:
                out = pd.DataFrame(rows,
                                   columns=["day", "main", "super", "large",
                                            "mid", "small"])
                out["day"] = pd.to_datetime(out["day"])
        except (ValueError, KeyError):
            out = None
    with _LOCK:
        _FLOW_CACHE[(symbol, n)] = (now, out)
    return out


def fetch_sector(symbol: str):
    """返回个股所属东财行业板块名 (f127 二级行业优先, f100 兜底) 或 None。"""
    code6 = symbol[-6:]
    secid = f"1.{code6}" if symbol.startswith("sh") else f"0.{code6}"
    now = time.time()
    with _LOCK:
        cached = _SECTOR_NAME_CACHE.get(symbol)
        if cached and now - cached[0] < _SECTOR_NAME_TTL:
            return cached[1]
    out = None
    with _EM_STOCK_SEM:
        r = _get("https://push2.eastmoney.com/api/qt/stock/get",
                 {"secid": secid, "fields": "f57,f100,f127"},
                 {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    if r is not None:
        try:
            d = (r.json().get("data") or {})
            out = str(d.get("f127") or d.get("f100") or "").strip() or None
        except (ValueError, KeyError):
            out = None
    with _LOCK:
        _SECTOR_NAME_CACHE[symbol] = (time.time(), out)
    return out


def _load_board_map():
    """东财行业板块名→码映射 (push2 clist 分页, 496个), 内存缓存1小时 + 磁盘缓存24小时。
    返回 {板块名: BK代码} 或 {}。"""
    now = time.time()
    with _LOCK:
        cached = _BOARD_CACHE.get("__map__")
        if cached and now - cached[0] < _BOARD_TTL:
            return cached[1]
    # 磁盘缓存 (跨会话复用, 板块码稳定)
    try:
        if os.path.exists(BOARD_MAP_FILE) and now - os.path.getmtime(BOARD_MAP_FILE) < 86400:
            with open(BOARD_MAP_FILE, encoding="utf-8") as f:
                bmap = json.load(f)
            if isinstance(bmap, dict) and bmap:
                with _LOCK:
                    _BOARD_CACHE["__map__"] = (time.time(), bmap)
                return bmap
    except Exception:
        pass
    bmap = {}
    for pn in range(1, 7):
        r = _clist_get({"pn": str(pn), "pz": "100", "po": "1", "np": "1",
                        "fltt": "2", "invt": "2", "ut": _EM_UT, "fid": "f62",
                        "fs": "m:90+t:2", "fields": "f12,f14"})
        if r is None:
            break
        try:
            diff = ((r.json().get("data") or {}).get("diff")) or []
        except (ValueError, KeyError):
            break
        if not diff:
            break
        for b in diff:
            if b.get("f14") and b.get("f12"):
                bmap[b["f14"]] = b["f12"]
        if len(diff) < 100:
            break
    # 东财裸 clist 拿不到 (rc:102/data:null) → akshare 东财板块名单兜底构建映射表
    if not bmap:
        bmap = _board_map_via_akshare()
    try:
        atomic_write_json(BOARD_MAP_FILE, bmap)
    except Exception:
        pass
    with _LOCK:
        _BOARD_CACHE["__map__"] = (time.time(), bmap)
    return bmap


def _board_map_via_akshare():
    """用 akshare 东财行业板块名单重建"板块名→BK码"映射 (兜底)。

    东财裸 clist 请求 (push2) 在某些网络/环境会回 rc:102, data:null, 导致
    映射表被写成空 {} → 产业链成分股拿不到 BK 码。akshare 内部请求方式已验证
    可用, 作为映射表兜底。返回 {板块名: BK代码}; akshare 缺失/异常返回 {}。
    """
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
    except Exception:
        return {}
    if df is None or len(df) == 0:
        return {}
    bmap = {}
    for _, row in df.iterrows():
        name = (_str(row.get("板块名称") or row.get("板块") or "")
                or _str(row.get("行业名称") or ""))
        code = (_str(row.get("板块代码") or "")
                or _str(row.get("代码") or ""))
        if name and code.startswith("BK"):
            bmap[name] = code
    return bmap


def _str(v):
    if v is None:
        return ""
    v = str(v).strip()
    return "" if v.lower() == "nan" else v


def _suggest_board(name: str):
    """板块名→QuoteID (如 90.BK1262)。searchapi 不稳, 改用 push2 clist 板块码映射表。"""
    code = _load_board_map().get(name)
    return f"90.{code}" if code else None


def fetch_sector_flow(symbol: str):
    """返回 (板块名, 板块主力资金流DataFrame) 或 (None, None)。
    DataFrame 列同个股: day/main/super/large/mid/small (元)。"""
    name = fetch_sector(symbol)
    if not name:
        return None, None
    now = time.time()
    with _LOCK:
        cached = _SECTOR_FLOW_CACHE.get(name)
        if cached and now - cached[0] < _FLOW_TTL:
            return name, cached[1]
    code = _suggest_board(name)
    out = None
    if code:
        with _EM_FLOW_SEM:
            r = _get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                     {"secid": code, "fields1": "f1,f2,f3,f7",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                      "klt": "101", "lmt": "120"},
                     {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        if r is not None:
            try:
                kl = ((r.json().get("data") or {}).get("klines")) or []
                rows = []
                for k in kl:
                    f = k.split(",")
                    if len(f) < 11:
                        continue
                    rows.append([f[0], float(f[1]), float(f[5]), float(f[4]),
                                 float(f[3]), float(f[2])])
                if rows:
                    out = pd.DataFrame(rows, columns=["day", "main", "super",
                                                      "large", "mid", "small"])
                    out["day"] = pd.to_datetime(out["day"])
            except (ValueError, KeyError):
                out = None
    with _LOCK:
        _SECTOR_FLOW_CACHE[name] = (time.time(), out)
    return name, out


def fetch_board_flow_by_code(bk_code: str):
    """直接按板块BK代码获取主力资金流 DataFrame (列同个股: day/main/super/large/mid/small)。
    失败返回 None。缓存 10 分钟。"""
    if not bk_code:
        return None
    now = time.time()
    with _LOCK:
        cached = _SECTOR_FLOW_CACHE.get(f"__{bk_code}")
        if cached and now - cached[0] < _FLOW_TTL:
            return cached[1]
    secid = f"90.{bk_code}"
    r = _get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
             {"secid": secid, "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
              "klt": "101", "lmt": "120"},
             {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    out = None
    if r is not None:
        try:
            kl = ((r.json().get("data") or {}).get("klines")) or []
            rows = []
            for k in kl:
                f = k.split(",")
                if len(f) < 11:
                    continue
                rows.append([f[0], float(f[1]), float(f[5]), float(f[4]),
                             float(f[3]), float(f[2])])
            if rows:
                out = pd.DataFrame(rows, columns=["day", "main", "super",
                                                   "large", "mid", "small"])
                out["day"] = pd.to_datetime(out["day"])
        except (ValueError, KeyError):
            out = None
    with _LOCK:
        _SECTOR_FLOW_CACHE[f"__{bk_code}"] = (time.time(), out)
    return out


def fetch_board_constituents(bk_code: str, limit: int = 80, name: str = ""):
    """获取板块 BK代码 的成份股列表, 按成交额降序取前 limit 只。
    返回 [(代码, 名称, 最新价), ...] 或 []。

    数据源 (按可用性降序):
    1. 东财 push2 clist, fs=b:BKxxxx (主源, 快速)。
    2. akshare 东财兜底 stock_board_industry_cons_em (需 BK 码; 当东财裸请求
       被拒 rc:102 或用 name 反查映射表补齐 BK 码后使用)。
    缓存 30 分钟 (板块成份股变动极慢)。

    东财 bk_code 兼容两种形态:
    - 裸代码 "BK1262"
    - 东财 QuoteID "90.BK1262" (board_bk_code/_suggest_board 返回的 secid 前缀
      仅用于 fflow 的 secid 参数, 不适用于 clist 的 fs=b: 过滤, 需剥掉 NN. 前缀)。
    name 可选: 东财失败时用板块名经映射表补 BK 码 (akshare 兜底需要 BK 码)。
    """
    bk_variants = _em_bk_variants(bk_code)
    # 若只有板块名 (bk 缺失/为空), 先尝试用映射表补 BK 码
    if not bk_variants and name:
        resolved = _load_board_map().get(name)
        if resolved:
            bk_variants = [resolved]
    for candidate in bk_variants:
        if not candidate:
            continue
        stocks = _fetch_constituents_em(candidate, limit)
        if stocks:
            return stocks
    # 东财裸请求失败 → akshare 东财成分股接口兜底 (仅当安装 akshare; 需 BK 码)
    for candidate in bk_variants:
        stocks = _fetch_constituents_akshare(candidate, limit)
        if stocks:
            return stocks
    return []


def _em_bk_variants(bk_code):
    """把传入的板块码归一成东财裸 BK 码候选 (剥掉 'NN.' 前缀)。"""
    if not bk_code:
        return []
    raw = str(bk_code).strip()
    if "." in raw:
        raw = raw.split(".", 1)[-1].strip()
    if not raw.startswith("BK"):
        return []
    return [raw]


def _fetch_constituents_em(bk_code: str, limit: int):
    """东财 push2 clist 拉板块成分股 (裸 BK 码)。返回 [(code, name, price)]。"""
    cache_key = f"__const_{bk_code}_{limit}"
    now = time.time()
    with _LOCK:
        c = _BOARD_CACHE.get(cache_key)
        if c and now - c[0] < _MARKET_TTL:
            return c[1]
    stocks = []
    r = _clist_get({"pn": "1", "pz": str(limit), "po": "1", "np": "1",
                    "fltt": "2", "invt": "2", "ut": _EM_UT, "fid": "f6",
                    "fs": f"b:{bk_code}", "fields": "f12,f14,f2,f3"})
    if r is not None:
        try:
            diff = ((r.json().get("data") or {}).get("diff")) or []
            for d in diff:
                code = d.get("f12", "")
                nm = d.get("f14", "")
                price = d.get("f2")
                if code and nm:
                    prefix = "sh" if code.startswith(("6", "9")) else "sz"
                    if code.startswith(("8", "4")):
                        prefix = "bj"
                    stocks.append((f"{prefix}{code}", nm, float(price or 0)))
        except (ValueError, KeyError):
            pass
    if stocks:
        with _LOCK:
            _BOARD_CACHE[cache_key] = (time.time(), stocks)
    return stocks


def _fetch_constituents_akshare(bk_code: str, limit: int):
    """akshare 东财成分股兜底: stock_board_industry_cons_em(symbol=BK码)。
    东财裸 clist 被拒 (rc:102) 时用, 需要东财 BK 码。
    返回 [(code, name, price)] 或 []。akshare 缺失/异常/无数据时返回 []。"""
    cache_key = f"__const_ak_{bk_code}_{limit}"
    now = time.time()
    with _LOCK:
        c = _BOARD_CACHE.get(cache_key)
        if c and now - c[0] < _MARKET_TTL:
            return c[1]
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=bk_code)
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    stocks = []
    for _, row in df.iterrows():
        code = str(row.get("代码") or row.get("code") or "").strip()
        nm = str(row.get("名称") or row.get("name") or "").strip()
        if not (code.isdigit() and len(code) == 6 and nm):
            continue
        try:
            price = float(row.get("最新价") or row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        prefix = "sh" if code.startswith(("6", "9")) else \
            "bj" if code.startswith(("8", "4")) else "sz"
        stocks.append((f"{prefix}{code}", nm, price))
        if len(stocks) >= limit:
            break
    if stocks:
        with _LOCK:
            _BOARD_CACHE[cache_key] = (time.time(), stocks)
    return stocks


def fetch_all_board_stats():
    """批量获取所有行业板块的基础统计: 名称/代码/最新价/涨跌幅/20日主力净流入。
    返回 [{name, bk_code, price, pct, flow20, live}, ...] 按 flow20 降序。
    主源: 同花顺 akshare (透传, 90个行业板块), 后备: 东财 push2 批量 API。
    两者均失败时回退离线列表 (名称+代码, live=False)。"""
    now = time.time()
    with _LOCK:
        c = _BOARD_CACHE.get("__stats__")
        if c and now - c[0] < _BOARD_TTL:
            return c[1]
    bmap = _load_board_map()
    # ── 主源: 同花顺 akshare (透传, 90个行业板块, 带净流入) ──
    stats = _fetch_board_stats_ths()
    if stats:
        # THS 名称 → EM BK 代码映射 (精确匹配 + 模糊匹配)
        em_names = list(bmap.keys())
        import difflib
        for s in stats:
            name = s["name"]
            if name in bmap:
                s["bk_code"] = bmap[name]
            else:
                fuzzy = difflib.get_close_matches(name, em_names, n=1, cutoff=0.7)
                if fuzzy and fuzzy[0] in bmap:
                    s["bk_code"] = bmap[fuzzy[0]]
        with _LOCK:
            _BOARD_CACHE["__stats__"] = (time.time(), stats)
        return stats
    # ── 后备: 东财 push2 批量 API ──
    stats = _fetch_board_stats_em(bmap)
    if stats:
        with _LOCK:
            _BOARD_CACHE["__stats__"] = (time.time(), stats)
        return stats
    # ── 离线回退 ──
    stats = [{"name": name, "bk_code": code, "price": 0, "pct": 0,
              "flow20": 0, "live": False}
             for name, code in sorted(bmap.items())]
    with _LOCK:
        _BOARD_CACHE["__stats__"] = (time.time(), stats)
    return stats


def _fetch_board_stats_ths():
    """同花顺 akshare 行业板块统计 (90个板块, 带涨跌幅+净流入+领涨股)。
    返回 stats 列表 或 []。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_summary_ths()
        if df is None or len(df) == 0:
            return []
        stats = []
        for _, row in df.iterrows():
            name = str(row.get("板块", ""))
            pct_str = str(row.get("涨跌幅", "0"))
            flow_str = str(row.get("净流入", "0"))
            try:
                pct = float(pct_str)
            except (ValueError, TypeError):
                pct = 0.0
            try:
                flow20 = float(flow_str)  # 单位: 万元
            except (ValueError, TypeError):
                flow20 = 0.0
            if not name:
                continue
            stats.append({
                "name": name, "bk_code": "",
                "price": 0, "pct": pct, "flow20": flow20, "live": True,
            })
        stats.sort(key=lambda x: -x["flow20"])
        return stats
    except Exception:
        return []


def _fetch_board_stats_em(bmap):
    """东财 push2 批量行业板块统计 (496个板块, 带指数价+涨跌幅+主力流)。
    返回 stats 列表 或 []。"""
    if not bmap:
        return []
    r = _clist_get({"pn": "1", "pz": "600", "po": "0", "np": "1",
                    "fltt": "2", "invt": "2", "ut": _EM_UT, "fid": "f62",
                    "fs": f"b:{','.join(list(bmap.values())[:300])}",
                    "fields": "f12,f14,f2,f3,f62"})
    if r is None:
        return []
    code_to_name = {v: k for k, v in bmap.items()}
    stats = []
    try:
        diff = ((r.json().get("data") or {}).get("diff")) or []
        for d in diff:
            bk_code = d.get("f12", "")
            name = code_to_name.get(bk_code) or d.get("f14", "")
            price = d.get("f2")
            pct = d.get("f3")
            flow_raw = d.get("f62")
            if name and bk_code and price is not None:
                stats.append({
                    "name": name, "bk_code": bk_code,
                    "price": float(price), "pct": float(pct or 0),
                    "flow20": float(flow_raw or 0), "live": True,
                })
    except (ValueError, KeyError):
        pass
    stats.sort(key=lambda x: -x["flow20"])
    return stats


def holder_ratio_ok(record):
    """股东户数环比记录是否可用于展示 (True=有效, False=失真)。

    过滤失真环比: 首发上市等特殊记录 (PRE_HOLDER_NUM 过小/缺失或 ratio 缺失)
    会造成假性天量增幅 (如福莱特上市前17户→上市后13.9万户, 环比+819570%),
    环比对比无意义。规则: ratio/pre_num/holder_num 齐全且 |环比| ≤ 100%。
    该函数为单一权威判断, market.fetch_holder_history 与 chart 均复用,
    避免多处过滤漂移 (历史事故: fundamental 过滤而 chart 未过滤)。
    """
    if not record:
        return False
    try:
        ratio = record.get("ratio")
        hn = record.get("holder_num")
        pn = record.get("pre_num")
        if ratio is None or not hn or not pn:
            return False
        return abs(float(ratio)) <= 100.0 and float(hn) > 0 and float(pn) > 0
    except (TypeError, ValueError):
        return False


def build_confirm_section(phase, df, fund=None, flow=None, holder=None, sector=None,
                          events=None):
    """把基本面+资金流证据对齐到当前阶段, 返回 (置信修饰, 条目列表)。
    置信修饰: "high" → 阶段前缀"高置信"; "caution" → 阶段后缀"(需谨慎)"。
    条目: [(text, tone), ...], tone: bullish/bearish/neutral。
    纯增强, 不参与阶段判定本身。
    events: 近期威科夫事件列表, 用于实证校准 (backtest_confirm.py):
      Spring 反转式买信号上 flow 确认反向 — 资金流出期的吸筹区恰是 Spring 温床,
      故多头阶段出现 Spring 时, flow 驱动的 "caution" 不再作为减分依据。
    """
    base = phase.split(" ")[0]
    bull = base in ("底部整固", "上升趋势")
    bear = base in ("顶部构筑", "下跌趋势")
    recent_types = [e["type"] for e in (events or []) if e["idx"] >= len(df) - 60]
    spring_present = "Spring" in recent_types
    items = []
    pos = neg = 0

    # ── 估值 ──
    if fund:
        pe = fund.get("pe_ttm")
        pb = fund.get("pb")
        if pe and pe > 0:
            if pe < 25:
                items.append((f"估值: PE(TTM) {pe:.1f} · 相对合理/偏低", "bullish"))
                if bull or base == "区间整理":
                    pos += 1
            elif pe < 45:
                items.append((f"估值: PE(TTM) {pe:.1f} · 中性", "neutral"))
            else:
                items.append((f"估值: PE(TTM) {pe:.1f} · 偏高", "bearish"))
                neg += 1
        if pb and pb > 0:
            if pb < 1.5:
                items.append((f"估值: PB {pb:.2f} · 低市净率/破净", "bullish"))
                if bull or base == "区间整理":
                    pos += 1
            elif pb < 4:
                items.append((f"估值: PB {pb:.2f} · 中性", "neutral"))
            else:
                items.append((f"估值: PB {pb:.2f} · 高市净率", "bearish"))
                neg += 1
        if fund.get("net_growth") is not None:
            g = fund["net_growth"] * 100
            if bull:
                if g >= 0:
                    items.append((f"成长: 净利同比 {g:+.0f}%", "bullish"))
                    pos += 1
                else:
                    items.append((f"成长: 净利同比 {g:+.0f}% · 负增长, 防价值陷阱", "bearish"))
                    neg += 1
            elif bear:
                if g >= 0:
                    items.append((f"成长: 净利同比 {g:+.0f}% · 基本面仍正, 或仅技术派发", "neutral"))
                else:
                    items.append((f"成长: 净利同比 {g:+.0f}% · 盈利下滑, 派发更可信", "bearish"))
                    neg += 1
            else:
                items.append((f"成长: 净利同比 {g:+.0f}%",
                              "bullish" if g >= 0 else "bearish"))

    # ── 历史主力资金流 ──
    if flow is not None and len(flow):
        main20 = float(flow.tail(20)["main"].sum())
        super5 = float(flow.tail(5)["super"].sum())
        ret10 = float(df["close"].iloc[-1] / df["close"].iloc[-11] - 1) \
            if len(df) > 11 else 0.0
        if bull:
            if main20 > 0:
                items.append((f"资金: 近20日主力净流入 {main20 / 1e8:+.2f}亿", "bullish"))
                pos += 1
            else:
                items.append((f"资金: 近20日主力净流出 {main20 / 1e8:+.2f}亿 · 吸筹证据不足", "bearish"))
                neg += 1
        elif bear:
            if main20 < 0:
                items.append((f"资金: 近20日主力净流出 {main20 / 1e8:+.2f}亿", "bearish"))
                neg += 1
            else:
                items.append((f"资金: 近20日主力净流入 {main20 / 1e8:+.2f}亿 · 派发存疑", "neutral"))
        else:
            items.append((f"资金: 近20日主力净流入 {main20 / 1e8:+.2f}亿",
                          "bullish" if main20 > 0 else "bearish"))
        if super5 != 0:
            s_txt = "净流入" if super5 > 0 else "净流出"
            items.append((f"资金: 近5日超大单{s_txt} {abs(super5) / 1e8:.2f}亿",
                          "bullish" if super5 > 0 else "bearish"))
            if bull and super5 > 0:
                pos += 1
            elif bull and super5 < 0:
                neg += 1
        # 量价背离 / 底部承接
        if ret10 > 0.02 and main20 < 0:
            items.append((f"背离: 近10日价涨{ret10 * 100:+.1f}% 但主力净流出 → 警惕接盘", "bearish"))
            neg += 1
        elif ret10 < -0.02 and main20 > 0:
            items.append((f"承接: 近10日价跌{ret10 * 100:+.1f}% 但主力净流入 → 底部承接", "bullish"))
            pos += 1

    # ── 板块资金流 (威科夫三击法: 大盘→板块→个股; 东财行业板块20日主力) ──
    if sector:
        s_name = sector.get("name")
        s20 = sector.get("main20")
        if s_name and s20 is not None:
            s_txt = "净流入" if s20 > 0 else "净流出"
            s_abs = abs(s20) / 1e8
            if bull:
                if s20 > 0:
                    items.append((f"板块: {s_name} · 近20日主力{s_txt} {s_abs:.2f}亿 · 板块强势", "bullish"))
                    pos += 1
                else:
                    items.append((f"板块: {s_name} · 近20日主力{s_txt} {s_abs:.2f}亿 · 板块弱势", "bearish"))
                    neg += 1
            elif bear:
                if s20 < 0:
                    items.append((f"板块: {s_name} · 近20日主力{s_txt} {s_abs:.2f}亿", "bearish"))
                    neg += 1
                else:
                    items.append((f"板块: {s_name} · 近20日主力{s_txt} {s_abs:.2f}亿 · 个股派发或板块背离", "neutral"))
            else:
                items.append((f"板块: {s_name} · 近20日主力{s_txt} {s_abs:.2f}亿",
                              "bullish" if s20 > 0 else "bearish"))

    # ── 产业链: 链条强度 + 传导方向 → 门禁证据 ──
    if sector and sector.get("name"):
        try:
            from .chain import chain_factor_for, chain_home
            _TIER_CN = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}
            cf = chain_factor_for(sector["name"])
            if cf:
                cn, tier = chain_home(sector["name"])
                trans = cf.get("trans") or ""
                tone = cf.get("tone")
                tr_txt = f" · {trans}受益环节" if trans and tone == "bullish" else (
                    f" · 逆{trans}" if trans and tone == "bearish" else (
                        f" · {trans}" if trans else ""))
                tier_txt = _TIER_CN.get(tier, tier or "")
                items.append((f"产业链: [{cn}] 强度{cf['pct']*100:.0f}分位{tr_txt}"
                              + (f" · 所处{tier_txt}" if tier_txt else ""), tone))
                if tone == "bullish" and bull:
                    pos += 1
                elif tone == "bearish" and bull:
                    neg += 1
        except Exception:
            pass

    # ── 当日订单流 (外盘/内盘) ──
    if fund and fund.get("buy_vol") and fund.get("sell_vol"):
        b, s = float(fund["buy_vol"]), float(fund["sell_vol"])
        if b + s > 0:
            ratio = b / (b + s)
            if ratio > 0.52:
                tilt, tone = "主动买盘占优", "bullish"
                if bull:
                    pos += 1
                elif bear:
                    neg += 1
            elif ratio < 0.48:
                tilt, tone = "主动卖盘占优", "bearish"
                if bull:
                    neg += 1
            else:
                tilt, tone = "买卖均衡", "neutral"
            items.append((f"当日订单流: 外盘/内盘 {b / 1e4:.0f}/{s / 1e4:.0f}万手 "
                          f"({ratio * 100:.0f}%) {tilt}", tone))

    # ── 股东户数 (筹码集中度) ──
    if holder:
        # 过滤失真环比: 首发上市等特殊记录 (PRE_HOLDER_NUM 过小) 会造成
        # 假性天量增幅 (如福莱特上市前17户→上市后13.9万户, 环比+819570%),
        # 环比对比无意义, 跳过。判断复用 holder_ratio_ok 单一权威函数。
        last = holder[-1]
        if holder_ratio_ok(last):
            rpct = float(last["ratio"])
            concentrated = rpct < 0
            items.append((f"筹码: 股东户数环比 {rpct:+.1f}% "
                          f"({'筹码集中' if concentrated else '筹码分散'})",
                          "bullish" if concentrated else "bearish"))
            if concentrated and bull:
                pos += 1
            elif (not concentrated) and bull:
                neg += 1

    # ── 置信度修饰: 多头阶段以正向证据为确认, 空头阶段以负向证据为确认 ──
    if bull or bear:
        confirms = pos if bull else neg
        contradicts = neg if bull else pos
        if confirms >= 2 and contradicts == 0:
            qualifier = "high"
        elif contradicts >= 2:
            qualifier = "caution"
        else:
            qualifier = ""
    else:
        qualifier = ""
    # 实证(backtest_confirm.py): 确认机制对确认式信号(ST/UTAD)有效, 但对反转式
    # Spring 反向 — 资金流出期的吸筹区恰是 Spring 温床。多头阶段出现 Spring 时,
    # flow 驱动的 caution 不再作为减分 (流出末端=反转温床), 仅保留提示文本。
    flow_contra = any(("资金" in t or "背离" in t) and tone == "bearish"
                      for t, tone in items)
    if bull and qualifier == "caution":
        if spring_present and flow_contra:
            qualifier = ""
            items.append(("注: 已现Spring且资金流出 — 吸筹反转温床, flow谨慎不视为看空",
                          "neutral"))
        elif flow_contra:
            items.append(("注: 资金流出期的吸筹区常是 Spring 反转温床, 此'谨慎'"
                          "宜待资金回流后再确认, 不宜直接视为看空", "neutral"))
    return qualifier, items
