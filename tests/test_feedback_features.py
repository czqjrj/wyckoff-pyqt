# -*- coding: utf-8 -*-
"""build_feedback_record 四类阶段带统一特征落库测试。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.storage import build_feedback_record


def _df(n=80):
    t = np.arange(n)
    close = 10 + t * 0.02
    lo = close - 0.3 - (t % 7) * 0.01
    hi = close + 0.3 + ((t + 3) % 5) * 0.01
    day = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"day": day, "close": close, "low": lo, "high": hi})


def test_all_segment_kinds_get_features():
    df = _df()
    for key in ("accumulation", "distribution", "markup", "markdown"):
        rec = build_feedback_record("sh600000", 80, 240, df, 10, 60,
                                    "k-" + key, "标签")
        f = rec["features"]
        for k in ("lo1", "lo2", "hi1", "hi2"):
            assert k in f, f"{key} 缺 {k}"
        assert f["lo1"] <= f["lo2"] + 1e-9
        assert abs(f["low_defense"] - round(f["lo2"] / f["lo1"], 4)) < 1e-6


def test_single_bar_segment_fallback():
    df = _df()
    rec = build_feedback_record("sh600000", 80, 240, df, 40, 40, "k", "x")
    f = rec["features"]
    assert f["lo1"] == pytest_approx(f["lo2"])
    assert f["hi1"] == pytest_approx(f["hi2"])


def pytest_approx(v):
    return v
