"""本地股票拼音索引 + 股票搜索 (本地优先, 东方财富补全)。

键盘精灵搜索范围必须覆盖全部 A 股: 启动时后台下载全市场股票列表并构建
拼音索引 (用户数据目录 wyckoff_all_stocks.json, 30 天刷新一次), 使拼音/
名称/代码搜索不局限于自选股。
"""
import json
import os
import time
from threading import Lock

from ._log import log_exc
from ._shared import atomic_write_json, http_session
from .datasource import fetch_name
from .paths import ALL_STOCKS_FILE, STOCK_NAMES_FILE
from .storage import load_watchlist
from .utils import normalize_symbol

try:
    from pypinyin import lazy_pinyin
    _HAS_PINYIN = True
except ImportError:
    _HAS_PINYIN = False
    lazy_pinyin = None

# 股票搜索缓存
_STOCK_SEARCH_CACHE = {}
_STOCK_SEARCH_CACHE_TTL = 3600  # 1小时缓存
_SEARCH_CACHE_LOCK = Lock()

# 本地股票拼音索引
# 结构: { "code": {"name": "上港集团", "full": "shanggangjituan", "init": "sgjt"} }
_STOCK_PINYIN_CACHE = {}
_PINYIN_LOCK = Lock()

# 全市场索引中已覆盖的代码集合: 用于判定"拼音搜索命中即全市场命中" (不再
# 需要网络兜底), 也避免 save_pinyin_cache 把全市场条目写回 bundle 资源文件。
_FULL_MARKET_CODES = set()

# 全市场索引刷新周期 (天): 保证新股/更名能被索引到, 又不至于频繁整表下载。
_FULL_MARKET_MAX_AGE_DAYS = 30

# 全市场 A 股列表来源 (东方财富行情列表, pz 上限 100 需分页; fs 覆盖
# 沪主板/深主板/创业板/科创板/北交所的全部 A 股)。push2delay 为备用延迟行情
# 主机, push2 在某些网络/地区不可达时使用。
_EM_CLIST_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
]
_EM_CLIST_PARAMS = {
    "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f12",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
    "fields": "f12,f14",
}
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json",
}

# 全市场列表分页参数: pz 单页上限 100, 全市场约 5900 只 → 最多约 60 页
_EM_PAGE_SIZE = 100
_EM_MAX_PAGES = 100
# 全市场索引构建的完整度门槛: 少于该数量视为接口异常/被限流, 放弃构建
_MIN_MARKET_STOCKS = 3000


def _py_convert(text: str) -> str:
    """把中文转拼音小写, 非中文保留原字符, 返回全拼"""
    if not text:
        return ""
    if not _HAS_PINYIN:
        return ""
    return "".join(lazy_pinyin(text)).lower()


def _py_initials(text: str) -> str:
    """返回拼音首字母, 如 上港集团 -> sgjt"""
    if not text:
        return ""
    if not _HAS_PINYIN:
        return ""
    return "".join(p[0] for p in lazy_pinyin(text)).lower()


def _read_json(path):
    """读取 JSON 文件为 dict, 失败/损坏返回 {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_pinyin_cache():
    """加载本地拼音索引 (全市场文件优先, bundle 种子兜底)。

    返回 True 表示全市场索引已就绪 (拼音搜索命中即可视为覆盖全部 A 股)。
    """
    global _STOCK_PINYIN_CACHE, _FULL_MARKET_CODES
    with _PINYIN_LOCK:
        _STOCK_PINYIN_CACHE = {}
        _FULL_MARKET_CODES = set()
    seed = _read_json(STOCK_NAMES_FILE)
    full = _read_json(ALL_STOCKS_FILE)
    merged = dict(seed)
    if full:
        merged.update(full)
        with _PINYIN_LOCK:
            _FULL_MARKET_CODES = set(full.keys())
    with _PINYIN_LOCK:
        _STOCK_PINYIN_CACHE = merged
    return bool(full)


def save_pinyin_cache():
    """把索引的增量条目 (不在全市场索引内的) 写回 bundle 种子文件。

    全市场索引就绪后, 拼音/名称覆盖已完整, 只需持久化额外的非 A 股增量
    (如历史网络搜索到的自定义条目), 避免把 ~6000 条全市场数据写进
    随包分发的资源文件。
    """
    with _PINYIN_LOCK:
        snapshot = dict(_STOCK_PINYIN_CACHE)
    if _FULL_MARKET_CODES:
        snapshot = {k: v for k, v in snapshot.items() if k not in _FULL_MARKET_CODES}
    if not snapshot:
        return
    try:
        atomic_write_json(STOCK_NAMES_FILE, snapshot)
    except Exception:
        pass


def fetch_market_stock_list() -> list:
    """分页拉取全部 A 股 (代码+名称), 返回 [(code, name), ...]。

    依次尝试多个行情主机, 任一主机成功分页拉全即返回。
    """
    last_err = None
    for url in _EM_CLIST_URLS:
        try:
            return _fetch_market_stock_list_from(url)
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("全市场股票列表获取失败")


def _fetch_market_stock_list_from(url) -> list:
    stocks = []
    seen = set()
    for pn in range(1, _EM_MAX_PAGES + 1):
        params = dict(_EM_CLIST_PARAMS)
        params["pn"] = str(pn)
        params["pz"] = str(_EM_PAGE_SIZE)
        r = http_session().get(url, params=params, headers=_EM_HEADERS, timeout=5)
        diff = (r.json().get("data") or {}).get("diff") or []
        if not diff:
            break
        for it in diff:
            code = str(it.get("f12") or "").strip()
            name = str(it.get("f14") or "").strip()
            if code in seen or not (code.isdigit() and len(code) == 6 and name):
                continue
            seen.add(code)
            stocks.append((code, name))
    return stocks


def build_full_market_index() -> bool:
    """下载全市场股票列表并构建拼音索引, 写入用户数据目录。

    成功后 _STOCK_PINYIN_CACHE 覆盖全部 A 股 (键盘精灵可搜到任意 A 股)。
    失败时回退到已有索引。应在后台线程调用。
    """
    global _STOCK_PINYIN_CACHE, _FULL_MARKET_CODES
    try:
        stocks = fetch_market_stock_list()
        if len(stocks) < _MIN_MARKET_STOCKS:
            # 明显不完整 (接口异常/被限流), 放弃本次构建, 保留旧索引
            log_exc("全市场股票列表不完整 (跳过构建)",
                    RuntimeError(f"仅 {len(stocks)} 只"))
            return load_pinyin_cache()
        full = {}
        for code, name in stocks:
            full[code] = {
                "name": name,
                "full": _py_convert(name),
                "init": _py_initials(name),
            }
        with _PINYIN_LOCK:
            base = dict(_STOCK_PINYIN_CACHE)
        base.update(full)
        with _PINYIN_LOCK:
            _STOCK_PINYIN_CACHE = base
            _FULL_MARKET_CODES = set(full.keys())
        try:
            atomic_write_json(ALL_STOCKS_FILE, full)
        except Exception as e:
            log_exc("保存全市场索引失败", e)
        return True
    except Exception as e:
        log_exc("构建全市场股票索引失败", e)
        return load_pinyin_cache()


def ensure_full_market_index(force=False) -> bool:
    """确保全市场 A 股拼音索引可用: 文件未过期直接加载, 否则下载重建。

    返回 True 表示全市场索引已就绪。首次运行/超期时耗时较长, 应在后台线程调用。
    """
    if os.path.exists(ALL_STOCKS_FILE) and not force:
        try:
            age_days = (time.time() - os.path.getmtime(ALL_STOCKS_FILE)) / 86400.0
        except OSError:
            age_days = _FULL_MARKET_MAX_AGE_DAYS + 1
        if age_days < _FULL_MARKET_MAX_AGE_DAYS:
            return load_pinyin_cache()
    return build_full_market_index()


def _cache_stock(code: str, name: str):
    """把一只股票加入本地拼音索引"""
    if not code or not name:
        return
    with _PINYIN_LOCK:
        if code in _STOCK_PINYIN_CACHE:
            return
        _STOCK_PINYIN_CACHE[code] = {
            "name": name,
            "full": _py_convert(name),
            "init": _py_initials(name),
        }


def load_watchlist_stocks():
    """把自选股列表中的股票加入本地拼音索引 (作为启动时的种子数据)"""
    try:
        for c in load_watchlist():
            with _PINYIN_LOCK:
                if c in _STOCK_PINYIN_CACHE:
                    continue
            try:
                name = fetch_name(normalize_symbol(c))
            except Exception:
                name = ""
            if name and name != c:
                _cache_stock(c, name)
    except Exception:
        pass


def _local_search(query: str, limit: int = 10) -> list:
    """在本地拼音索引中搜索, 支持 代码/名称/拼音全拼/拼音首字母
    返回与 search_stock 相同的结构

    直接在锁内扫描索引而非先整表拷贝 (约 6000 条, 每次查询都拷贝代价高);
    扫描约 1ms, 持锁期间仅阻塞稀疏的索引写入, 可接受。
    """
    q = query.strip().lower()
    if not q:
        return []
    results = []
    with _PINYIN_LOCK:
        cache = _STOCK_PINYIN_CACHE
        if not cache:
            return []
        # 优先代码精确匹配
        if q in cache:
            info = cache[q]
            results.append({
                "code": q, "name": info["name"], "symbol": normalize_symbol(q),
                "score": 0,
            })
        for code, info in cache.items():
            if len(results) >= limit:
                break
            if code == q:
                continue
            name = info.get("name", "")
            full = info.get("full", "")
            init = info.get("init", "")
            if q in name or q in full or q in init:
                score = 0
                if q == full:
                    score = 1
                elif q == init:
                    score = 2
                elif q in full:
                    score = 3
                elif q in name:
                    score = 4
                elif q in init:
                    score = 5
                try:
                    symbol = normalize_symbol(code)
                except ValueError:
                    symbol = ""
                results.append({
                    "code": code, "name": name, "symbol": symbol, "score": score,
                })
    results.sort(key=lambda x: (x.get("score", 9), x["code"]))
    return results[:limit]


def local_search_stock(query: str, limit: int = 10) -> list:
    """仅搜索本地拼音索引 (代码/名称/拼音), 绝不触发网络。

    供 GUI 线程安全地取股票名 (启动加载自选股等), 名称权威值由实时行情补齐。
    全市场索引未就绪时可能返回空, 属正常降级。
    """
    return _local_search(query, limit=limit)


def search_stock(query: str, limit: int = 10) -> list:
    """根据股票代码、名称、拼音搜索股票
    优先本地拼音索引, 再从东方财富接口补充
    返回格式: [{"code": "600104", "name": "上港集团", "symbol": "sh600104"}, ...]
    """
    if not query or len(query.strip()) < 1:
        return []

    query = query.strip()

    # 指数优先: 名称/完整符号/拼音可搜索到指数时置顶 (指数代码与个股重叠, 需完整符号)
    try:
        from .indices import search_index
        idx_hits = [{"code": i["symbol"], "name": i["name"],
                     "symbol": i["symbol"], "score": 0}
                    for i in search_index(query, limit=limit)]
    except Exception:
        idx_hits = []

    # 检查缓存
    cache_key = query.lower()
    with _SEARCH_CACHE_LOCK:
        if cache_key in _STOCK_SEARCH_CACHE:
            cached_time, cached_result = _STOCK_SEARCH_CACHE[cache_key]
            if time.time() - cached_time < _STOCK_SEARCH_CACHE_TTL:
                return cached_result[:limit]

    # 本地拼音索引搜索
    local = _local_search(query, limit=limit)
    # 拼音查询仅限纯 ASCII 字母 (中文名称的 isalpha() 也为 True, 不能当拼音处理)
    is_pinyin = query.isascii() and query.isalpha()
    # 全市场索引就绪 → 本地命中即覆盖全部 A 股, 无需网络补全 (W2 键盘精灵范围)。
    if local and _FULL_MARKET_CODES:
        with _SEARCH_CACHE_LOCK:
            _STOCK_SEARCH_CACHE[cache_key] = (time.time(), local)
        return local[:limit]
    # 全市场索引未就绪 (可能只有自选股种子): 结果已满才直接返回;
    # 拼音查询即使命中自选股也必须走网络兜底, 避免搜索范围锁死在自选股上。
    if is_pinyin:
        if local and len(local) >= limit:
            with _SEARCH_CACHE_LOCK:
                _STOCK_SEARCH_CACHE[cache_key] = (time.time(), local)
            return local[:limit]
    elif local and len(local) >= limit:
        with _SEARCH_CACHE_LOCK:
            _STOCK_SEARCH_CACHE[cache_key] = (time.time(), local)
        return local

    try:
        # 使用东方财富搜索接口
        url = "https://searchapi.eastmoney.com/api/suggest/get"
        params = {
            "input": query,
            "type": "14",  # 14表示A股
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": str(limit)
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://www.eastmoney.com/"
        }
        r = http_session().get(url, params=params, headers=headers, timeout=5)
        data = r.json()

        results = []
        # 兼容不同响应结构: QuotationCodeTable.Data 可能是 list 或 None
        table = data.get("QuotationCodeTable") or {}
        items = table.get("Data") or []
        for item in items:
            code = item.get("Code", "")
            name = item.get("Name", "")
            sec_type = item.get("SecurityTypeName", "") or ""
            # 只保留A股 (沪A/深A/京A/北A等), 排除债券/基金/指数
            if "债券" in sec_type or "基金" in sec_type or "指数" in sec_type:
                continue
            if code and name and len(code) == 6 and code.isdigit():
                # 根据代码前缀判断市场
                try:
                    symbol = normalize_symbol(code)
                except ValueError:
                    continue
                results.append({
                    "code": code,
                    "name": name,
                    "symbol": symbol
                })

        # 把搜索结果加入本地拼音索引 (仅中文/代码查询缓存, 拼音查询的网络结果可能误匹配)
        if not is_pinyin:
            for item in results:
                _cache_stock(item["code"], item["name"])
            save_pinyin_cache()

        # 拼音查询: 校验网络结果的名称拼音是否真正匹配, 过滤误匹配
        if is_pinyin and results and _HAS_PINYIN:
            q = query.lower()
            filtered = []
            for item in results:
                full = _py_convert(item["name"])
                init = _py_initials(item["name"])
                if q in full or q in init or full.startswith(q) or init.startswith(q):
                    filtered.append(item)
            results = filtered

        # 合并指数 + 本地 + 网络结果, 指数/本地优先
        merged = []
        seen = set()
        for item in idx_hits + local + results:
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            merged.append(item)
        merged = merged[:limit]

        # 更新缓存
        with _SEARCH_CACHE_LOCK:
            _STOCK_SEARCH_CACHE[cache_key] = (time.time(), merged)
        return merged
    except Exception as e:
        log_exc("搜索股票失败", e)
        if idx_hits:
            return idx_hits[:limit]
        if local:
            return local[:limit]
        return []
