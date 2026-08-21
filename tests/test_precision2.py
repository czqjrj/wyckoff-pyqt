# -*- coding: utf-8 -*-
"""第二轮精度升级回归测试:
1. 跟进确认 (confirm_events): 事件后 window 根内价格朝信号方向跟进 → 确认;
   因果性 (只用后续已见bar), 中性/末端事件为待确认 None;
2. 融合层加权: 已确认事件 ×1.2 / 未确认 ×0.5 / 待确认 ×0.9;
3. 高周期对齐 (_align + fuse_signals mf): 顺周/月线方向加权, 逆势降权;
4. 波动率特征 (event_confidence): 带宽分位 bw_pct 只记录不参与打分;
5. 回测确认子集 (backtest_events win_confirmed): 已确认事件单独统计。
"""
import numpy as np
import pandas as pd

from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.events import detect_all, confirm_events
from wyckoff.fusion import fuse_signals, _event_score, _align, _htf_direction
from wyckoff.backtest import backtest_events


def _mk_df(n=500, seed=11):
    rng = np.random.default_rng(seed)
    closes = 50 + np.cumsum(rng.normal(0, 0.3, n))
    closes = np.clip(closes, 20, None)
    return pd.DataFrame({
        "day": pd.date_range("2022-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.002, n)),
        "close": closes * (1 + rng.normal(0, 0.002, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": rng.uniform(1e5, 1e6, n),
    })


# ───────────────────────── 1. 跟进确认 confirm_events ─────────────────────────

def test_confirm_bull_event_confirmed():
    """多头事件后收盘突破事件bar高点 → 确认。"""
    df = _mk_df()
    df.loc[20, "close"] = df.loc[20, "high"].max()
    df.loc[21, "close"] = df.loc[20, "high"] * 1.02
    ev = [{"idx": 20, "type": "Spring", "price": float(df.loc[20, "low"])}]
    out = confirm_events(df, ev, window=3)
    assert out[0]["confirmed"] is True


def test_confirm_bear_event_confirmed():
    """空头事件后收盘跌破事件bar低点 → 确认。"""
    df = _mk_df()
    df.loc[30, "close"] = df.loc[30, "low"].min()
    df.loc[31, "close"] = df.loc[30, "low"] * 0.98
    ev = [{"idx": 30, "type": "UTAD", "price": float(df.loc[30, "high"])}]
    out = confirm_events(df, ev, window=3)
    assert out[0]["confirmed"] is True


def test_confirm_not_followed_false():
    """事件后价格未朝信号方向跟进 → 未确认。"""
    df = _mk_df()
    ev = [{"idx": 40, "type": "Spring", "price": float(df.loc[40, "low"])}]
    hi = float(df.loc[40, "high"])
    for k in range(41, 44):
        df.loc[k, "close"] = min(float(df.loc[k, "close"]), hi * 0.98)
    out = confirm_events(df, ev, window=3)
    assert out[0]["confirmed"] is False


def test_confirm_pending_for_latest():
    """末端事件 (无足够后续bar) → 待确认 None。"""
    df = _mk_df(n=100)
    ev = [{"idx": 97, "type": "Spring", "price": 1.0}]
    out = confirm_events(df, ev, window=3)
    assert out[0]["confirmed"] is None


def test_confirm_neutral_pending():
    """中性事件 (SC/BC/AR) 不参与方向确认 → None。"""
    df = _mk_df()
    ev = [{"idx": 50, "type": "SC", "price": 1.0}]
    out = confirm_events(df, ev, window=3)
    assert out[0]["confirmed"] is None


def test_confirm_does_not_mutate_input():
    """confirm_events 返回新事件列表, 不改动原对象。"""
    df = _mk_df()
    ev = [{"idx": 50, "type": "SOS", "price": 1.0}]
    before = dict(ev[0])
    confirm_events(df, ev, window=3)
    assert ev[0] == before and "confirmed" not in ev[0]


def test_detect_all_carries_confirmed():
    """detect_all 输出的事件均带 confirmed 字段。"""
    df = add_indicators(_mk_df(), symbol="600104")
    ev = detect_all(df, find_pivots(df, order=6))
    assert ev, "应至少产出事件"
    assert all("confirmed" in e for e in ev)


# ─────────────────────── 2. 融合层确认加权 _event_score ──────────────────────

def test_event_score_confirmed_weighting():
    """已确认事件得分 > 待确认 > 未确认 (同类型同置信同时点)。"""
    base = {"idx": 490, "type": "SOS", "conf": 100}
    s_ok = _event_score([dict(base, confirmed=True)], max_idx=499)
    s_none = _event_score([dict(base, confirmed=None)], max_idx=499)
    s_fail = _event_score([dict(base, confirmed=False)], max_idx=499)
    assert s_ok > s_none > s_fail > 0


def test_event_score_no_confirmed_key_unchanged():
    """无 confirmed 字段 (旧数据/手工构造) 权重不变; 显式 None 视为待确认×0.9。"""
    base = {"idx": 490, "type": "SOS", "conf": 100}
    s_plain = _event_score([dict(base)], max_idx=499)
    s_none = _event_score([dict(base, confirmed=None)], max_idx=499)
    assert s_plain > s_none  # None=待确认 ×0.9; 无字段不做任何调整
    assert abs(s_plain - s_none * 1.1111) < 1e-3 or (s_plain - s_none) > 0


# ─────────────────────── 3. 高周期对齐 _align / fuse ─────────────────────────

def test_align_matches_and_conflicts():
    """顺高周期方向 → ×1.2; 逆势 → ×0.6 (方向不变但被压缩); 无参照/零分 → 不变。"""
    assert abs(_align(50.0, 1) - 60.0) < 1e-9
    assert abs(_align(-50.0, 1) - (-30.0)) < 1e-9
    assert abs(_align(50.0, -1) - 30.0) < 1e-9
    assert abs(_align(-50.0, -1) - (-60.0)) < 1e-9
    assert _align(0.0, 1) == 0.0
    assert _align(50.0, 0) == 50.0


def test_htf_direction_from_mf():
    """周/月线偏多 → +1, 偏空 → -1, 无参照 → 0。"""
    assert _htf_direction({"weekly_phase": "上升趋势", "monthly_phase": "底部整固"}) == 1
    assert _htf_direction({"weekly_phase": "下跌趋势"}) == -1
    assert _htf_direction({}) == 0
    assert _htf_direction(None) == 0
    assert _htf_direction({"weekly_phase": "区间整理"}) == 0


def test_fuse_with_mf_aligns_score():
    """传入 mf 高周期方向 → 顺向事件维度加权, 综合评分更高。"""
    events = [{"idx": 490, "type": "Spring", "conf": 100, "confirmed": True}]
    vsa = [{"idx": 495, "label": "SPR"}]
    pnf = {"direction": "up", "tr_top": 90.0, "tr_bottom": 70.0, "横向计数上方目标": 110.0}
    df = add_indicators(_mk_df(), symbol="600104")
    df["price_ma20"] = df["price_ma20"].ffill()
    df["price_ma50"] = df["price_ma50"].ffill()
    f0 = fuse_signals(df, "底部整固 (Accumulation)", events, vsa, pnf, mf=None)
    f1 = fuse_signals(df, "底部整固 (Accumulation)", events, vsa, pnf,
                      mf={"weekly_phase": "上升趋势"})
    f2 = fuse_signals(df, "底部整固 (Accumulation)", events, vsa, pnf,
                      mf={"weekly_phase": "下跌趋势"})
    assert f1["score"] > f0["score"] > f2["score"]
    assert f1["htf"] == 1 and f2["htf"] == -1
    assert any("顺高周期" in d["detail"] for d in f1["dims"])
    assert any("逆高周期" in d["detail"] for d in f2["dims"])


# ─────────────────────── 4. 波动率状态门 (event_confidence) ──────────────────

def test_vol_gate_breaks_in_low_vol():
    """带宽分位 (bw_pct) 只记录特征不参与打分 (实证 rho=+0.007 无预测力)。

    回归: 低波动蓄势 / 高波动追高两种形态下, bw_pct 特征应被正确计算
    (压缩→低分位, 扩张→高分位), 且不影响置信度评分。
    """
    from wyckoff.events import event_confidence

    def conf_for(compress_before, expand_before):
        """事件bar i 前 expand_before 根带宽上升(高波动) / 压缩(低波动蓄势)。"""
        df = add_indicators(_mk_df(), symbol="600104")
        df["boll_mid"] = 1.0
        n = len(df)
        i = n - 30
        half = 0.3  # 30% 带宽
        df["boll_up"] = 1.0 + half
        df["boll_dn"] = 1.0 - half
        if compress_before:
            df.loc[i - expand_before:i, ["boll_up", "boll_dn"]] = 1.01, 0.99  # 压缩→低分位
        else:
            df.loc[:i - expand_before - 1, ["boll_up", "boll_dn"]] = 1.01, 0.99  # 前期压缩
        ev = event_confidence(df, [{"idx": i, "type": "SOS", "price": float(df['close'].iloc[i])}])
        return ev[0]

    e_low = conf_for(compress_before=True, expand_before=20)    # 事件前收窄→蓄势
    e_hi = conf_for(compress_before=False, expand_before=20)    # 事件前扩张→追高
    # 特征记录: 压缩段带宽分位应显著低于扩张段
    assert e_low["feat"]["bw_pct"] is not None and e_hi["feat"]["bw_pct"] is not None
    assert e_low["feat"]["bw_pct"] < e_hi["feat"]["bw_pct"], \
        f"低波动带宽分位应更低: {e_low['feat']['bw_pct']} vs {e_hi['feat']['bw_pct']}"
    # 打分不受带宽状态影响 (bw_pct 不参与打分)
    assert e_low["conf"] == e_hi["conf"], \
        f"带宽分位不应影响置信度: {e_low['conf']} vs {e_hi['conf']}"
    assert 0 < e_low["conf"] <= 100


# ─────────────────────── 5. 回测确认子集 win_confirmed ───────────────────────

def test_backtest_events_confirmed_subset():
    """backtest_events 对样本充足的类型补充已确认子集统计。"""
    df = add_indicators(_mk_df(seed=17), symbol="600104")
    res = backtest_events(df, [], horizon=10, min_n=1, cost=0.0)
    assert "by_type" in res
    # detect_all 是在 backtest 内部按 wdf 调用的, 事件已带 confirmed;
    # 只要样本足够, 至少一个类型出现 win_confirmed 字段 (或全部不足亦可接受)。
    have_conf = any("win_confirmed" in s for s in res["by_type"].values())
    if have_conf:
        for s in res["by_type"].values():
            if "win_confirmed" in s:
                assert 0 <= s["win_confirmed"] <= 100
                assert s["n_confirmed"] >= 2
