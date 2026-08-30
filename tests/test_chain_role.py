# -*- coding: utf-8 -*-
"""产业链作用提升后的单元测试: 环节定位 / 因子 / conf 加分 / 同链去重。"""
import datetime as _dt
import sys
sys.path.insert(0, r"E:\wyckoff-pyqt")

import pytest

from wyckoff import chain, conservative_bt as cbt


# ───────────────────────── 环节定位 / 分组 ─────────────────────────

def test_chain_home_mapping():
    assert chain.chain_home("半导体材料") == ("半导体", "upstream")
    assert chain.chain_home("消费电子") == ("半导体", "downstream")
    assert chain.chain_home("银行") == (None, None)


def test_chain_cap_key():
    assert chain.chain_cap_key("半导体材料") == "半导体"
    assert chain.chain_cap_key("银行") is None


# ───────────────────────── 产业链因子 (stub 网络依赖) ─────────────────────────

def _stub_strength_snapshot(trans="上游→下游", up=0.9, mid=0.6, dn=0.35):
    chain.sector_strength_pct = lambda n: 0.85
    chain.chain_snapshot = lambda n: [{
        "name": "半导体", "trans": trans,
        "avg": {"upstream": up, "midstream": mid, "downstream": dn}}]


def test_chain_conf_adjust_off_graph_zero():
    # 不在图谱内 / 数据不可用 → 0 (fail-soft, 不改变现行为)
    chain.sector_strength_pct = lambda n: None
    assert chain.chain_conf_adjust("银行", 90) == 0
    assert chain.chain_conf_adjust("", 90) == 0


def test_chain_conf_adjust_beneficiary_positive():
    _stub_strength_snapshot("上游→下游", up=0.9, mid=0.6, dn=0.35)
    # 下游=受益环节 → 正向
    adj = chain.chain_conf_adjust("消费电子", 90)
    assert adj > 0


def test_chain_conf_adjust_contra_non_positive():
    _stub_strength_snapshot("上游→下游", up=0.9, mid=0.6, dn=0.35)
    # 上游=已领涨/逆传导环节 → 打分折扣 (不强于受益环节)
    up_adj = chain.chain_conf_adjust("半导体材料", 90)
    dn_adj = chain.chain_conf_adjust("消费电子", 90)
    assert up_adj <= dn_adj


def test_chain_factor_for_historical_ts_uses_strength_at(monkeypatch):
    from wyckoff import fundamental as _  # noqa
    captured = {}

    def fake_strength_at(name, ts, max_gap_days=45):
        captured["ts"] = ts
        return 0.8
    chain.strength_at = fake_strength_at
    chain.chain_snapshot = lambda n: [{
        "name": "半导体", "trans": "上游→下游",
        "avg": {"upstream": 0.9, "midstream": 0.6, "downstream": 0.35}}]
    cf = chain.chain_factor_for("消费电子", ts=_dt.datetime(2024, 1, 1))
    assert cf is not None
    assert captured.get("ts") is not None  # 走历史快照路径 (无前视)


# ───────────────────────── 同产业链去重 (保守回测) ─────────────────────────

def _sig(code, day, conf, ty="Spring"):
    return {"date": day, "code": code, "name": "x", "type": ty,
            "conf": conf, "ret5": 0.02, "ret10": 0.04, "ret20": 0.10,
            "ret40": 0.15}


def test_dedup_chain_keeps_one_per_chain_per_day():
    sigs = [
        _sig("sh600001", _dt.date(2024, 1, 2), 95),
        _sig("sh600002", _dt.date(2024, 1, 2), 70),   # 同链低 conf
        _sig("sz000003", _dt.date(2024, 1, 2), 88),   # 不同链
        _sig("sz000004", _dt.date(2024, 1, 3), 90),
    ]
    cmap = {"sh600001": "半导体", "sh600002": "半导体", "sz000003": "医药",
            "sz000004": "半导体"}
    out = cbt.dedup_signals(sigs, mode="chain", chain_map=cmap)
    by_date = {}
    for r in out:
        by_date.setdefault(r["date"], []).append(r)
    d2 = by_date[_dt.date(2024, 1, 2)]
    # 同日每链一组: 半导体保 95, 医药保 88
    assert len(d2) == 2
    assert {r["conf"] for r in d2} == {95, 88}


def test_dedup_chain_falls_back_to_date_without_map():
    sigs = [_sig("sh600001", _dt.date(2024, 1, 2), 95),
            _sig("sh600002", _dt.date(2024, 1, 2), 90)]
    out = cbt.dedup_signals(sigs, mode="chain", chain_map=None)
    assert len(out) == 1  # 退化为同日去重


def test_chain_map_from_sectors_downgrades_to_sector():
    smap = {"sh600001": "半导体材料", "sh600002": "消费电子", "sh600003": "银行"}
    cmap = cbt.chain_map_from_sectors(smap)
    assert cmap["sh600001"] == "半导体"
    assert cmap["sh600002"] == "半导体"
    assert cmap["sh600003"] == "银行"   # 不在图谱内用板块名兜底
