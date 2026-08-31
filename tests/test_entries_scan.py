"""今日入场点扫描测试 (wyckoff.entries + _shared.analyze_light/parallel_map)。

P0 回归: _shared.analyze_light / parallel_map 此前从未实现, entries 在函数内
from ._shared import ... 抛出 AttributeError 被 except Exception 静默吞掉,
导致「今日入场点」扫描恒返回空 (功能死代码)。本测试验证管线接通且并行框架可用。
全部离线: 打桩 _shared.analyze_light 与 entries.measured_win_rates, 不访问网络。
"""
import numpy as np
import pandas as pd
import pytest


def _mk_df(closes):
    n = len(closes)
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"day": dates, "open": c, "close": c,
                         "high": c + 0.05, "low": c - 0.05,
                         "volume": np.full(n, 8e5)})


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """离线: 打桩分析管线与宏观因子的网络抓取。"""
    monkeypatch.setattr("wyckoff.entries.measured_win_rates",
                        lambda scale=240: {"Spring": {"win": 0.61, "n": 200}})
    monkeypatch.setattr("wyckoff.entries.sector_strength_pct", lambda name: 0.8)
    # find_entry_signals 的三重共振参数直接打桩, 跳过网络宏观抓取
    monkeypatch.setattr("wyckoff.entries.find_entry_signals",
                        lambda df, events=None, **kw: [
                            {"type": "Spring", "confirm_idx": int(len(df) - 1),
                             "fresh_bars": 0, "entry_date": "2024-02-01",
                             "entry_price": 10.0, "last": 10.2, "stop": 9.5,
                             "risk_pct": 5.0, "conf": 85, "model_rel": 0.9,
                             "rel_tier": "high"}])


@pytest.fixture
def light_df():
    return _mk_df(np.r_[np.full(110, 10.0), 10.0, 10.2])


@pytest.fixture
def ent(monkeypatch):
    import wyckoff.entries as e
    monkeypatch.setattr(e, "AUTO_RECORD_ENTRIES", False)
    return e


def test_analyze_light_missing_import_fixed():
    """P0: entries 不再因 _shared 缺 analyze_light/parallel_map 而崩溃。"""
    import wyckoff._shared as s
    assert hasattr(s, "analyze_light")
    assert hasattr(s, "parallel_map")
    # 冷环境 (未打桩) 直接调用应 fail-soft 返回含 df=None 的 dict, 而非抛异常
    r = s.analyze_light("sh600001")
    assert isinstance(r, dict)
    assert "df" in r and "phase" in r and "events" in r


def test_scan_entries_serial_produces_rows(ent, monkeypatch, light_df):
    """串行扫描接通: 命中 Spring → 返回行而非恒空。"""
    from wyckoff import _shared
    monkeypatch.setattr(_shared, "analyze_light",
                        lambda *a, **k: {"df": light_df, "phase": "底部整固",
                                         "events": [], "pivots": [],
                                         "name": "测试股", "sector": {"name": "电池"},
                                         "market_series": None, "flow": None})
    rows = ent.scan_entries(["sh600001", "sh600002"], datalen=120)
    assert len(rows) == 2
    assert rows[0]["code"] == "600001"
    assert rows[0]["type"] == "Spring"
    assert rows[0]["name"] == "测试股"


def test_scan_entries_parallel_produces_rows(ent, monkeypatch, light_df):
    """并行扫描接通 && parallel_map 收集线程安全。"""
    from wyckoff import _shared
    monkeypatch.setattr(_shared, "analyze_light",
                        lambda *a, **k: {"df": light_df, "phase": "底部整固",
                                         "events": [], "pivots": [],
                                         "name": "测试股", "sector": {"name": "电池"},
                                         "market_series": None, "flow": None})
    streamed = []
    rows = ent.scan_entries_parallel(
        ["sh600001", "sh600002", "sh600003"], workers=3,
        on_rows=lambda hit: streamed.extend(hit))
    assert len(rows) == 3
    assert len(streamed) == 3
    assert {r["code"] for r in rows} == {"600001", "600002", "600003"}


def test_parallel_map_threadsafe_collection():
    """parallel_map: 返回值按 input 收集 (线程安全) v.s.串行路径一致。"""
    from wyckoff._shared import parallel_map
    data = list(range(200))
    ser = parallel_map(data, lambda x: x * 2, workers=1)
    par = parallel_map(data, lambda x: x * 2, workers=8)
    assert sorted(ser) == sorted(par) == [x * 2 for x in data]
    # None / 异常被跳过
    assert sorted(parallel_map([1, 2, 3], lambda x: x * 2 if x != 2 else None,
                               workers=2)) == [2, 6]


def test_parallel_map_stop_fn():
    """stop_fn 生效: 提前中断且不抛异常。"""
    from wyckoff._shared import parallel_map
    calls = []
    stop = [False]

    def fn(x):
        calls.append(x)
        return x

    parallel_map(range(100), fn, workers=4, stop_fn=lambda: stop[0])
    # 不崩溃即可; 若 stop_fn 在派发前就 True, 应返回空
    assert parallel_map([1, 2], fn, workers=4, stop_fn=lambda: True) == []
