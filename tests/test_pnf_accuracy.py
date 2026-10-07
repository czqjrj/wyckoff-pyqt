"""点数图三档目标准确率评估 (wyckoff/pnf_accuracy.py) 骨架单测 (T2)。

覆盖: 合成行情生成的可复现性、段收集结构、_row 统计口径、报告落盘/读取。
不跑 run_eval 全流程 (需批量取数), 只锁定可离线验证的纯函数与 IO 契约。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from wyckoff import pnf_accuracy as pa

# ── 合成行情 ──

def test_gen_trend_and_range_deterministic():
    """同 seed 完全可复现, 不同 seed 结果不同。"""
    a = pa._gen_trend_and_range(7)
    b = pa._gen_trend_and_range(7)
    c = pa._gen_trend_and_range(8)
    assert a["close"].tolist() == b["close"].tolist(), "同 seed 必须可复现"
    assert a["close"].tolist() != c["close"].tolist(), "不同 seed 应产生不同序列"


def test_gen_trend_and_range_shape():
    """列齐全 (OHLCV+day), 根数与数值域合法。"""
    df = pa._gen_trend_and_range(1, n_bars=400)
    for col in ("day", "open", "high", "low", "close", "volume"):
        assert col in df.columns, f"缺列 {col}"
    assert len(df) == 400
    assert (df["close"] > 0).all()
    assert (df["high"] >= df["close"]).all()
    assert (df["low"] <= df["close"]).all()
    assert (df["volume"] > 0).all()


# ── 段收集 ──

def test_collect_segments_returns_boxed_targets():
    """段列表非空, 每段带回 _box (供 UI 换算列宽) 与突破方向。"""
    df = pa._gen_trend_and_range(7)
    hist = pa._collect_segments(df)
    assert hist, "合成行情应产出至少一个历史段"
    assert len(hist) <= 20, "max_items=20 截断"
    for h in hist:
        assert "_box" in h and h["_box"] > 0, "每段须带盒宽 _box"
        assert h["direction"] in ("up", "down")
        assert {"break_col", "tr_top", "tr_bottom"} <= set(h)


# ── 统计行 ──

def _counter():
    return {"n": 3, "hist": [
        {"up目标_保守": 22.0, "uphit_保守": True,
         "up概率_保守": 0.6, "up空间_保守%": 10.0},
        {"up目标_保守": 21.0, "uphit_保守": False,
         "up概率_保守": 0.4, "up空间_保守%": 8.0},
        {"up目标_保守": None, "uphit_保守": False,
         "up概率_保守": 0.5, "up空间_保守%": 6.0},
    ]}


def test_row_stats():
    """n_valid 只数有目标的段, hit_rate/均值/校准差按文档口径。"""
    row = pa._row(_counter(), "up", "保守")
    assert row["n_total"] == 3
    assert row["n_valid"] == 2, "目标为 None 的段不计入有效"
    assert row["n_hit"] == 1
    assert row["hit_rate%"] == 50.0
    assert row["avg_prob%"] == 50.0, "概率均值含全部数值项 (0.6/0.4/0.5)"
    assert row["calib_pp"] == 0.0, "calib = avg_prob - hit_rate"
    assert row["avg_space%"] == 8.0


def test_row_empty_counter_returns_neutral():
    """无样本: 命中率 0, 概率/校准/空间为 None (UI 侧应显示占位)。"""
    row = pa._row({"n": 0, "hist": []}, "down", "激进")
    assert row["n_total"] == 0 and row["n_valid"] == 0 and row["n_hit"] == 0
    assert row["hit_rate%"] == 0.0
    assert row["avg_prob%"] is None
    assert row["calib_pp"] is None
    assert row["avg_space%"] is None


def test_row_down_direction_keys():
    """down 方向键名独立计数, 不与 up 串味。"""
    counter = {"n": 1, "hist": [
        {"down目标_中": 9.0, "downhit_中": True,
         "down概率_中": 0.7, "down空间_中%": 5.0},
    ]}
    row = pa._row(counter, "down", "中")
    assert row["n_valid"] == 1 and row["n_hit"] == 1
    assert row["hit_rate%"] == 100.0 and row["avg_prob%"] == 70.0
    assert row["calib_pp"] == -30.0


# ── 报告 IO ──

def test_save_and_load_latest_report(monkeypatch, tmp_path):
    """save_report 写 latest + 日期归档, load_latest_report 原样读回。"""
    d = str(tmp_path / "pnf_accuracy")
    monkeypatch.setattr(pa, "PNF_ACC_DIR", d)
    monkeypatch.setattr(pa, "PNF_ACC_LATEST", os.path.join(d, "pnf_latest.json"))

    payload = {"ts": "2026-10-07T09:30:00", "total_segments": 42,
               "tiers": [{"tier": "保守", "hit_rate%": 70.0}]}
    path = pa.save_report(payload)
    assert path == pa.PNF_ACC_LATEST
    assert os.path.exists(path)
    assert os.path.exists(os.path.join(d, "pnf_acc_20261007.json")), "应按日期归档"

    got = pa.load_latest_report()
    assert got["total_segments"] == 42
    assert got["tiers"][0]["tier"] == "保守"
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["ts"] == "2026-10-07T09:30:00"


def test_load_latest_report_missing_returns_empty(monkeypatch, tmp_path):
    """文件缺失/损坏 → 返回空 dict (UI 侧按无数据处理, 不抛异常)。"""
    d = str(tmp_path / "pnf_accuracy_missing")
    monkeypatch.setattr(pa, "PNF_ACC_DIR", d)
    monkeypatch.setattr(pa, "PNF_ACC_LATEST", os.path.join(d, "pnf_latest.json"))
    assert pa.load_latest_report() == {}

    os.makedirs(d, exist_ok=True)
    with open(pa.PNF_ACC_LATEST, "w", encoding="utf-8") as f:
        f.write("{broken json")
    assert pa.load_latest_report() == {}


@pytest.mark.parametrize("tier", list(pa.TIERS))
def test_tiers_registry(tier):
    """三档标签注册表稳定 (UI/报告按顺序消费)。"""
    assert tier in ("保守", "中", "激进")
