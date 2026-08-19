# -*- coding: utf-8 -*-
"""信号准确度追踪模块测试: 记录/去重/冷却窗/过期清理/胜率表。

覆盖:
  - record_signals: 同日期去重; 冷却窗合并洪水信号; 重复调用不新增。
  - expire_stale_signals: 未评估超期删除, 已评估保留。
  - load_win_rates / win_rate_of: 胜率表与样本下限。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

import wyckoff.signal_accuracy as sa


def _df(n=100):
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": np.linspace(10, 15, n), "high": np.linspace(11, 16, n),
        "low": np.linspace(9, 14, n), "close": np.linspace(10.5, 15.5, n),
        "volume": np.random.rand(n) * 1e6,
    })


def _ev(idx, typ="NS", conf=60):
    df = _df()
    return {"idx": idx, "type": typ, "conf": conf, "price": 12.0,
            "date": df["day"].iloc[idx].strftime("%Y-%m-%d %H:%M:%S")}


def _isolated(tmp_path, monkeypatch):
    sa.SIGNAL_ACCURACY_FILE = str(tmp_path / "wx_signal_accuracy.json")
    sa._WINRATE_CACHE = None


def test_record_dedup_same_date(tmp_path, monkeypatch):
    """同 symbol+scale+kind+type+date 视为同一信号, 重复记录不新增。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = [_ev(10), _ev(10), _ev(90)]
    n1 = sa.record_signals(df, "sh600104", "600104", 240, 100,
                           events=ev, vsa_signals=[], name="测试")
    n2 = sa.record_signals(df, "sh600104", "600104", 240, 100,
                           events=ev, vsa_signals=[], name="测试")
    assert n1 == 2  # idx10 与 idx90 两个独立信号 (同日期合并)
    assert n2 == 0  # 再跑一次不新增
    assert len(sa.load_signals()) == 2


def test_cooldown_merges_flood(tmp_path, monkeypatch):
    """冷却窗合并连续洪水信号: 18 个同类型密集信号 → 少量代表。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = [_ev(i) for i in range(5, 40, 2)]  # 18 个 NS
    n = sa.record_signals(df, "sh600104", "600104", 240, 100,
                          events=ev, vsa_signals=[], name="测试")
    recs = sa.load_signals()
    assert n == 3  # 15 根冷却窗内只留 3 个代表
    assert len(recs) == 3
    # 再跑一次 (含已评估的代表) 不新增
    n2 = sa.record_signals(df, "sh600104", "600104", 240, 100,
                           events=ev, vsa_signals=[], name="测试")
    assert n2 == 0
    assert len(sa.load_signals()) == 3


def test_cooldown_keeps_far_signals(tmp_path, monkeypatch):
    """间隔超过冷却窗的同类型信号各自独立。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = [_ev(5), _ev(90)]
    n = sa.record_signals(df, "sh600104", "600104", 240, 100,
                          events=ev, vsa_signals=[], name="测试")
    assert n == 2
    assert len(sa.load_signals()) == 2


def test_expire_stale_signals(tmp_path, monkeypatch):
    """未评估且创建超期 → 删除; 已评估 → 保留。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = [_ev(10), _ev(90)]
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev, vsa_signals=[], name="测试")
    recs = sa.load_signals()
    assert len(recs) == 2
    # 全部标记为超期未评估
    for r in recs:
        r["created_ts"] = 1
        r["results"] = {}
    sa.save_signals(recs)
    nd = sa.expire_stale_signals(max_age_days=30, keep_done_days=30)
    assert nd == 2
    assert sa.load_signals() == []
    # 已评估的即使超期(>max_age)也保留 (只要未超过 keep_done_days)
    df = _df()
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=[_ev(10)], vsa_signals=[], name="测试")
    recs = sa.load_signals()
    import time as _time
    old = _time.time() - 60 * 86400  # 60 天前
    for r in recs:
        r["created_ts"] = old
    sa.save_signals(recs)
    # max_age_days=30 → 未评估会删; 但这条已评估, 60天 < keep_done_days=365 → 保留
    assert sa.expire_stale_signals(max_age_days=30, keep_done_days=365) == 0
    assert len(sa.load_signals()) == 1


def test_win_rates_and_min_sample(tmp_path, monkeypatch):
    """胜率表: 样本不足的类型不回退到默认基线, 且 n<10 不参与。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    # 造 12 个 SOS 信号 (关闭冷却窗, 留足未来行情), 全部未来上涨 → 胜率 1.0
    ev = []
    for i in range(0, 12):
        idx = i * 7  # 0,7,...,77; +20根未来行情仍在 100 根窗口内
        ev.append({"idx": idx, "type": "SOS", "conf": 80, "price": 12.0,
                   "date": df["day"].iloc[idx].strftime("%Y-%m-%d %H:%M:%S")})
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev, vsa_signals=[], name="测试", cooldown_bars=0)
    rates = sa.load_win_rates(horizon=20, force=True)
    key = ("event", "SOS")
    assert key in rates
    assert rates[key]["n"] == 12
    assert abs(rates[key]["win"] - 1.0) < 1e-6
    # 样本不足的类型回退基线
    assert sa.win_rate_of("event", "不存在的类型", horizon=20) == 0.5
    # 缓存失效后重新加载
    sa.invalidate_win_rate_cache()
    assert len(sa.load_win_rates(horizon=20)) == 1


def test_l1_bayes_shrink_and_ci(tmp_path, monkeypatch):
    """L1 贝叶斯收缩: 全涨样本收缩回基线附近; Wilson CI 合理; 小样本平滑。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = []
    for i in range(0, 12):
        idx = i * 7
        ev.append({"idx": idx, "type": "SOS", "conf": 80, "price": 12.0,
                   "date": df["day"].iloc[idx].strftime("%Y-%m-%d %H:%M:%S")})
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev, vsa_signals=[], name="测试", cooldown_bars=0)
    rates = sa.load_win_rates(horizon=20, force=True)
    s = rates[("event", "SOS")]
    # 收缩值在 原始1.0 与 p0 (全池=1.0→钳到0.6) 之间 → 明显低于 1.0
    assert 0.0 < s["shrunk"] < 0.85
    # Wilson CI 单调包含 win
    assert s["ci_lo"] <= s["win"] <= s["ci_hi"]
    # 收缩语义: n 相同时, 极端胜率被向基线压缩
    p0 = s["p0"]
    expect = (12 + sa.PRIOR_ALPHA0 * p0) / (12 + sa.PRIOR_ALPHA0)
    assert abs(s["shrunk"] - expect) < 1e-6
    # 小样本 (< MIN_SHRUNK_N) 不出现在表中 (旧版 n<10 才不入表, 现在更宽)
    sa.invalidate_win_rate_cache()
    ev2 = [{"idx": 2, "type": "ST", "conf": 66, "price": 12.0,
            "date": df["day"].iloc[2].strftime("%Y-%m-%d %H:%M:%S")}]
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev2, vsa_signals=[], name="测试", cooldown_bars=0)
    rates = sa.load_win_rates(horizon=20, force=True)
    assert ("event", "ST") not in rates  # n=1 < MIN_SHRUNK_N=3
    assert sa.win_rate_of("event", "ST", horizon=20) == 0.5


def test_win_rate_of_uses_shrunk_for_small_sample(tmp_path, monkeypatch):
    """小样本 (n<10 但 ≥MIN_SHRUNK_N) 返回收缩值而非原始胜率或基线。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = []
    for i in range(0, 5):
        idx = i * 10
        ev.append({"idx": idx, "type": "SOS", "conf": 80, "price": 12.0,
                   "date": df["day"].iloc[idx].strftime("%Y-%m-%d %H:%M:%S")})
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev, vsa_signals=[], name="测试", cooldown_bars=0)
    rates = sa.load_win_rates(horizon=20, force=True)
    s = rates[("event", "SOS")]
    got = sa.win_rate_of("event", "SOS", horizon=20)
    assert got == s["shrunk"]  # 用收缩值 (5全涨 → 收缩到 0.6~0.7), 而非 1.0


def _profile_rec(typ, rets, date="2024-05-01"):
    """构造带多周期结果的信号记录 (rets: {h: ret})。"""
    return {"kind": "event", "type": typ, "date": date, "conf": 70,
            "results": {str(h): {"ret": v} for h, v in rets.items()}}


def test_win_rate_profile_consistent(tmp_path, monkeypatch):
    """L5 多周期: 各周期同向 → 边缘稳定, 数据不足 → 样本不足。"""
    _isolated(tmp_path, monkeypatch)
    recs = [_profile_rec("SOS", {5: 0.1, 10: 0.1, 20: 0.1, 40: 0.1})
            for _ in range(8)]
    sa.save_signals(recs)
    p = sa.win_rate_profile("event", "SOS")
    assert p["horizons"]["5"] is not None and p["horizons"]["40"] is not None
    assert p["consistent"] is True
    assert p["verdict"] in ("边缘稳定", "贴近随机")
    # 无记录类型 → 样本不足
    p2 = sa.win_rate_profile("event", "未知X")
    assert p2["verdict"] == "样本不足"


def test_win_rate_profile_reversal(tmp_path, monkeypatch):
    """L5 多周期: 20根全涨但 40根全跌 → 方向反转 (危险信号)。"""
    _isolated(tmp_path, monkeypatch)
    recs = [_profile_rec("JOC", {5: 0.1, 10: 0.05, 20: 0.1, 40: -0.1})
            for _ in range(8)]
    sa.save_signals(recs)
    p = sa.win_rate_profile("event", "JOC")
    assert p["consistent"] is False
    assert p["verdict"] == "方向反转"


def test_phase_reliability(tmp_path, monkeypatch):
    """L5 阶段可信度: 按阶段统计标注正确率 + L1 收缩。"""
    from wyckoff.storage import phase_reliability
    fb = []
    for _ in range(10):
        fb.append({"label": "accumulation", "verdict": "correct"})
        fb.append({"label": "distribution", "verdict": "wrong"})
    rel = phase_reliability(fb)
    assert set(rel) == {"accumulation", "distribution"}
    assert rel["accumulation"]["n"] == 10 and rel["accumulation"]["correct"] == 10
    # 全涨样本收缩回基线: 原始1.0 → shrunk < 1.0
    assert rel["accumulation"]["raw"] == 1.0
    assert 0.0 < rel["accumulation"]["shrunk"] < 1.0
    # 错误标注不入统计
    rel2 = phase_reliability([{"label": "markup", "verdict": ""}])
    assert "markup" not in rel2
    # 空输入
    assert phase_reliability([]) == {}


def test_export_review_report(tmp_path, monkeypatch):
    """复盘周报: 生成 Markdown 且包含类型汇总与明细表。"""
    _isolated(tmp_path, monkeypatch)
    df = _df()
    ev = [_ev(10), _ev(90)]
    sa.record_signals(df, "sh600104", "600104", 240, 100,
                      events=ev, vsa_signals=[], name="测试")
    out = tmp_path / "wx_signal_review.md"
    p = sa.export_review_report(path=str(out), days=2000)
    txt = out.read_text(encoding="utf-8")
    assert os.path.exists(p)
    assert "威科夫信号复盘周报" in txt
    assert "类型胜率" in txt
    assert "信号明细" in txt
    assert "600104" in txt
    # HTML 版本
    out2 = tmp_path / "wx_signal_review.html"
    sa.export_review_report(path=str(out2), days=2000, markdown=False)
    html = out2.read_text(encoding="utf-8")
    assert "<html" in html and "威科夫信号复盘周报" in html
