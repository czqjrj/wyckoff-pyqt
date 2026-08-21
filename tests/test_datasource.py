# -*- coding: utf-8 -*-
"""数据源 K 线时间段回归测试: 无论哪个源返回多少根, fetch_kline 都必须
只保留最近 datalen 根 (东财 beg=0 时忽略 lmt 返回全历史, 曾在近1月时把
整个1997年以来的K线灌进来, 导致"时间段没起作用")。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from wyckoff import datasource
from wyckoff.analysis import clamp_window


def _dfn(n, start="2024-01-01"):
    return pd.DataFrame({
        "day": pd.date_range(start, periods=n),
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 100.0,
    })


def _fake_full(symbol, datalen, scale):
    return _dfn(7000, start="1990-01-01")


def test_fetch_kline_truncates_to_datalen(monkeypatch):
    """全历史返回也必须被截断到所选时间段。"""
    for fn in ("_fetch_kline_sina", "_fetch_kline_eastmoney", "_fetch_kline_tencent"):
        monkeypatch.setattr(datasource, fn, _fake_full)
    df = datasource.fetch_kline("sh600104", datalen=30, scale=240, use_cache=False)
    assert len(df) == 30
    df = datasource.fetch_kline("sh600104", datalen=120, scale=240, use_cache=False)
    assert len(df) == 120
    df = datasource.fetch_kline("sh600104", datalen=700, scale=240, use_cache=False)
    assert len(df) == 700


def test_clamp_window_bounds_to_period():
    """分析边界兜底: 即便上游漏出全历史, 分析窗口也不超过 所选时间段+1实时bar。"""
    df = _dfn(6846)
    out = clamp_window(df, 250)
    assert len(out) == 251
    # 保留最新 (末尾) 的根, 实时bar在末尾不被丢弃
    assert out["day"].iloc[-1] == df["day"].iloc[-1]
    out30 = clamp_window(_dfn(6846), 30)
    assert len(out30) == 31
    out700 = clamp_window(_dfn(6846), 700)
    assert len(out700) == 701


def test_clamp_window_noop_when_within_period():
    """正常窗口 (datalen+1 含实时bar) 不被误裁。"""
    df = _dfn(251)
    out = clamp_window(df, 250)
    assert out is df
    df2 = _dfn(250)
    out2 = clamp_window(df2, 250)
    assert out2 is df2
    assert clamp_window(None, 250) is None


def _sina_line(sym, name, price):
    """构造一条新浪实时行情 (>=32 字段)。"""
    f = [name, price, price, price, price, price, "0", "0", "1000"]
    f += [str(i) for i in range(1, 23)]   # f[9..30]
    f.append("0")                         # f[31]
    return f'var hq_str_{sym}="{",".join(f)}";'


def test_fetch_realtime_index_and_stock_same_code_coexist(monkeypatch):
    """指数 sh000001 与个股 sz000001 同码: 必须同时保留两者的完整符号键,
    避免自选股栏把同码指数行情顶到个股卡片上 (出现两只同名卡片)。"""
    text = (_sina_line("sz000001", "平安银行", "10.10")
            + "\n" + _sina_line("sh000001", "上证指数", "3210.00"))

    def fake_get(*a, **k):
        r = type("Resp", (), {})()
        r.encoding = "gbk"
        r.text = text
        return r

    monkeypatch.setattr(datasource.http_session(), "get", fake_get)
    out = datasource.fetch_realtime(["000001", "sh000001"])
    assert out["sz000001"]["name"] == "平安银行"
    assert out["sh000001"]["name"] == "上证指数"
    assert out["sz000001"] is not out["sh000001"]
    # 6 位裸代码别名仍可用 (兼容旧调用方)
    assert "000001" in out


def test_fetch_kline_uses_cache(monkeypatch):
    """缓存命中时不再调用源, 且返回长度一致。"""
    calls = {"n": 0}

    def fake(symbol, datalen, scale):
        calls["n"] += 1
        return _fake_full(symbol, datalen, scale)

    monkeypatch.setattr(datasource, "_fetch_kline_sina", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_eastmoney", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_tencent", fake)
    datasource._KLINE_CACHE.clear()
    a = datasource.fetch_kline("sh600104", datalen=90, scale=240, use_cache=True)
    b = datasource.fetch_kline("sh600104", datalen=90, scale=240, use_cache=True)
    assert len(a) == len(b) == 90
    assert calls["n"] == 1
    datasource._KLINE_CACHE.clear()


def test_fetch_kline_persists_to_sqlite(monkeypatch):
    """SQLite 持久缓存: 首次网络拉取落盘, 模拟重启 (清空内存) 后直接命中,
    不再请求网络, 且长度/内容一致。"""
    calls = {"n": 0}

    def fake(symbol, datalen, scale):
        calls["n"] += 1
        return _fake_full(symbol, datalen, scale)

    monkeypatch.setattr(datasource, "_fetch_kline_sina", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_eastmoney", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_tencent", fake)
    a = datasource.fetch_kline("sh600104", datalen=700, scale=240, use_cache=True)
    assert calls["n"] == 1
    assert datasource.data_source_of("sh600104", 700, 240) == "新浪"
    datasource._KLINE_CACHE.clear()   # 模拟进程重启: 只剩 SQLite
    b = datasource.fetch_kline("sh600104", datalen=700, scale=240, use_cache=True)
    assert calls["n"] == 1
    assert len(b) == len(a) == 700
    assert b["close"].tolist() == a["close"].tolist()
    assert datasource.data_source_of("sh600104", 700, 240) == "新浪"
    datasource._KLINE_CACHE.clear()


def test_fetch_kline_sqlite_insufficient_refetches(monkeypatch):
    """SQLite 缓存根数不足所选时间段 (跨 datalen 会话) 时视为失效重拉, 防"时间段没起作用"。"""
    calls = {"n": 0}

    def fake(symbol, datalen, scale):
        calls["n"] += 1
        return _fake_full(symbol, datalen, scale)

    monkeypatch.setattr(datasource, "_fetch_kline_sina", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_eastmoney", fake)
    monkeypatch.setattr(datasource, "_fetch_kline_tencent", fake)
    datasource.fetch_kline("sh600104", datalen=30, scale=240, use_cache=True)
    assert calls["n"] == 1
    datasource._KLINE_CACHE.clear()
    # 上次只缓存了 30 根, 现在要 700 根 → 必须重新请求网络
    df = datasource.fetch_kline("sh600104", datalen=700, scale=240, use_cache=True)
    assert calls["n"] == 2
    assert len(df) == 700
    datasource._KLINE_CACHE.clear()


def test_normalize_kline_df_guard():
    """归一化守卫: 30根可接受 (近1月), <20根拒绝。"""
    rows = [["2024-01-%02d" % (i + 1), 10, 10.2, 9.8, 10.1, 1000] for i in range(30)]
    df = datasource._normalize_kline_df(rows)
    assert len(df) == 30
    with pytest.raises(RuntimeError):
        datasource._normalize_kline_df(rows[:10])


def test_source_health_tracking():
    """源健康度记录: 成功/失败计数与比率, 可重置。"""
    datasource.reset_source_health()
    assert datasource.source_health() == {}
    datasource._health_hit("新浪", True)
    datasource._health_hit("新浪", True)
    datasource._health_hit("新浪", False, "网络错误")
    datasource._health_hit("腾讯", True)
    h = datasource.source_health()
    assert h["新浪"]["ok"] == 2 and h["新浪"]["fail"] == 1
    assert abs(h["新浪"]["ok_ratio"] - 2 / 3) < 0.001
    assert h["腾讯"]["ok"] == 1 and h["腾讯"]["fail"] == 0
    assert "网络错误" in h["新浪"]["last_err"]
    datasource.reset_source_health()
    assert datasource.source_health() == {}


def test_fetch_kline_records_source_health(monkeypatch):
    """fetch_kline 成功/失败都要记录源健康度。"""
    datasource.reset_source_health()
    datasource._SOURCE_LOG.clear()

    def fake_ok(symbol, datalen, scale):
        return _dfn(50)

    def fake_bad(symbol, datalen, scale):
        raise RuntimeError("源不可用")

    monkeypatch.setattr(datasource, "_fetch_kline_sina", fake_bad)
    monkeypatch.setattr(datasource, "_fetch_kline_eastmoney", fake_bad)
    monkeypatch.setattr(datasource, "_fetch_kline_tencent", fake_ok)
    df = datasource.fetch_kline("sh600104", datalen=50, scale=240, use_cache=False)
    assert len(df) == 50
    h = datasource.source_health()
    assert h["新浪"]["fail"] == 1
    assert h["东方财富"]["fail"] == 1
    assert h["腾讯"]["ok"] == 1
    datasource.reset_source_health()
    datasource._SOURCE_LOG.clear()
