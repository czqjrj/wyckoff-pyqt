"""扫描样本入库 (消除选择偏差) 测试: 批量入库 / 扫描钩子 / 事件回传。

背景: wx_signal_accuracy.json 原覆盖仅约 99 只自选股 (打开图表才 record_signals),
而模拟盘扫描的是全市场 ~3100 只主板 → 强事件命中率统计存在严重选择偏差。
本测试覆盖:
  - record_events_batch: 多标的一次性合并/去重/立即评估/弱类型剔除;
  - pick_candidates 扫描钩子: scan_individual 回传检测事件 → 批量入库,
    低质池 (ST/退/历史低价) 标的被跳过;
  - scan_individual events_out: 与候选解耦的全量事件回传。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

import wyckoff.paper as paper
import wyckoff.paper._selection as _selection
import wyckoff.signal_accuracy as sa
import wyckoff.strategies.candidates as cands


def _df(n=400):
    base = np.linspace(10.0, 11.0, n)
    days = pd.bdate_range("2025-06-02", periods=n)
    return pd.DataFrame({
        "day": days,
        "open": base,
        "high": base + 0.05,
        "low": base - 0.05,
        "close": base,
        "volume": np.random.rand(n) * 1e6,
    })


def _ev(idx=300, typ="Spring", conf=90):
    df = _df()
    return {"type": typ, "idx": idx, "date": str(df["day"].iloc[idx])[:10],
            "price": float(df["close"].iloc[idx]), "conf": conf}


def _isolated(tmp_path):
    sa.SIGNAL_ACCURACY_FILE = str(tmp_path / "wx_signal_accuracy.json")
    sa._WINRATE_CACHE = None


class EvtMgr:
    """假策略管理器: 回传检测事件, 返回一个左侧买点候选 (gated=False)。"""

    def __init__(self, evs):
        self.evs = evs

    def scan_individual(self, code, df=None, min_conf=90, gates_ok=None,
                        name="", event_types=None, strategies=None,
                        st_confirm=False, events_out=None):
        if events_out is not None:
            events_out["evs"] = self.evs
        return {"strategy": "long_buy_left", "type": "Spring", "idx": 388,
                "conf": 85, "kind": "spring", "entry_price": 10.1,
                "stop_price": 9.4, "target_price": 13.1, "rr": 3.0,
                "gated": False}


def _apply(monkeypatch, mgr):
    monkeypatch.setattr(paper, "_strategy_manager", lambda: mgr)
    monkeypatch.setattr("wyckoff.datasource.fetch_kline",
                        lambda *a, **k: _df().copy())
    monkeypatch.setattr("wyckoff.indicators.add_indicators", lambda df, **k: df)
    monkeypatch.setattr("wyckoff.fundamental.fetch_sector", lambda c: "")
    monkeypatch.setattr(paper, "_market_trend_ok", lambda: (True, ""))
    return paper._CUR.get("enable_long_left", False)


def _with_long_left(monkeypatch, val=True):
    """开启左侧买点赛道 (独立赛道, enable_long_left 默认 False)。"""
    monkeypatch.setattr(paper, "_CUR", {**paper._CUR, "enable_long_left": val})


def test_record_events_batch_add_eval_dedup(tmp_path, monkeypatch):
    """批量入库: 新增 + 立即评估 + 弱类型剔除 + 重跑不新增。"""
    _isolated(tmp_path)
    df = _df()
    evs = [_ev(300, "Spring", 90), _ev(200, "UTAD", 88), _ev(150, "JOC", 75)]
    st = sa.record_events_batch([(df, "sh600999", "600999", 240, len(df),
                                  evs, [], "测试")])
    assert st["added"] == 2       # JOC (弱类型) 被剔除
    assert st["evaluated"] == 2
    recs = sa.load_signals()
    assert len(recs) == 2
    types = {r["type"] for r in recs}
    assert types == {"Spring", "UTAD"}
    assert all(r["status"] == "done" and r["results"] for r in recs)
    # 幂等重跑
    st2 = sa.record_events_batch([(df, "sh600999", "600999", 240, len(df),
                                   evs, [], "测试")])
    assert st2["added"] == 0
    assert len(sa.load_signals()) == 2


def test_record_events_batch_vsa_skipped_with_empty(tmp_path):
    """传 vsa_signals=[] 时只记事件维度 (扫描钩子不付 VSA 重检成本)。"""
    _isolated(tmp_path)
    df = _df()
    evs = [_ev(300, "Spring", 92)]
    sa.record_events_batch([(df, "sh600888", "600888", 240, len(df),
                             evs, [], "")])
    recs = sa.load_signals()
    assert len(recs) == 1 and recs[0]["kind"] == "event"


def test_pick_candidates_records_scanned_events(tmp_path, monkeypatch):
    """扫描钩子: 假管理器回传事件 → 全市场扫描后入库并评估。"""
    _isolated(tmp_path)
    mgr = EvtMgr([_ev(300, "Spring", 90)])
    _apply(monkeypatch, mgr)
    _with_long_left(monkeypatch)
    out = paper.pick_candidates(universe=["sh600001"], max_codes=5,
                                skip_gates=True)
    assert len(out) == 1
    recs = sa.load_signals()
    assert len(recs) == 1
    assert recs[0]["symbol"] == "sh600001"
    assert recs[0]["type"] == "Spring"
    assert recs[0]["conf"] == 90
    assert recs[0]["status"] == "done" and recs[0]["results"]


def test_pick_candidates_skips_low_quality(tmp_path, monkeypatch):
    """扫描钩子: 低质池 (如 ST) 标的的强事件不进库, 候选仍正常返回。"""
    _isolated(tmp_path)
    mgr = EvtMgr([_ev(300, "Spring", 90)])
    _apply(monkeypatch, mgr)
    _with_long_left(monkeypatch)

    def _block_recording_only(code, price=None, name=None):
        # 候选低质检查带 price, 入库路径 price=None —— 只拦入库路径
        return price is None and (name or "").startswith("ST")

    monkeypatch.setattr(_selection, "_stock_name", lambda c: "ST测试")
    monkeypatch.setattr(_selection, "_is_low_quality", _block_recording_only)
    out = paper.pick_candidates(universe=["sh600002"], max_codes=5,
                                skip_gates=True)
    assert len(out) == 1          # 候选仍正常返回
    assert sa.load_signals() == []  # 但其事件不进信号库


def test_scan_individual_events_out(monkeypatch):
    """events_out: 与候选解耦, 即使产不出候选也回传全部检测事件。"""
    df = _df()
    fake_evs = [_ev(300, "Spring", 95), _ev(180, "ST", 80)]
    monkeypatch.setattr("wyckoff.events.detect_all", lambda df, piv: fake_evs)
    monkeypatch.setattr("wyckoff.indicators.find_pivots",
                        lambda df, order=6: [])
    eo = {}
    cands.scan_individual("sh600001", df=df, min_conf=90, events_out=eo)
    assert eo["evs"] is fake_evs
    assert "piv" in eo
