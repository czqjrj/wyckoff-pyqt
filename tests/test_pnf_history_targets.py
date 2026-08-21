# -*- coding: utf-8 -*-
"""点数图历史上涨/下跌测算 (pnf_history_targets) 的回归测试。

验证: 历史各 TR 突破段均产出上/下目标, 且突破后的实际价格走势被正确
核对为 到位(up_hit/down_hit) 或 未到。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.pnf import (build_pnf, pnf_targets, pnf_history_targets, _pnf_zone)


def _df_with_trends():
    """构造含多段 TR 盘整 + 突破的合成行情 (下跌→反弹→再跌)。"""
    np.random.seed(7)
    close = []
    p = 80.0
    for _nbar, _base, drift in [(300, 100, 0.15), (120, 135, 0.1),
                                (200, 100, -0.15), (180, 85, 0.1)]:
        for _ in range(_nbar):
            p += drift + np.random.randn() * 1.2
            close.append(p)
    close = np.array(close)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=len(close)),
        "open": close * 0.999, "close": close,
        "high": close * 1.008, "low": close * 0.992,
        "volume": np.random.rand(len(close)) * 1e6,
    })


def test_history_targets_produced():
    df = _df_with_trends()
    cols, box = build_pnf(df)
    hist = pnf_history_targets(cols, box)
    assert len(hist) >= 1, "应至少产出1段历史测算"
    for h in hist:
        assert "break_col" in h and h["direction"] in ("up", "down")
        assert h["tr_top"] > h["tr_bottom"]
    assert h["break_col"] < len(cols), "突破列必须在图内"
    assert h["zone"] in ("吸筹", "派发")
    assert h["seq"] >= 1
    # 区间语义按突破后的走势结果 (pnf_history_targets 与 _pnf_zone 必须同口径)
    z, _note = _pnf_zone(cols, h["tr_top"], h["tr_bottom"], h["direction"],
                         h["break_col"], box)
    assert h["zone"] == z


def test_tr_column_range_near_break():
    """回归: TR 列序号必须紧邻其突破列 (base 列偏移 bug 曾把 TR 画到
    窗口左移 (len(view)-12) 列处, 与数据无关)。"""
    df = _df_with_trends()
    cols, box = build_pnf(df)
    hist = pnf_history_targets(cols, box)
    # 动态窗口: win_size = max(12, min(len//4, 30))
    win_size = max(12, min(len(cols) // 4, 30))
    for h in hist:
        assert h["break_col"] < len(cols)
        # TR 来自突破列前 win_size 列窗口, 起点不得早于突破列前 (win_size-1) 列
        assert h["tr_start_col"] >= h["break_col"] - (win_size - 1), h
        assert h["tr_end_col"] <= h["break_col"], h
    cur = pnf_targets(df, cols, box)
    if cur:
        assert cur["tr_end_col"] == len(cols) - 1
        assert cur["tr_start_col"] >= len(cols) - win_size


def _cols_from_rows(seq):
    """把 [(type, lo, hi), ...] 直接构造成点数图列 (供 _pnf_zone 单元测试)。"""
    return [{"type": t, "rows": [], "lo": lo, "hi": hi,
             "count": round((hi - lo) / 1.0) + 1} for t, lo, hi in seq]


def test_zone_utad_failed_upbreak():
    """向上突破后快速跌回区间下沿 → UTAD上冲, 实为派发区间。"""
    cols = _cols_from_rows([("X", 21.0, 30.0), ("O", 15.0, 22.0)])
    zone, note = _pnf_zone(cols, 24.0, 20.0, "up", 0, box=1.0)
    assert zone == "派发" and note == "UTAD上冲"
    # 向上突破后仅小幅回踩 (未跌破下沿) → 吸筹
    cols2 = _cols_from_rows([("X", 21.0, 30.0)])
    zone2, _ = _pnf_zone(cols2, 24.0, 20.0, "up", 0, box=1.0)
    assert zone2 == "吸筹"


def test_zone_spring_failed_downbreak():
    """向下破位后快速涨回区间上沿 → Spring弹簧, 实为吸筹区间。"""
    cols = _cols_from_rows([("O", 15.0, 22.0), ("X", 25.0, 30.0)])
    zone, note = _pnf_zone(cols, 24.0, 20.0, "down", 0, box=1.0)
    assert zone == "吸筹" and note == "Spring"
    # 向下破位后延续 → 派发
    cols2 = _cols_from_rows([("O", 15.0, 19.0)])
    zone2, _ = _pnf_zone(cols2, 24.0, 20.0, "down", 0, box=1.0)
    assert zone2 == "派发"


def test_history_hit_flag_consistency():
    """已到位标志必须与后续实际价格一致: up_hit=True ⇔ 后续有列 hi≥up_target。"""
    df = _df_with_trends()
    cols, box = build_pnf(df)
    hist = pnf_history_targets(cols, box)
    for h in hist:
        if h["direction"] == "up" and h.get("up_target") is not None:
            real = any(c["hi"] >= h["up_target"] for c in cols[h["break_col"] + 1:])
            assert bool(h["up_hit"]) == real
        if h["direction"] == "down" and h.get("down_target") is not None:
            real = any(c["lo"] <= h["down_target"] for c in cols[h["break_col"] + 1:])
            assert bool(h["down_hit"]) == real


def test_history_consistent_with_current():
    """历史测算与最新测算共用 _pnf_targets_at 核心, 数据形态一致。"""
    df = _df_with_trends()
    cols, box = build_pnf(df)
    cur = pnf_targets(df, cols, box)
    assert set(["tr_top", "tr_bottom", "tr_width", "direction"]) <= set(cur)
    hist = pnf_history_targets(cols, box)
    assert all(set(["tr_top", "tr_bottom", "direction"]) <= set(h) for h in hist)


def test_short_history_returns_empty():
    """列数不足时历史测算返回空列表, 不崩溃。"""
    np.random.seed(3)
    close = 50 + np.cumsum(np.random.randn(40) * 0.5)
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=len(close)),
        "open": close, "close": close,
        "high": close + 0.3, "low": close - 0.3,
        "volume": np.random.rand(len(close)) * 1e6,
    })
    cols, box = build_pnf(df)
    assert pnf_history_targets(cols, box) == []


def test_plot_pnf_with_history_renders():
    """带 history 绘制点数图不报错 (回归: 标注文本/线条生成)。"""
    import matplotlib
    matplotlib.use("Agg")
    from wyckoff.pnf import plot_pnf
    df = _df_with_trends()
    cols, box = build_pnf(df)
    hist = pnf_history_targets(cols, box)
    fig = plot_pnf(df, cols, box, "测试", targets=pnf_targets(df, cols, box),
                   history=hist)
    ax = fig.axes[0]
    marks = [t.get_text() for t in ax.texts
             if "已到" in t.get_text() or "未到" in t.get_text()]
    assert marks, "应生成历史命中标注文本"
    segs = [t.get_text() for t in ax.texts if "段" in t.get_text()]
    assert segs, "应生成历史段短标签"
    tops = [t.get_text() for t in fig.texts if "当前区间" in t.get_text()]
    assert tops, "图注应有当前区间信息行"
    bottoms = [t.get_text() for t in fig.texts
               if "准确率" in t.get_text() or "到位" in t.get_text()]
    assert bottoms, "图底部应有历史准确率统计"
