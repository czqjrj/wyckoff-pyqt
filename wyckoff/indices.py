# -*- coding: utf-8 -*-
"""A股主要指数目录: 提供指数名/代码 → 完整新浪符号 的解析与目录查询。

指数代码 (000xxx / 399xxx) 与深市个股代码区间重叠 (如 000001 既是平安银行
也是上证指数), 因此 6 位裸代码一律按个股处理; 指数必须用完整符号 (sh000001)
或中文名 (上证指数) 访问。本模块负责把完整符号/中文名解析为可分析的符号。
"""
INDEX_CATALOG = [
    # ── 宽基 / 综合 ──
    {"symbol": "sh000001", "name": "上证指数", "category": "宽基综合"},
    {"symbol": "sz399001", "name": "深证成指", "category": "宽基综合"},
    {"symbol": "sh000300", "name": "沪深300", "category": "宽基综合"},
    {"symbol": "sh000016", "name": "上证50", "category": "宽基综合"},
    {"symbol": "sh000010", "name": "上证180", "category": "宽基综合"},
    {"symbol": "sh000905", "name": "中证500", "category": "宽基综合"},
    {"symbol": "sh000852", "name": "中证1000", "category": "宽基综合"},
    {"symbol": "sh000510", "name": "中证A500", "category": "宽基综合"},
    {"symbol": "sh000903", "name": "中证A100", "category": "宽基综合"},
    {"symbol": "sz399330", "name": "深证100", "category": "宽基综合"},
    # ── 板块 / 风格 ──
    {"symbol": "sz399006", "name": "创业板指", "category": "板块风格"},
    {"symbol": "sh000688", "name": "科创50", "category": "板块风格"},
    {"symbol": "sh000698", "name": "科创100", "category": "板块风格"},
    {"symbol": "bj899050", "name": "北证50", "category": "板块风格"},
    {"symbol": "sz399005", "name": "中小100", "category": "板块风格"},
    # ── 策略 / 红利 ──
    {"symbol": "sh000015", "name": "红利指数", "category": "策略红利"},
    {"symbol": "sh000922", "name": "中证红利", "category": "策略红利"},
]

_INDEX_BY_SYMBOL = {i["symbol"]: i for i in INDEX_CATALOG}
_INDEX_BY_NAME = {i["name"]: i for i in INDEX_CATALOG}


def index_symbols():
    """返回全部指数完整符号列表。"""
    return [i["symbol"] for i in INDEX_CATALOG]


def is_index_symbol(symbol: str) -> bool:
    """判断是否为目录内的指数完整符号。"""
    return symbol in _INDEX_BY_SYMBOL


def find_index(query: str):
    """按 完整符号 / 中文名 / 6位代码(仅目录内唯一且不冲突的399开头) 解析指数。

    返回目录条目 dict (含 symbol/name/category) 或 None。
    6 位裸代码一律不在此解析 (可能与深市个股冲突, 走个股路径)。
    """
    if not query:
        return None
    q = (query or "").strip().lower().replace(" ", "")
    if q in _INDEX_BY_SYMBOL:
        return _INDEX_BY_SYMBOL[q]
    if q in _INDEX_BY_NAME:
        return _INDEX_BY_NAME[q]
    for sym, item in _INDEX_BY_SYMBOL.items():
        if sym == q:
            return item
    return None


def search_index(query: str, limit: int = 10) -> list:
    """按 名称/符号/拼音首字母 模糊搜索指数, 返回 [{symbol, name, category, code}, ...]。

    供代码补全与指数菜单过滤使用。
    """
    if not query:
        return []
    q = query.strip().lower().replace(" ", "")
    if not q:
        return []
    hits = []
    for item in INDEX_CATALOG:
        name = item["name"]
        if q in name.lower() or q in item["symbol"] or _initials(name) == q:
            hits.append(item)
    return hits[:limit]


def _initials(name: str) -> str:
    """指数名称拼音首字母 (如 沪深300 -> hs300), 无 pypinyin 时返回空。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return ""
    return "".join(p[0] for p in lazy_pinyin(name)).lower()
