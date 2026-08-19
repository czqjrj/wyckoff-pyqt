# -*- coding: utf-8 -*-
"""SQLite 持久化缓存测试: 读写往返 / TTL 过期 / 清除 / 统计 / 顺序还原。"""
import pandas as pd

from wyckoff import sqldb


def _kline(n=30):
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.0] * n, "volume": [100.0] * n,
    })


def test_kline_roundtrip():
    df = _kline()
    sqldb.kline_save("sh600104", 240, df, "新浪")
    got, source = sqldb.kline_load("sh600104", 240, max_age=3600)
    assert source == "新浪"
    assert len(got) == len(df)
    assert got["day"].iloc[0] == df["day"].iloc[0]
    assert got["close"].iloc[-1] == 10.0
    assert str(got["close"].dtype).startswith("float")
    assert str(got["day"].dtype).startswith("datetime64")


def test_kline_miss_and_expired():
    assert sqldb.kline_load("sh999999", 240, 3600) is None
    sqldb.kline_save("sh600104", 240, _kline(), "腾讯")
    assert sqldb.kline_load("sh600104", 240, max_age=0) is None


def test_kline_scale_is_distinct():
    sqldb.kline_save("sh600104", 240, _kline(30), "新浪")
    sqldb.kline_save("sh600104", 60, _kline(50), "东方财富")
    got60, _ = sqldb.kline_load("sh600104", 60, 3600)
    assert len(got60) == 50
    got240, _ = sqldb.kline_load("sh600104", 240, 3600)
    assert len(got240) == 30


def test_qfq_roundtrip_and_miss():
    fac = pd.DataFrame({"d": pd.date_range("2024-01-01", periods=3),
                        "f": [1.2, 1.1, 1.0]})
    sqldb.qfq_save("sh600104", fac)
    got = sqldb.qfq_load("sh600104", max_age=3600)
    assert got is not None and len(got) == 3
    assert got["f"].tolist() == [1.2, 1.1, 1.0]
    assert sqldb.qfq_load("sh600104", max_age=0) is None
    assert sqldb.qfq_load("sh999999", 3600) is None


def test_clear_and_stats():
    sqldb.kline_save("sh600104", 240, _kline(), "新浪")
    sqldb.kline_save("sh000001", 240, _kline(50), "东方财富")
    sqldb.qfq_save("sh600104", pd.DataFrame({"d": ["2024-01-01"], "f": [1.0]}))
    stats = sqldb.cache_stats()
    assert stats["kline_rows"] == 2
    assert stats["qfq_rows"] == 1
    assert stats["db_bytes"] > 0
    sqldb.clear_cache()
    stats = sqldb.cache_stats()
    assert stats["kline_rows"] == 0
    assert stats["qfq_rows"] == 0
    assert sqldb.kline_load("sh600104", 240, 3600) is None


def test_rows_roundtrip_preserves_order():
    df = pd.DataFrame({
        "day": pd.to_datetime(["2024-01-03", "2024-01-01", "2024-01-02"]),
        "open": [1.0, 2.0, 3.0], "high": [1.5, 2.5, 3.5],
        "low": [0.5, 1.5, 2.5], "close": [1.1, 2.1, 3.1], "volume": [1.0, 2.0, 3.0],
    })
    sqldb.kline_save("sh600104", 240, df, "新浪")
    got, _ = sqldb.kline_load("sh600104", 240, 3600)
    assert got["day"].tolist() == sorted(df["day"].tolist())


def test_save_none_is_noop():
    sqldb.kline_save("sh600104", 240, None, "新浪")
    sqldb.qfq_save("sh600104", None)
    assert sqldb.cache_stats()["kline_rows"] == 0
