"""模拟盘日志 (wyckoff/paper_log.py) 骨架单测 (T2: 最薄模块补测)。

覆盖: 日志开关 (set_enabled/disabled)、事件写入与汇总、账户快照去重、
候选截断、查询/范围/日期列表、日报文本、清理与清空。
所有用例把 _LOG_DIR 指向 tmp_path, 不触碰真实 DATA_DIR 下的 paper_logs。
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff import paper_log as pl


def _use_tmp_log_dir(monkeypatch, tmp_path):
    d = str(tmp_path / "paper_logs")
    monkeypatch.setattr(pl, "_LOG_DIR", d)
    return d


def _today():
    return datetime.now().strftime("%Y-%m-%d")


# ── 开关 ──

def test_set_enabled_toggles_and_restores():
    """set_enabled 返回旧值; disabled() 上下文退出后恢复原状态。"""
    old = pl.set_enabled(True)
    assert old is True or old is False
    try:
        assert pl.set_enabled(False) is True
        assert pl.logging_enabled() is False
    finally:
        pl.set_enabled(True)

    assert pl.logging_enabled() is True
    with pl.disabled():
        assert pl.logging_enabled() is False
    assert pl.logging_enabled() is True, "disabled() 退出必须恢复原状态"


def test_disabled_blocks_write(monkeypatch, tmp_path):
    """开关关闭时 _add_event 立即 no-op, 不产生事件文件内容。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    with pl.disabled():
        pl.log_buy("600104", "上汽集团", 100, 10.0, 90)
    data = pl.get_log(_today())
    assert data.get("events") == [], "禁用期间不得写入事件"


# ── 事件写入与汇总 ──

def test_log_buy_and_sell_summary(monkeypatch, tmp_path):
    """买入/卖出落盘 + summary 计数与金额/盈亏口径正确。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()

    pl.log_buy("600104", "上汽集团", 100, 10.1234, 92,
               strategy="paper_discipline_bull", event_type="Spring", reason="候选买入")
    pl.log_sell("600104", "上汽集团", 100, 10.0, 11.0, reason="止盈", ret=0.1,
                bars_held=5, strategy="paper_discipline_bull", event_type="Spring")

    data = pl.get_log(_today())
    types = [e["type"] for e in data["events"]]
    assert types == ["buy", "sell"]
    assert data["summary"]["buy_count"] == 1
    assert data["summary"]["sell_count"] == 1

    buy = data["events"][0]["detail"]
    assert buy["price"] == 10.123, "价格应 round 到 3 位"
    assert buy["amount"] == round(100 * 10.1234, 2)

    sell = data["events"][1]["detail"]
    assert sell["pnl"] == 100.0, "(11-10)*100"
    assert sell["ret"] == 0.1
    assert sell["ret_pct"] == "+10.00%"
    assert sell["bars_held"] == 5


def test_log_scan_caps_candidates(monkeypatch, tmp_path):
    """候选明细最多记录 20 条, 其余字段只存关键列。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()
    cands = [{"code": f"{i:06d}", "name": f"股票{i}", "type": "Spring",
              "conf": 90, "last": 10.0, "strategy": "s", "sector": "x",
              "secret": "不应落盘"} for i in range(30)]
    pl.log_scan(1, 5000, 30, candidates=cands,
                gate_results={"market": True})

    detail = pl.get_log(_today())["events"][0]["detail"]
    assert detail["codes_scanned"] == 5000
    assert detail["candidates_found"] == 30
    assert len(detail["candidates"]) == 20, "候选明细应截断到 20"
    assert "secret" not in detail["candidates"][0], "只保留白名单字段"
    assert detail["gate_results"] == {"market": True}


def test_account_snapshot_dedup(monkeypatch, tmp_path):
    """连续相同快照去重; 有变化的快照追加。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()

    pl.log_account_snapshot(1_000_000.0, 500_000.0, 3, 1)
    pl.log_account_snapshot(1_000_000.0, 500_000.0, 3, 1)   # 完全相同 → 跳过
    pl.log_account_snapshot(1_001_000.0, 500_000.0, 3, 1)   # 净值变化 → 追加

    events = pl.get_log(_today())["events"]
    assert [e["type"] for e in events] == ["account", "account"]
    assert events[0]["detail"]["equity"] == 1_000_000.0


# ── 查询接口 ──

def test_get_log_range_and_dates(monkeypatch, tmp_path):
    """范围查询只返回有事件的日期; 日期列表按升序。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()
    pl.log_buy("600104", "上汽集团", 100, 10.0, 90)

    today = _today()
    rng = pl.get_log_range(today, today)
    assert len(rng) == 1 and rng[0]["date"] == today

    # 空日期段不返回
    yest = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    assert pl.get_log_range(yest, yest) == []

    assert pl.get_log_dates() == [today]
    assert pl.get_recent_logs(days=7), "近7天应包含今天"


def test_format_daily_report(monkeypatch, tmp_path):
    """日报包含汇总行与各类型事件的可读文本。"""
    _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()
    pl.log_scan(1, 100, 1)
    pl.log_buy("600104", "上汽集团", 100, 10.0, 90, reason="候选买入")
    pl.log_sell("600104", "上汽集团", 100, 10.0, 11.0, reason="止盈", ret=0.1)
    pl.log_account_snapshot(1_000_000.0, 500_000.0, 2, 0)

    text = pl.format_daily_report(_today())
    assert f"=== 模拟盘日志 {_today()} ===" in text
    assert "扫描次数: 1" in text
    assert "买入次数: 1" in text
    assert "卖出次数: 1" in text
    assert "买入 600104 上汽集团 100股" in text
    assert "收益=+10.00%" in text
    assert "账户快照" in text
    assert "风控拦截: 0" in text


# ── 清理 ──

def test_clear_today_and_cleanup_old_logs(monkeypatch, tmp_path):
    """clear_today 清空当日事件; cleanup_old_logs 只删超过保留期的文件。"""
    log_dir = _use_tmp_log_dir(monkeypatch, tmp_path)
    pl.clear_today()
    pl.log_buy("600104", "上汽集团", 100, 10.0, 90)
    assert pl.get_log(_today())["events"], "前置: 今日应有事件"

    # 造一个 120 天前的旧日志文件
    old_date = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    old_file = os.path.join(log_dir, f"paper_log_{old_date}.json")
    with open(old_file, "w", encoding="utf-8") as f:
        json.dump({"date": old_date, "events": [{"ts": "x", "type": "buy", "detail": {}}],
                   "summary": {}}, f)

    removed = pl.cleanup_old_logs(keep_days=90)
    assert removed == 1, "120 天前的日志应被清理"
    assert os.path.exists(os.path.join(log_dir, f"paper_log_{_today()}.json")), \
        "今日日志应保留"
    assert _today() in pl.get_log_dates(), "今日应仍在日期列表中"
    assert old_date not in pl.get_log_dates()

    pl.clear_today()
    assert pl.get_log(_today())["events"] == []
    assert pl.get_log(_today())["summary"] == {}
