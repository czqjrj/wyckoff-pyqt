"""状态分机械规则回测单元测试。"""

import numpy as np
import pandas as pd

from wyckoff import ashare_backtest as ab


def _frame(n=320, drift=0.0008, pattern=None):
    close = 100 * np.exp(np.cumsum(np.full(n, drift)))
    day = pd.date_range("2024-01-01", periods=n, freq="B")
    if pattern == "alternate":
        score = np.where(np.arange(n) % 2 == 0, 0.0, 80.0)
    else:
        score = np.full(n, 80.0)
    return pd.DataFrame(
        {
            "day": day,
            "close": close,
            "ma200": pd.Series(close).rolling(200, min_periods=120).mean(),
            "regime_score": score,
        }
    )


def test_exposure_from_score():
    assert ab.exposure_from_score(80) == 0.8
    assert ab.exposure_from_score(60) == 0.6
    assert ab.exposure_from_score(40) == 0.4
    assert ab.exposure_from_score(25) == 0.2
    assert ab.exposure_from_score(10) == 0.0
    assert ab.exposure_from_score(None) == 0.0
    assert ab.exposure_from_score(float("nan")) == 0.0


def test_overlay_exposure():
    assert ab.overlay_exposure(80, False) == 0.0
    assert ab.overlay_exposure(80, True) == 1.0
    assert ab.overlay_exposure(40, True) == 0.8
    assert ab.overlay_exposure(25, True) == 0.5
    assert ab.overlay_exposure(10, True) == 0.2
    assert ab.overlay_exposure(None, True) == 0.2


def test_backtest_metrics_and_baseline():
    f = _frame()
    out = ab.backtest_state(f, cost_bps=5)
    assert out["ok"] is True
    assert out["strategy"]["avg_exposure"] > 79
    assert out["buy_hold"]["avg_exposure"] > 99
    assert out["strategy"]["max_drawdown"] <= 0
    assert out["buy_hold"]["total_return"] > out["strategy"]["total_return"]
    assert out["ma200"]["total_return"] is not None
    assert out["overlay"]["avg_exposure"] > 0
    assert out["yearly"]
    assert out["rolling"]["n"] >= 1
    assert out["rolling"]["win_rate"] is not None
    assert len(out["equity"]["dates"]) == len(f)
    assert out["equity"]["strategy"][0] == 1.0
    assert out["equity"]["overlay"][0] == 1.0


def test_cost_reduces_return():
    f = _frame(pattern="alternate")
    cheap = ab.backtest_state(f, cost_bps=0)
    expensive = ab.backtest_state(f, cost_bps=50)
    assert cheap["strategy"]["total_return"] > expensive["strategy"]["total_return"]
    assert expensive["strategy"]["trades"] > 0


def test_backtest_insufficient_data():
    f = _frame(n=30)
    out = ab.backtest_state(f)
    assert out["ok"] is False


def test_score_components_minimal_frame():
    assert ab.score_components(_frame()) is None


def _full_frame(n=400):
    import numpy as np

    from wyckoff import ashare_outlook as ao
    from wyckoff.indicators import add_indicators

    rng = np.random.default_rng(3)
    ret = 0.0008 + rng.normal(0, 0.01, n)
    close = 100 * np.exp(np.cumsum(ret))
    day = pd.date_range("2023-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "day": day,
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_500_000.0),
        }
    )
    df = add_indicators(df, symbol="sh000001")
    f = ao.compute_features(df)
    f["regime_score"] = ao.regime_score_series(f)
    return f


def test_backtest_ablation_and_oos():
    f = _full_frame()
    out = ab.backtest_state(f)
    assert out["ok"] is True
    assert out["oos"] is not None
    assert out["oos"]["days"] == ab.OOS_DAYS
    assert out["oos"]["strategy"]["n"] == ab.OOS_DAYS
    assert out["ablation"] is not None
    names = [r["name"] for r in out["ablation"]["rows"]]
    assert "趋势" in names and "v2 配方" in names
    for r in out["ablation"]["rows"]:
        assert r["ic"] is None or -1.0 <= r["ic"] <= 1.0
        assert r["bt_total"] is not None
        assert r["bt_oos"] is not None
    wf = out["walkforward"]
    assert wf["ic"]["n"] >= 1
    assert 0 <= wf["ic"]["positive_rate"] <= 100
    assert wf["yearly"]["n_years"] >= 1
    assert 0 <= wf["yearly"]["beat_ma200"] <= 100


def test_render_backtest_ablation_oos():
    text = ab.render_backtest(ab.backtest_state(_full_frame()))
    assert "样本外" in text
    assert "成分消融" in text
    assert "v2 配方" in text
    assert "Walk-forward 稳定性" in text
    assert "逐年跑赢 MA200" in text


def test_render_backtest():
    text = ab.render_backtest(ab.backtest_state(_frame()))
    assert "状态分机械规则回测" in text
    assert "MA200" in text
    assert "MA200+状态分" in text
    assert "滚动窗口对比" in text
    assert "买入持有" in text
