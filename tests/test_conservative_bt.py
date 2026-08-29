"""保守年化回测模块测试 (wyckoff.conservative_bt)。

覆盖:
1. filter_actionable_long: 只保留已评估、事件、标称多/中性、强梯队、conf 过滤;
2. per_trade_stats: 每笔均值/胜率/成本与止损生效、空样本兜底;
3. portfolio_backtest: 并发上限、仓位切分、缺口翻车、空输入;
4. export_csv: 写出且不抛, 表头正确;
5. build_report: 生成 markdown 含关键指标。
"""
import os

from wyckoff import conservative_bt as cbt


def _rec(type_, conf, ret20, date="2024-06-01", ret5=None, status="done",
         kind="event"):
    results = {"20": {"ret": ret20}}
    if ret5 is not None:
        results["5"] = {"ret": ret5}
    return {"status": status, "kind": kind, "type": type_, "conf": conf,
            "date": date, "results": results, "code": "600000", "name": "测试"}


def _recs():
    # 多头强事件: Spring
    a = [_rec("Spring", 95, 0.05, date=f"2024-{(i % 12) + 1:02d}-05", ret5=0.02)
         for i in range(30)]
    # 空头强事件 (应被剔除: A股不可裸空)
    b = [_rec("UTAD", 95, 0.05) for _ in range(5)]
    # 弱事件/非强梯队 (应被剔除)
    c = [_rec("SOS", 95, 0.05) for _ in range(5)]
    # 未评估 (应被剔除)
    d = [_rec("Spring", 95, 0.05, status="pending") for _ in range(3)]
    return a + b + c + d


def test_filter_actionable_long_filters():
    recs = _recs()
    out = cbt.filter_actionable_long(recs)
    # 只保留 30 条 Spring
    assert len(out) == 30
    assert all(r["type"] == "Spring" for r in out)
    assert all(r["conf"] >= 90 for r in out)


def test_filter_conf_min():
    recs = _recs()
    out = cbt.filter_actionable_long(recs, conf_min=96)
    assert len(out) == 0          # 全 95 < 96
    out2 = cbt.filter_actionable_long(recs, conf_min=90)
    assert len(out2) == 30


def test_per_trade_stats_values_and_stop():
    recs = _recs()
    pt = cbt.per_trade_stats(recs, conf_min=90)
    assert pt.n == 30
    # 每笔均值(净, cost 0.8%) ≈ 5% - 0.8% = 4.2%
    assert abs(pt.mean_net - 4.2) < 0.01
    assert pt.win_rate == 100.0        # 全赢
    assert pt.pl_ratio >= 0            # 无亏损 → 盈亏比退化为 0 (全赢无损失样本)
    # 止损-5: ret5=2% > -5%, 不停损
    pt_stop = cbt.per_trade_stats(recs, conf_min=90, stop=-0.05)
    assert abs(pt_stop.mean_net - 4.2) < 0.01


def test_per_trade_stop_triggers():
    recs = [_rec("Spring", 90, 0.20, ret5=-0.10) for _ in range(10)]  # 5根已破 -5%
    pt = cbt.per_trade_stats(recs, conf_min=90, stop=-0.05, cost=0.0)
    # 止损返回 -0.05 (近似, 成本0)
    assert abs(pt.mean_net + 5.0) < 0.01   # -5% (成本0)
    pt_nostop = cbt.per_trade_stats(recs, conf_min=90, stop=None, cost=0.0)
    assert abs(pt_nostop.mean_net - 20.0) < 0.01


def test_empty_records_no_crash():
    pt = cbt.per_trade_stats([], conf_min=0)
    assert pt.n == 0 and pt.cagr_low == 0
    pv = cbt.portfolio_backtest([])
    assert pv.n_trades == 0 and pv.cagr == 0
    assert cbt.filter_actionable_long([]) == []


def test_portfolio_concurrency_and_size():
    # 日期分散在一年内 → 并发上限限制同时持仓数, 但全年可多轮执行
    recs = [_rec("Spring", 95, 0.05, date=f"2024-{(i % 12) + 1:02d}-05",
                 ret5=0.02) for i in range(20)]
    pv = cbt.portfolio_backtest(recs, conf_min=90, position_count=3)
    # 分散到 12 个不同月份 → 多数可入槽; 并发 3 生效
    assert 3 <= pv.n_trades <= 20
    assert pv.n_trades == 0 or pv.cagr > -100.0   # 不溢出、合理范围
    pv2 = cbt.portfolio_backtest(recs, conf_min=90, position_count=6)
    assert pv2.n_trades >= pv.n_trades            # 槽位越多执行越多


def test_export_csv_writes(tmp_path):
    recs = _recs()
    dest = str(tmp_path / "trades.csv")
    path = cbt.export_csv(recs, conf_min=90, path=dest)
    assert os.path.exists(path)
    with open(path, encoding="utf-8-sig") as f:
        rows = list(f)
    assert rows[0].startswith("日期,代码")      # 表头
    assert len(rows) == 31                       # 1 表头 + 30 行


def test_build_report_contains_key_metrics():
    recs = _recs()
    md = cbt.build_report(recs, conf_min=90, capital=100_000, position_count=3)
    assert "# 威科夫保守年化收益回测" in md
    assert "每笔均值" in md
    assert "保守年化结论" in md


# ───────────────────────── 去重 (dedup) ─────────────────────────

def _dup_sigs():
    # 10 条同日 Spring: 5 只 code A-E, 每只 2 条不同 conf
    out = []
    for ci, code in enumerate("ABCDE"):
        for k, conf in enumerate((99, 60)):
            out.append({"date": _dt_date("2024-01-05"), "code": code,
                        "name": code, "type": "Spring", "conf": conf,
                        "ret5": 0.02, "ret10": 0.03, "ret20": 0.05,
                        "ret40": 0.06})
    return out


def _dt_date(s):
    import datetime as _mdt
    return _mdt.datetime.strptime(s, "%Y-%m-%d")


def test_dedup_date_keeps_highest_conf_per_day():
    sigs = _dup_sigs()
    out = cbt.dedup_signals(sigs, mode="date")
    assert len(out) == 1          # 全部同一天 → 只留 1 笔
    assert out[0]["conf"] == 99   # 优先 conf 高


def test_dedup_sector_keeps_one_per_sector_per_day():
    sigs = _dup_sigs()
    smap = {"A": "汽车", "B": "汽车", "C": "银行", "D": "银行", "E": "医药"}
    out = cbt.dedup_signals(sigs, mode="sector", sector_map=smap)
    # 同日 3 个板块 → 各留 1 笔 = 3 笔
    assert len(out) == 3
    assert sorted(r["conf"] for r in out) == [99, 99, 99]


def test_dedup_sector_falls_back_to_date_without_map():
    sigs = _dup_sigs()
    out = cbt.dedup_signals(sigs, mode="sector", sector_map=None)
    assert len(out) == 1          # 无地图 → 退化为 date
    assert out[0]["conf"] == 99


def test_dedup_none_keeps_all():
    sigs = _dup_sigs()
    assert len(cbt.dedup_signals(sigs, mode="none")) == 10


def test_dedup_invalid_mode_raises():
    import pytest
    with pytest.raises(ValueError):
        cbt.dedup_signals(_dup_sigs(), mode="bogus")


def test_filter_actionable_dedup_date():
    recs = [{"status": "done", "kind": "event", "type": "Spring",
             "conf": 95, "date": "2024-01-05", "code": "A", "name": "A",
             "results": {"20": {"ret": 0.05}}},
            {"status": "done", "kind": "event", "type": "Spring",
             "conf": 80, "date": "2024-01-05", "code": "B", "name": "B",
             "results": {"20": {"ret": 0.06}}}]
    out = cbt.filter_actionable_long(recs, dedup="date")
    assert len(out) == 1
    assert out[0]["conf"] == 95
