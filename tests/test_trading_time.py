"""wyckoff/trading_time.py: 交易日历缓存 / 节假日放行逻辑测试。"""

import datetime
import json
import sys
from unittest import mock

from wyckoff import trading_time as tt


def _weekday(hh=10, mm=0, month=9, day=10):
    return datetime.datetime(2026, month, day, hh, mm)  # 2026-09-10 为周四


def test_in_trading_hours_boundaries(monkeypatch):
    for hh, mm in ((9, 30), (11, 30), (13, 0), (15, 0)):
        assert tt.in_trading_hours(now=_weekday(hh, mm)) is True, (hh, mm)
    for hh, mm in ((9, 29), (11, 31), (12, 59), (15, 1)):
        assert tt.in_trading_hours(now=_weekday(hh, mm)) is False, (hh, mm)


def test_is_trading_day_weekend():
    sunday = datetime.datetime(2026, 9, 13, 10, 0)
    assert tt.is_trading_day(now=sunday) is False


def test_load_trade_dates_reads_fresh_cache(tmp_path, monkeypatch):
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text(json.dumps({
        "fetched": datetime.datetime.now().strftime("%Y-%m-%d"),
        "dates": ["2026-09-10", "2026-09-11"],
    }), encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    got = tt._load_trade_dates()
    assert got == {"2026-09-10", "2026-09-11"}


def test_load_trade_dates_stale_cache_falls_back_offline(tmp_path, monkeypatch):
    # 当日缓存过期 + akshare 不可用 → 返回 None (offline 兜底)
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text(json.dumps({
        "fetched": "2020-01-01",
        "dates": ["2020-01-02"],
    }), encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    with mock.patch.dict(sys.modules, {"akshare": None}):  # import akshare 报错
        assert tt._load_trade_dates() is None


def test_load_trade_dates_corrupt_cache(tmp_path, monkeypatch):
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text("not-json{", encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    with mock.patch.dict(sys.modules, {"akshare": None}):
        assert tt._load_trade_dates() is None


def test_is_trading_day_cache_today(tmp_path, monkeypatch):
    # 固定用周四 (2026-09-10), 避免真实"今天"落在周末被直接排除
    now = _weekday()
    today = now.strftime("%Y-%m-%d")
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text(json.dumps({
        "fetched": today, "dates": [today, "2030-01-01"],
    }), encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    assert tt.is_trading_day(now=now) is True


def test_is_trading_day_cache_holiday(tmp_path, monkeypatch):
    # 日历覆盖范围延伸至今后 (newest ≥ today) 且缺今天 → 确为节假日
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text(json.dumps({
        "fetched": today, "dates": ["2026-01-02", "2030-12-31"],
    }), encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    assert tt.is_trading_day(now=_weekday()) is False


def test_is_trading_day_calendar_not_covering_year(tmp_path, monkeypatch):
    # 列表最新日期早于今天 → 视为未覆盖当年, 放行
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cache = tmp_path / "wx_trade_dates.json"
    cache.write_text(json.dumps({
        "fetched": today, "dates": ["2020-12-31"],
    }), encoding="utf-8")
    monkeypatch.setattr(tt, "TRADE_DATES_CACHE", str(cache))
    assert tt.is_trading_day(now=_weekday()) is True


def test_is_trading_day_no_calendar_allow_by_weekday(monkeypatch):
    monkeypatch.setattr(tt, "_load_trade_dates", lambda: None)
    assert tt.is_trading_day(now=_weekday()) is True


def test_gate_reason_anytime_bypasses_all(monkeypatch):
    night = _weekday(hh=20)
    assert tt.gate_reason(now=night, anytime=True) is None


def test_gate_reason_off_hours_and_holiday(monkeypatch):
    monkeypatch.setattr(tt, "is_trading_day", lambda now=None: False)
    night = _weekday(hh=20)
    reason = tt.gate_reason(now=night)
    assert reason is not None and "非交易时段" in reason
    day = _weekday(hh=10)
    reason = tt.gate_reason(now=day)
    assert reason is not None and "非交易日" in reason
