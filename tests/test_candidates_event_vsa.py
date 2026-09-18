"""策略2「事件 + 高价值VSA 双因共振」候选 (scan_individual 兜底赛道) 专项测试。

验证 event_vsa_candidate 的共时窗口/VR 门/标签白名单/事件 conf 过滤, 以及
scan_individual 优先序下 (纪律→左侧→事件+VSA) 双因策略作为兜底正确命中。
全部离线: monkeypatch detect_all / find_pivots / latest_buy_points / vsa_classify。
"""
import numpy as np
import pandas as pd

import wyckoff.strategies.candidates as cands


def _df(n=400):
    base = np.linspace(10.0, 11.0, n)
    return pd.DataFrame({
        "day": pd.bdate_range("2025-06-02", periods=n),
        "open": base, "high": base + 0.05, "low": base - 0.05,
        "close": base, "volume": np.full(n, 8e5),
    })


def _ev(idx=396, typ="Spring", conf=88):
    return {"type": typ, "idx": idx, "conf": conf}


def _vsa(idx=397, label="SPR", vr=1.8):
    return {"idx": idx, "date": None, "label": label, "color": "#x",
            "desc": f"量{vr:.1f}x", "features": {"vr": vr}}


def test_event_vsa_hit(monkeypatch):
    """强多头事件 conf 达标 + 共时窗口内高价值 VSA → 双因候选。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(397, "SPR", 1.8)])
    df = _df()
    cand = cands.event_vsa_candidate("sh600001", df, [_ev(396, "Spring", 88)],
                                     [], name="")
    assert cand is not None
    assert cand["strategy"] == cands.STRATEGY_EVENT_VSA
    assert cand["type"] == "Spring" and cand["vsa"] == "SPR"
    assert cand["conf"] == 88


def test_event_vsa_vr_gate(monkeypatch):
    """VSA 量比不达标 (vr < EVENT_VSA_MIN_VR) → 不作确认, 无候选。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(381, "SPR", 1.1)])
    df = _df()
    assert cands.event_vsa_candidate("sh600001", df, [_ev(396, "Spring", 88)],
                                     []) is None


def test_event_vsa_label_whitelist(monkeypatch):
    """非高价值标签 (如噪声型 ND) 不构成共振确认。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(381, "ND", 2.0)])
    df = _df()
    assert cands.event_vsa_candidate("sh600001", df, [_ev(396, "Spring", 88)],
                                     []) is None


def test_event_vsa_co_window(monkeypatch):
    """VSA 与事件相距超过共时窗口 → 分属不同行情, 不作共振。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(420, "SPR", 1.8)])
    df = _df()
    assert cands.event_vsa_candidate("sh600001", df, [_ev(396, "Spring", 88)],
                                     []) is None


def test_event_vsa_conf_gate(monkeypatch):
    """事件 conf 低于 EVENT_VSA_MIN_CONF → 无候选。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(397, "SPR", 1.8)])
    df = _df()
    assert cands.event_vsa_candidate("sh600001", df, [_ev(396, "Spring", 80)],
                                     []) is None


def test_event_vsa_event_freshness(monkeypatch):
    """事件太旧 (不在近端可买窗口) → 无候选。"""
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(397, "SPR", 1.8)])
    df = _df()
    assert cands.event_vsa_candidate("sh600001", df, [_ev(200, "Spring", 88)],
                                     []) is None


def test_scan_individual_fallback_order(monkeypatch):
    """优先序兜底: 纪律/左侧均空 → 事件+VSA 命中并带 gated=False (独立赛道)。"""
    df = _df()
    fake_evs = [_ev(396, "Spring", 88)]
    monkeypatch.setattr("wyckoff.events.detect_all", lambda df, piv: fake_evs)
    monkeypatch.setattr("wyckoff.indicators.find_pivots",
                        lambda df, order=6: [])
    monkeypatch.setattr("wyckoff.buypoints.latest_buy_points",
                        lambda df, evs, piv: [])
    monkeypatch.setattr("wyckoff.vsa.vsa_classify",
                        lambda df: [_vsa(397, "SPR", 1.8)])
    cand = cands.scan_individual("sh600001", df=df, min_conf=90)
    assert cand is not None
    assert cand["strategy"] == cands.STRATEGY_EVENT_VSA
    assert cand["gated"] is False
    assert cand["vsa"] == "SPR"
