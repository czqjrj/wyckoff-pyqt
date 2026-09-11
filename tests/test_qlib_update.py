"""QLib 数据更新脚本 (wyckoff/qlib_update.py) 回归测试。

覆盖纯文件逻辑 (日历读写、bin 格式、全量重写对齐、instruments 同步),
不依赖网络 akshare 与真实行情数据 —— 全部用临时目录构造。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from wyckoff import qlib_update


@pytest.fixture()
def provider(tmp_path):
    """构造最小 qlib 数据目录: 日历 + instruments/all.txt + 一只股票的 bin。"""
    p = str(tmp_path / "cn_data")
    cal = qlib_update.load_calendar  # noqa
    days = pd.date_range("2025-01-02", periods=80, freq="B")[:73]
    os.makedirs(os.path.join(p, "calendars"), exist_ok=True)
    os.makedirs(os.path.join(p, "instruments"), exist_ok=True)
    os.makedirs(os.path.join(p, "features", "sh600015"), exist_ok=True)
    with open(os.path.join(p, "calendars", "day.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(d.strftime("%Y-%m-%d") for d in days) + "\n")
    with open(os.path.join(p, "instruments", "all.txt"), "w", encoding="utf-8") as f:
        f.write("SH600015\t2025-01-02\t2025-03-31\n")
        f.write("SH600036\t2025-01-02\t2025-03-31\n")
    # 预写 sh600015 的 close bin (开头 10 天有值)
    start = 0
    vals = np.arange(1.0, 11.0, dtype="<f")
    np.hstack([np.float32(start), vals]).tofile(os.path.join(p, "features", "sh600015", "close.day.bin"))
    return p


def test_load_and_save_calendar(provider):
    cal = qlib_update.load_calendar(provider)
    assert len(cal) == 73
    assert cal.min().strftime("%Y-%m-%d") == "2025-01-02"
    qlib_update.save_calendar(provider, cal)
    cal2 = qlib_update.load_calendar(provider)
    assert cal2.equals(cal)


def test_extend_calendar_only_appends(provider, monkeypatch):
    """日历扩展只在末尾追加更晚交易日, 不动既有位置。"""
    cal0 = qlib_update.load_calendar(provider)

    fake_df = pd.DataFrame({
        "trade_date": pd.DatetimeIndex(["2025-05-05", "2025-05-06", "2025-05-07"])
    })

    class _FakeAK:
        @staticmethod
        def tool_trade_date_hist_sina():
            return fake_df

    monkeypatch.setitem(sys.modules, "akshare", _FakeAK)
    monkeypatch.setattr(qlib_update, "save_calendar", lambda uri, c: None)

    cal_new = qlib_update.extend_calendar(provider, cal0)
    # 追加了 3 个更晚的交易日, 且 2025-05-05 之后的都保留
    assert len(cal_new) == len(cal0) + 3
    assert set(cal_new).issuperset(set(cal0))
    assert cal_new.max() == pd.Timestamp("2025-05-07")


def test_day_indices_maps_calendar_positions(provider):
    cal = qlib_update.load_calendar(provider)
    days = pd.Series(pd.DatetimeIndex(["2025-01-03", "2025-01-06"]))
    idx, start = qlib_update._day_indices(cal, days)
    assert start == 1
    assert list(idx) == [1, 2]


def test_write_and_read_field_bin(provider):
    f = os.path.join(provider, "features", "sh600015", "tmp.day.bin")
    qlib_update.write_field(f, start=3, values=np.array([10.0, 20.0, np.nan]))
    data = np.fromfile(f, dtype="<f")
    assert int(data[0]) == 3
    assert data[1] == 10.0
    assert data[2] == 20.0
    assert np.isnan(data[3])


def test_rewrite_bins_preserves_start_index(provider):
    """全量重写必须保持头部 start_index 不变 (对齐日历位置)。"""
    cal = qlib_update.load_calendar(provider)
    df = pd.DataFrame({
        "day": pd.DatetimeIndex([cal[i] for i in (10, 11, 12, 15)]),
        "close": [5.0, 6.0, 7.0, 8.0],
        "open": [4.9, 5.9, 6.9, 7.9],
        "high": [5.2, 6.2, 7.2, 8.2],
        "low": [4.8, 5.8, 6.8, 7.8],
        "volume": [100.0, 200.0, 300.0, 400.0],
        "vwap": [5.1, 6.1, 7.1, 8.1],
    })
    n = qlib_update.rewrite_bins(provider, "sh600015", df, cal)

    close = np.fromfile(os.path.join(provider, "features", "sh600015", "close.day.bin"), dtype="<f")
    assert int(close[0]) == 10          # start = 日历位置 10
    assert len(close) == 1 + 6          # 覆盖位置 10..15
    assert close[1] == 5.0              # 位置 10
    assert close[2] == 6.0              # 位置 11
    assert np.isnan(close[4])           # 位置 13 (无数据)
    assert close[6] == 8.0              # 位置 15

    vol = np.fromfile(os.path.join(provider, "features", "sh600015", "volume.day.bin"), dtype="<f")
    assert vol[1] == 100.0
    assert vol[6] == 400.0
    assert n == 4


def test_update_instrument_end_extends():
    from pathlib import Path

    p = Path(str(__import__("tempfile").mkdtemp()))
    os.makedirs(p / "instruments", exist_ok=True)
    inst = p / "instruments" / "all.txt"
    inst.write_text("SH600015\t2025-01-02\t2025-03-31\n", encoding="utf-8")
    qlib_update.update_instrument_end(str(p), "sh600015", "2025-09-30")
    assert "2025-09-30" in inst.read_text(encoding="utf-8")
    # 更早的日期不缩短
    qlib_update.update_instrument_end(str(p), "sh600015", "2025-01-01")
    assert "2025-09-30" in inst.read_text(encoding="utf-8")


def test_fetch_ak_ohlcv_raises_when_no_data(monkeypatch):
    class _FakeAK:
        @staticmethod
        def stock_zh_a_daily(*a, **kw):
            return None

        @staticmethod
        def stock_zh_a_hist(*a, **kw):
            return None

    monkeypatch.setitem(sys.modules, "akshare", _FakeAK)
    with pytest.raises(RuntimeError):
        qlib_update.fetch_ak_ohlcv("sh600015")
