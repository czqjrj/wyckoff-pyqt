# -*- coding: utf-8 -*-
"""键盘精灵全市场搜索测试: 拼音索引必须覆盖全部 A 股, 而非仅自选股。

回归背景: 本地拼音索引只由自选股播种, 纯拼音查询命中自选股就直接返回,
导致键盘精灵搜不到自选股之外的任意 A 股。修复后启动后台构建全市场索引,
拼音查询命中即视为覆盖全部 A 股; 索引未就绪时回退网络搜索补全市场。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import wyckoff.pinyin as pinyin


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """替代 wyckoff._shared.http_session 的假会话。

    resp 为 None 时任何 get 都直接失败 (断言本地命中不应请求网络);
    否则返回预设响应。
    """

    def __init__(self, resp=None):
        self._resp = resp

    def get(self, *a, **k):
        if self._resp is None:
            pytest.fail("本地命中不应请求网络")
        return self._resp


def _patch_net(monkeypatch, resp=None):
    monkeypatch.setattr(pinyin, "http_session", lambda: _FakeSession(resp))


def _make_market(n=3200):
    """生成 n 只假 A 股 (代码+名称), 保证包含一只非自选股的知名股。"""
    stocks = [("601899", "紫金矿业")]
    for i in range(1, n):
        code = "%06d" % i
        if code == "601899":
            continue
        stocks.append((code, "测试股份%d" % i))
    return stocks


@pytest.fixture
def pinyin_env(tmp_path, monkeypatch):
    """把索引文件路径重定向到临时目录, 并复位模块级状态。"""
    seed = tmp_path / "seed.json"
    full = tmp_path / "full.json"
    seed.write_text(json.dumps({
        "600104": {"name": "上汽集团", "full": "shangqijituan", "init": "sqjt"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(pinyin, "STOCK_NAMES_FILE", str(seed))
    monkeypatch.setattr(pinyin, "ALL_STOCKS_FILE", str(full))
    monkeypatch.setattr(pinyin, "_STOCK_PINYIN_CACHE", {})
    monkeypatch.setattr(pinyin, "_FULL_MARKET_CODES", set())
    monkeypatch.setattr(pinyin, "_STOCK_SEARCH_CACHE", {})
    return tmp_path


def test_build_full_market_index_covers_whole_market(pinyin_env, monkeypatch):
    """构建全市场索引后, 非自选股也能按 代码/名称/拼音 被搜索到。"""
    monkeypatch.setattr(pinyin, "fetch_market_stock_list", _make_market)
    assert pinyin.build_full_market_index() is True
    assert "601899" in pinyin._FULL_MARKET_CODES
    # 全市场文件已落盘
    assert os.path.exists(pinyin.ALL_STOCKS_FILE)

    # 代码搜索 (不依赖网络)
    _patch_net(monkeypatch)
    hit = pinyin.search_stock("601899", limit=5)
    assert any(h["code"] == "601899" for h in hit)
    # 名称搜索
    hit = pinyin.search_stock("紫金矿业", limit=5)
    assert any(h["code"] == "601899" for h in hit)
    # 拼音搜索 (全市场索引就绪 → 本地直接命中)
    hit = pinyin.search_stock("zjky", limit=5)
    assert any(h["code"] == "601899" for h in hit)
    hit = pinyin.search_stock("zijinkuangye", limit=5)
    assert any(h["code"] == "601899" for h in hit)


def test_search_pinyin_not_limited_to_watchlist(pinyin_env, monkeypatch):
    """全市场索引就绪时, 拼音查询绝不因自选股命中而锁死在自选股上。"""
    # 只装载全市场索引 (含紫金矿业), 自选股种子仅有上汽集团
    full = {
        "601899": {"name": "紫金矿业", "full": "zijinkuangye", "init": "zjky"},
        "600104": {"name": "上汽集团", "full": "shangqijituan", "init": "sqjt"},
    }
    monkeypatch.setattr(pinyin, "_STOCK_PINYIN_CACHE", dict(full))
    monkeypatch.setattr(pinyin, "_FULL_MARKET_CODES", set(full))
    _patch_net(monkeypatch)
    hit = pinyin.search_stock("zjky", limit=10)
    assert any(h["code"] == "601899" for h in hit)
    hit = pinyin.search_stock("sqjt", limit=10)
    assert any(h["code"] == "600104" for h in hit)


def test_search_pinyin_without_full_market_uses_network(pinyin_env, monkeypatch):
    """全市场索引未就绪时, 拼音查询命中自选股也必须走网络补全市场。"""
    # 仅自选股种子命中 (上汽集团 sqjt), 无全市场索引
    pinyin.load_pinyin_cache()
    assert not pinyin._FULL_MARKET_CODES

    payload = {"QuotationCodeTable": {"Data": [
        {"Code": "601899", "Name": "紫金矿业", "SecurityTypeName": "沪A"},
    ]}}
    _patch_net(monkeypatch, _FakeResp(payload))
    hit = pinyin.search_stock("zjky", limit=10)
    assert any(h["code"] == "601899" for h in hit), "未就绪时也要能搜到自选股之外"
    # 拼音查询的网络结果被校验过, 上汽集团 (sqjt 不匹配 zjky) 不应混入
    assert not any(h["code"] == "600104" for h in hit)


def test_build_rejects_incomplete_market_list(pinyin_env, monkeypatch):
    """接口返回明显不完整的列表时放弃构建, 保留旧索引。"""
    monkeypatch.setattr(pinyin, "fetch_market_stock_list", lambda: [("600104", "上汽集团")] * 2)
    pinyin.load_pinyin_cache()
    assert pinyin.build_full_market_index() is False
    assert not pinyin._FULL_MARKET_CODES
    assert not os.path.exists(pinyin.ALL_STOCKS_FILE)


def test_ensure_reloads_fresh_and_rebuilds_stale(pinyin_env, monkeypatch):
    """新鲜的全市场文件直接加载; 缺失/过期则后台下载重建。"""
    # 新鲜文件 → 直接加载, 不触发网络
    pinyin.ALL_STOCKS_FILE  # noqa
    with open(pinyin.ALL_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"601899": {"name": "紫金矿业", "full": "zijinkuangye",
                              "init": "zjky"}}, f, ensure_ascii=False)
    _patch_net(monkeypatch)
    assert pinyin.ensure_full_market_index() is True
    assert "601899" in pinyin._FULL_MARKET_CODES

    # 过期文件 → 触发重建
    old = time.time() - (pinyin._FULL_MARKET_MAX_AGE_DAYS + 1) * 86400
    os.utime(pinyin.ALL_STOCKS_FILE, (old, old))
    monkeypatch.setattr(pinyin, "fetch_market_stock_list", _make_market)
    assert pinyin.ensure_full_market_index() is True
    assert "601899" in pinyin._FULL_MARKET_CODES


def test_save_pinyin_cache_skips_full_market_entries(pinyin_env, monkeypatch):
    """全市场索引就绪后, 增量保存不得把全市场条目写回 bundle 种子文件。"""
    full = {
        "601899": {"name": "紫金矿业", "full": "zijinkuangye", "init": "zjky"},
        "600104": {"name": "上汽集团", "full": "shangqijituan", "init": "sqjt"},
    }
    monkeypatch.setattr(pinyin, "_STOCK_PINYIN_CACHE", dict(full))
    monkeypatch.setattr(pinyin, "_FULL_MARKET_CODES", set(full))
    pinyin._cache_stock("999999", "测试股")
    pinyin.save_pinyin_cache()
    saved = json.load(open(pinyin.STOCK_NAMES_FILE, encoding="utf-8"))
    assert "999999" in saved
    assert "600104" not in saved and "601899" not in saved


def test_load_merges_seed_and_full_market(pinyin_env):
    """load_pinyin_cache 合并 bundle 种子与全市场索引, 全市场优先。"""
    with open(pinyin.ALL_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"601899": {"name": "紫金矿业", "full": "zijinkuangye",
                              "init": "zjky"}}, f, ensure_ascii=False)
    assert pinyin.load_pinyin_cache() is True
    assert pinyin._STOCK_PINYIN_CACHE["600104"]["name"] == "上汽集团"
    assert pinyin._STOCK_PINYIN_CACHE["601899"]["name"] == "紫金矿业"
