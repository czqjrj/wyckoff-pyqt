"""多周期共振入场门 (entries.ENTRY_HTF_GATE / multitime.htf_direction) 专项测试。

验证: 周/月线偏空 (htf=-1) 反向过滤日线多头入场; 偏多/中性分别放行;
fail-open/fail-close 两档行为一致; 融合侧 _htf_direction 与单源收敛。
全部离线: 显式传 events, 打桩 online_model, 不走网络。
"""
import numpy as np
import pandas as pd
import pytest


def _mk_df(closes):
    n = len(closes)
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    c = np.array(closes, dtype=float)
    df = pd.DataFrame({"day": dates, "open": c, "close": c,
                       "high": c + 0.05, "low": c - 0.05,
                       "volume": np.full(n, 8e5), "atr": np.full(n, 0.3)})
    return df


@pytest.fixture
def spring_df():
    return _mk_df(np.r_[np.full(110, 10.0), 10.0, 10.0, 10.0])


@pytest.fixture
def spring_event(spring_df):
    n = len(spring_df)
    return [{"type": "Spring", "conf": 85, "idx": n - 2,
             "avail_idx": n - 1, "price": 9.2,
             "date": str(spring_df["day"].iloc[n - 1])}]


@pytest.fixture(autouse=True)
def _offline_model(monkeypatch):
    """打桩 online_model (若存在), 防止读到本机真实模型文件影响 tier 判定。"""
    import wyckoff.online_model as om
    monkeypatch.setattr(om, "reliability", lambda e: 0.9, raising=False)
    monkeypatch.setattr(om, "reliability_tier", lambda rel: "high", raising=False)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    """生产默认 ENTRY_HTF_GATE=False (survey 证伪后闲置); 测试强制开启观察行为。"""
    import wyckoff.entries as e
    monkeypatch.setattr(e, "ENTRY_HTF_GATE", True)
    monkeypatch.setattr(e, "AUTO_RECORD_ENTRIES", False)


@pytest.fixture
def ent(monkeypatch):
    import wyckoff.entries as e
    monkeypatch.setattr(e, "AUTO_RECORD_ENTRIES", False)
    return e


def _find(ent, df, evs, htf=None, **kw):
    return ent.find_entry_signals(df, events=evs, htf=htf,
                                  mkt_trend_20=1.0,
                                  sector_strength_pct_val=0.8,
                                  fund_net_pct_val=0.9, **kw)


def test_htf_direction_single_source():
    """multitime.htf_direction 是唯一实现, 融合侧委派同源。"""
    from wyckoff.fusion import _htf_direction as fdir
    from wyckoff.multitime import htf_direction as mdir
    assert mdir({"weekly_phase": "上升趋势 (Markup)"}) == 1
    assert mdir({"weekly_phase": "下跌趋势 (Markdown)"}) == -1
    assert mdir({"weekly_phase": "区间整理"}) == 0
    assert mdir(None) == 0 and mdir({}) == 0
    assert mdir({"weekly_phase": "下跌趋势 (Markdown)",
                 "monthly_phase": "上升趋势 (Markup)"}) == 0
    assert mdir({"weekly_phase": "底部整固 (Accumulation)",
                 "monthly_phase": "上升趋势 (Markup)"}) == 1
    assert fdir({"weekly_phase": "下跌趋势 (Markdown)"}) == -1


def test_htf_bear_blocks_entry(ent, spring_df, spring_event):
    """周/月线偏空 (htf=-1) → 日线 Spring 入场点被反向过滤。"""
    import wyckoff.entries as e
    assert _find(e, spring_df, spring_event, htf=-1) == []


def test_htf_bull_passes_entry(ent, spring_df, spring_event):
    """周/月线偏多 (htf=+1) → 入场点放行且带 htf 字段。"""
    import wyckoff.entries as e
    rows = _find(e, spring_df, spring_event, htf=1)
    assert len(rows) == 1
    assert rows[0]["type"] == "Spring"
    assert rows[0]["htf"] == 1


def test_htf_neutral_fail_open_auth(ent, spring_df, spring_event):
    """htf=0 (中性) fail-open 放行 (默认), fail-close (ENTRY_HTF_FAIL_OPEN=False) 拦截。"""
    import wyckoff.entries as e
    assert len(_find(e, spring_df, spring_event, htf=0)) == 1
    old = e.ENTRY_HTF_FAIL_OPEN
    try:
        e.ENTRY_HTF_FAIL_OPEN = False
        assert _find(e, spring_df, spring_event, htf=0) == []
    finally:
        e.ENTRY_HTF_FAIL_OPEN = old


def test_htf_none_means_gate_off(ent, spring_df, spring_event):
    """htf=None 表示未启用本门 (调用方未提供), 不影响既有行为。"""
    import wyckoff.entries as e
    assert len(_find(e, spring_df, spring_event, htf=None)) == 1


def test_htf_gate_switch_off(ent, spring_df, spring_event):
    """ENTRY_HTF_GATE=False (生产默认) 时即便 htf=-1 也不拦截。"""
    import wyckoff.entries as e
    e.ENTRY_HTF_GATE = False
    assert len(_find(e, spring_df, spring_event, htf=-1)) == 1
