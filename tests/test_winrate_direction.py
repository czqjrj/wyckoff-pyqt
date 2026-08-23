"""信号胜率方向化测试: 标称空头信号 (UTAD/LPSY/SUP 等) 以"下跌"为命中。

- vsa_dir / event_dir 方向语义;
- load_win_rates 按方向命中汇总 (空头信号上涨占比低 → 方向命中率高);
- 中性信号保留上涨占比口径。
"""
import pytest

from wyckoff.config import EVENT_BEAR, EVENT_BULL, VSA_BEAR, VSA_BULL, event_dir, vsa_dir


def test_vsa_dir_semantics():
    assert vsa_dir("SUP") == -1       # 强势供给 → 看空
    assert vsa_dir("ND") == -1        # 无需求 → 看空
    assert vsa_dir("UPT") == -1       # 上冲量(诱多) → 看空
    assert vsa_dir("BC") == -1        # 买入高潮 → 看空
    assert vsa_dir("SV") == 1         # 停止量 → 看多
    assert vsa_dir("SC") == 1         # 卖出高潮 → 看多
    assert vsa_dir("DEM") == 1        # 强势需求 → 看多
    assert vsa_dir("NS") == 1         # 无供给 → 看多
    assert vsa_dir("CHOC") == 0       # 性质变化 → 中性
    assert vsa_dir("ABS") == 0        # 吸收 → 中性
    assert not (VSA_BULL & VSA_BEAR)


def test_event_dir_semantics():
    assert event_dir("Spring") == 1
    assert event_dir("Shakeout") == 1
    assert event_dir("UTAD") == -1
    assert event_dir("LPSY") == -1
    assert event_dir("SOW") == -1
    assert event_dir("SC") == 0
    assert not (EVENT_BULL & EVENT_BEAR)


def test_winrate_bear_signal_down_is_hit(monkeypatch):
    """空头信号下跌应计为命中; 需在调用前失效缓存。"""
    from wyckoff import signal_accuracy as sa
    recs = [
        dict(kind="event", type="UTAD", results={"20": {"ret": -0.05}}),
        dict(kind="event", type="UTAD", results={"20": {"ret": -0.08}}),
        dict(kind="event", type="UTAD", results={"20": {"ret": +0.03}}),   # 空头下涨 → 未中
        dict(kind="event", type="Spring", results={"20": {"ret": +0.10}}),
        dict(kind="event", type="Spring", results={"20": {"ret": +0.04}}),
        dict(kind="event", type="Spring", results={"20": {"ret": -0.02}}),  # 多头下跌 → 未中
        dict(kind="vsa", type="SUP", results={"20": {"ret": -0.04}}),
        dict(kind="vsa", type="SUP", results={"20": {"ret": -0.07}}),
        dict(kind="vsa", type="SUP", results={"20": {"ret": +0.02}}),       # 标空上涨 → 未中
        dict(kind="vsa", type="ND", results={"20": {"ret": -0.06}}),
        dict(kind="vsa", type="ND", results={"20": {"ret": -0.03}}),
        dict(kind="vsa", type="ND", results={"20": {"ret": +0.01}}),
        dict(kind="vsa", type="SV", results={"20": {"ret": +0.03}}),
        dict(kind="vsa", type="SV", results={"20": {"ret": +0.05}}),
        dict(kind="vsa", type="SV", results={"20": {"ret": +0.02}}),
        dict(kind="vsa", type="CHOC", results={"20": {"ret": -0.01}}),      # 中性 → 上涨口径(未中)
        dict(kind="vsa", type="CHOC", results={"20": {"ret": +0.02}}),      # 中性 → 上涨口径(中)
        dict(kind="vsa", type="CHOC", results={"20": {"ret": +0.04}}),      # 中性 → 上涨口径(中)
    ]
    monkeypatch.setattr("wyckoff.signal_accuracy.load_signals", lambda: recs)
    sa.invalidate_win_rate_cache()
    rates = sa.load_win_rates(20, force=True)
    # UTAD: 3条中2条下跌 → 方向命中 2/3
    util = rates[sa._winrate_key("event", "UTAD")]
    assert util["n"] == 3 and util["win"] == pytest.approx(2 / 3, abs=5e-4)
    # SUP: 3条中2条下跌 → 2/3
    sup = rates[sa._winrate_key("vsa", "SUP")]
    assert sup["n"] == 3 and sup["win"] == pytest.approx(2 / 3, abs=5e-4)
    # ND: 3条中2条下跌 → 2/3
    assert rates[sa._winrate_key("vsa", "ND")]["win"] == pytest.approx(2 / 3, abs=5e-4)
    # SV 多头: 全涨 → 1.0
    assert rates[sa._winrate_key("vsa", "SV")]["win"] == 1.0
    # 中性 CHOC: 使用上涨占比 (2/3), 与方向化无关
    assert rates[sa._winrate_key("vsa", "CHOC")]["win"] == pytest.approx(2 / 3, abs=5e-4)
    # Spring 多头: 2涨1跌 → 2/3
    assert rates[sa._winrate_key("event", "Spring")]["win"] == pytest.approx(2 / 3, abs=5e-4)


def test_winrate_real_db_direction_consistency():
    """真实库方向命中率与原始上涨占比的方向语义一致 (UTAD 应显著>60%)。"""
    from wyckoff import signal_accuracy as sa
    rates = sa.load_win_rates(20, force=True)
    utad = rates.get(sa._winrate_key("event", "UTAD"))
    if utad and utad["n"] >= 20:
        # 空头信号 UTAD: 方向命中(下跌)应远高于其原始上涨占比的"好"阈值,
        # 也即收缩值应显著 > 0.6 (若原始上涨占比 20% → 反向后 ~80%)
        assert utad["win"] > 0.6, utad
        assert utad["shrunk"] > 0.6, utad


def test_bootstrap_ci_direction_aware():
    """bootstrap_winrate_ci 方向化: 空头信号下跌记命中。"""
    from wyckoff.validation import bootstrap_winrate_ci, winrate_ci_table
    rets = [0.05, 0.08, 0.03]           # 全涨
    assert bootstrap_winrate_ci(rets)["win"] == 100.0      # 默认多头口径
    assert bootstrap_winrate_ci(rets, direction=-1)["win"] == 0.0  # 标空: 全未中
    rets2 = [-0.05, -0.08, -0.03]       # 全跌
    assert bootstrap_winrate_ci(rets2, direction=-1)["win"] == 100.0  # 标空: 全命中
    # 表接口按类型方向自动取
    recs = [dict(kind="vsa", type="SUP", results={"20": {"ret": v}})
            for v in (-0.05, -0.08, -0.03)]
    t = winrate_ci_table(recs, kind="vsa", min_n=3)
    assert t["types"]["SUP"]["win"] == 100.0  # 标空 + 全跌 → 全中


def test_oos_direction_aware(monkeypatch):
    """win_rate_of_oos 空头信号以跌为命中 (不依赖 monkeypatch 时也方向化)。"""
    import pandas as pd

    from wyckoff.validation import win_rate_of_oos
    recs = [dict(kind="event", type="UTAD", date=d, results={"20": {"ret": r}})
            for d, r in (("2024-01-05", -0.05), ("2024-01-10", -0.08),
                         ("2024-01-15", +0.03))]
    w = win_rate_of_oos(recs, "event", "UTAD", pd.Timestamp("2024-06-01"),
                        min_n=1, baseline=0.5)
    assert abs(w - 2 / 3) < 1e-9   # 跌2/涨1 → 方向命中 2/3


def test_fusion_weight_uses_directional_win(monkeypatch):
    """_winrate_weight 直接用方向化胜率加权: 空头命中率高 → 权重>1 (不再双重反转)。"""
    from wyckoff.fusion import _winrate_weight

    def fake_win(kind, type_, horizon=20, baseline=0.5):
        return 0.85          # 高方向命中
    monkeypatch.setattr("wyckoff.signal_accuracy.win_rate_of", fake_win)
    wb = _winrate_weight("vsa", "ND", direction=-1, before_ts=None)
    assert wb >= 1.5, wb     # 空头高命中 → 强提权 (旧逻辑 0.5-0.85<0 → 会降权到0.5)

    def fake_win_low(kind, type_, horizon=20, baseline=0.5):
        return 0.30          # 低方向命中
    monkeypatch.setattr("wyckoff.signal_accuracy.win_rate_of", fake_win_low)
    wl = _winrate_weight("event", "UTAD", direction=-1, before_ts=None)
    assert wl <= 0.5, wl     # 空头低命中 → 降权
