"""wyckoff/utils.py: locate_bar / normalize_symbol 纯逻辑测试。"""

import pandas as pd
import pytest

from wyckoff.utils import locate_bar, normalize_symbol


def _df(days, index=None):
    return pd.DataFrame({"day": pd.to_datetime(days)})


def test_locate_bar_exact_last_match():
    df = _df(["2026-01-01", "2026-01-02", "2026-01-02"])
    assert locate_bar(df, "2026-01-02") == 2          # 重复取最后一个
    assert locate_bar(df, "2026-01-01") == 0


def test_locate_bar_matches_full_datetime_key():
    df = _df(["2026-01-02 10:30:00", "2026-01-02 11:30:00"])
    assert locate_bar(df, "2026-01-02 11:30:00") == 1  # 完整字符串键


def test_locate_bar_prefix_fallback():
    # 传入纯日期键, day 列带时间 → 前 10 位前缀回退
    df = _df(["2026-01-01 09:30:00", "2026-01-02 10:00:00"])
    assert locate_bar(df, "2026-01-02") == 1


def test_locate_bar_minute_key():
    # 分钟级键: day 列带秒, 两侧截到分钟再比
    df = _df(["2026-01-02 10:30:59", "2026-01-02 11:30:04"])
    assert locate_bar(df, "2026-01-02 10:30") == 0
    assert locate_bar(df, "2026-01-02 11:30") == 1


def test_locate_bar_accepts_day_series():
    s = pd.to_datetime(["2026-01-01", "2026-01-05"])
    assert locate_bar(s, "2026-01-05") == 1


def test_locate_bar_nearest_before():
    s = pd.Series(pd.to_datetime([
        "2026-01-02", "2026-01-05", "2026-01-06",  # 中间缺 (周末/停牌)
        "2026-01-12", "2026-01-13",
    ]))
    assert locate_bar(s, "2026-01-07", nearest_before=True) == 2  # 取最近前交易
    assert locate_bar(s, "2026-01-01", nearest_before=True) is None  # 无前驱
    assert locate_bar(s, "2026-02-01", nearest_before=True, max_gap=1) is None  # 超 gap


def test_locate_bar_empty_and_missing():
    assert locate_bar(_df([]), "2026-01-01") is None
    df = _df(["2026-01-01", "2026-01-02"])
    assert locate_bar(df, "2026-03-01") is None
    assert locate_bar(None, "2026-01-01") is None


@pytest.mark.parametrize("inp,out", [
    ("600104", "sh600104"),
    ("000001", "sz000001"),
    ("688001", "sh688001"),
    ("510300", "sh510300"),
    ("159915", "sz159915"),
    ("234596", "sz234596"),
    ("430047", "bj430047"),
    ("830799", "bj830799"),
    ("900001", "bj900001"),
    ("sh600000", "sh600000"),
    ("sz000001", "sz000001"),
    ("bj430047", "bj430047"),
    ("600104.sh", "sh600104"),
    ("000001.SZ", "sz000001"),
    (" 600104 ", "sh600104"),
])
def test_normalize_symbol_valid(inp, out):
    assert normalize_symbol(inp) == out


@pytest.mark.parametrize("inp", ["", "abc", "12345", "1234567", "xx1234"])
def test_normalize_symbol_invalid(inp):
    with pytest.raises(ValueError):
        normalize_symbol(inp)
