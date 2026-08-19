# -*- coding: utf-8 -*-
"""A股主要指数目录单元测试: 符号/名称解析与个股代码不冲突。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from wyckoff.indices import (
    INDEX_CATALOG, find_index, index_symbols, is_index_symbol, search_index,
)


def test_catalog_has_major_indices():
    """目录必须覆盖核心宽基与板块指数, 且符号/名称唯一。"""
    symbols = [i["symbol"] for i in INDEX_CATALOG]
    names = [i["name"] for i in INDEX_CATALOG]
    assert len(symbols) == len(set(symbols))
    assert len(names) == len(set(names))
    for need in ("sh000001", "sz399001", "sz399006", "sh000300",
                 "sh000905", "sh000852", "sh000016", "sh000688"):
        assert need in symbols


def test_find_index_by_full_symbol():
    assert find_index("sh000001")["name"] == "上证指数"
    assert find_index("sz399006")["name"] == "创业板指"
    assert find_index("SH000300")["name"] == "沪深300"


def test_find_index_by_chinese_name():
    assert find_index("上证指数")["symbol"] == "sh000001"
    assert find_index("沪深300")["symbol"] == "sh000300"
    assert find_index(" 创业板指 ")["symbol"] == "sz399006"


def test_bare_6digit_code_never_resolves_as_index():
    """000001 同时是平安银行 (sz) 与上证指数 (sh) → 裸代码必须走个股路径。"""
    assert find_index("000001") is None
    assert find_index("000300") is None
    assert find_index("000016") is None


def test_is_index_symbol():
    assert is_index_symbol("sh000300")
    assert is_index_symbol("sz399001")
    assert not is_index_symbol("sh600104")
    assert not is_index_symbol("600104")
    assert not is_index_symbol("sz000001")


def test_search_index_by_name_and_symbol():
    assert any(i["symbol"] == "sz399006" for i in search_index("创业板", 5))
    assert any(i["symbol"] == "sh000905" for i in search_index("中证500", 5))
    assert any(i["symbol"] == "sh000300" for i in search_index("sh000300", 5))


def test_index_symbols_unique():
    syms = index_symbols()
    assert len(syms) == len(set(syms))
    assert all(s[:2] in ("sh", "sz", "bj") for s in syms)
