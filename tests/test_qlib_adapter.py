"""Qlib 适配层 (wyckoff/qlib_adapter.py) 回归测试。

核心: qlib 0.9.7 的 D.features 存在 inst_processors 参数错位 bug,
adapter 通过 DatasetD.dataset 绕过。测试覆盖:
- 无 qlib 时的降级行为 (不抛 ImportError)
- qlib 可用时 default 因子计算、Alpha158 全量 158 因子
- 模型保存/加载 roundtrip
- 缺模型时 qlib_signal_probability 的经验降级
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from wyckoff import qlib_adapter


def _require_qlib():
    if not qlib_adapter._is_qlib_available():
        pytest.skip("qlib not installed")


@pytest.fixture(autouse=True)
def _isolate_data(tmp_path, monkeypatch):
    """把模型文件/缓存重定向到临时目录, 避免触碰真实用户数据。"""
    monkeypatch.setattr(qlib_adapter, "QLIB_MODEL_FILE", str(tmp_path / "qlib_lgbm.joblib"))
    monkeypatch.setattr(qlib_adapter, "_qlib_features_cache", {})
    monkeypatch.setattr(qlib_adapter, "_qlib_available", None)
    yield


def test_is_qlib_available_returns_bool():
    assert isinstance(qlib_adapter._is_qlib_available(), bool)


def test_fetch_qlib_features_synthetic_without_qlib(monkeypatch):
    monkeypatch.setattr(qlib_adapter, "_is_qlib_available", lambda: False)
    feats = qlib_adapter.fetch_qlib_features("sh600015", "2026-01-01", "2026-09-11")
    assert set(feats) == {"momentum_10", "momentum_20", "mean_reversion_30", "volatility_10"}


def test_fetch_qlib_features_real():
    _require_qlib()
    feats = qlib_adapter.fetch_qlib_features("sh600015", "2026-01-01", "2026-09-11")
    assert set(feats) == {"momentum_10", "momentum_20", "mean_reversion_30", "volatility_10"}
    for v in feats.values():
        assert isinstance(v, float)


def test_fetch_alpha158_returns_158_columns():
    _require_qlib()
    df, names = qlib_adapter.fetch_alpha158_features("sh600015", "2025-06-01", "2026-07-03")
    assert len(names) == 158
    assert df is not None and len(df) > 100


def test_train_and_load_model_roundtrip(tmp_path):
    _require_qlib()
    import joblib

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qlib_adapter, "QLIB_MODEL_FILE", str(tmp_path / "m.joblib"))
    res = qlib_adapter.train_qlib_lgbm(
        symbols=["sh600015", "sh600036"],
        start_date="2023-01-01",
        end_date="2026-07-03",
        horizon=5,
        save=False,
    )
    assert res is not None
    assert set(res) >= {"model", "feature_names", "auc", "acc", "n_train", "n_valid"}
    assert len(res["feature_names"]) > 100

    loaded = qlib_adapter._load_qlib_model()
    assert loaded is None  # save=False 不应落盘
    loaded_obj = joblib.load(qlib_adapter.QLIB_MODEL_FILE) if os.path.exists(qlib_adapter.QLIB_MODEL_FILE) else None
    assert loaded_obj is None


def test_model_inference_uses_trained_model(tmp_path):
    _require_qlib()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qlib_adapter, "QLIB_MODEL_FILE", str(tmp_path / "m.joblib"))
    res = qlib_adapter.train_qlib_lgbm(
        symbols=["sh600015", "sh600036"],
        start_date="2023-01-01",
        end_date="2026-07-03",
        horizon=5,
        save=True,
    )
    assert res is not None

    probs = qlib_adapter.qlib_signal_probability("sh600015")
    assert 0.0 < probs["prob_buy"] < 1.0
    assert 0.0 < probs["prob_sell"] < 1.0


def test_signal_probability_synthetic_without_qlib(monkeypatch):
    monkeypatch.setattr(qlib_adapter, "_is_qlib_available", lambda: False)
    probs = qlib_adapter.qlib_signal_probability("sh600015")
    assert "prob_buy" in probs and "prob_sell" in probs and "confidence" in probs
    assert 0.0 <= probs["prob_buy"] <= 1.0
    assert 0.0 <= probs["prob_sell"] <= 1.0


def test_default_calibration_shape():
    cal = qlib_adapter._default_calibration()
    assert set(cal) == {"min_rr", "stop_pct", "confidence_cutoff"}


def test_alpha158_feature_config_parses():
    _require_qlib()
    from qlib.contrib.data.loader import Alpha158DL

    fields, names = Alpha158DL.get_feature_config()
    assert len(fields) == len(names)
    for n in names[:10]:
        assert isinstance(n, str) and n
