# -*- coding: utf-8 -*-
"""验证波段累积成交量 (每波段重置累计) 的切分与累计逻辑。"""
import numpy as np

from wyckoff.chart import _wave_segments, _wave_cum_volume


def test_wave_segments_reset_at_boundaries():
    """波段边界 = 波浪点 index, 前后导段方向与相邻段相反。"""
    waves = [(10, 5.0), (20, 8.0), (30, 6.0), (40, 9.0)]
    segs = _wave_segments(50, waves)
    assert segs == [(0, 10, -1), (10, 20, 1), (20, 30, -1), (30, 40, 1),
                    (40, 49, -1)]


def test_wave_segments_no_leading_trailing_when_full():
    """波浪点正好覆盖首末柱时不再延伸。"""
    waves = [(0, 5.0), (10, 8.0), (20, 6.0)]
    segs = _wave_segments(21, waves)
    assert segs == [(0, 10, 1), (10, 20, -1)]


def test_wave_segments_empty_and_single():
    """无波浪点返回 None, 单点返回整体一段中性段。"""
    assert _wave_segments(20, []) is None
    assert _wave_segments(20, None) is None
    assert _wave_segments(20, [(5, 8.0)]) == [(0, 19, 0)]


def test_wave_segments_accepts_triples():
    """兼容 wavecount 的三元组 (idx, price, label)。"""
    waves = [(5, 3.0, "1"), (9, 6.0, "2"), (13, 5.0, "3")]
    segs = _wave_segments(16, waves)
    assert segs == [(0, 5, -1), (5, 9, 1), (9, 13, -1), (13, 15, 1)]


def test_wave_cum_volume_resets_and_signed():
    """每波段内累计带符号量, 边界处重置; 涨柱正跌柱负。"""
    n = 10
    up = np.array([True, True, False, False, True, False, True, True,
                   False, True])
    vol = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    waves = [(2, 5.0), (6, 8.0)]
    cum, segs = _wave_cum_volume(n, waves, up, vol)
    assert segs is not None
    # 前导段 [0,2]: 累计 1, 3; 边界柱2由段2在边界处重置覆盖 (-3)
    assert cum[0] == 1.0
    assert cum[1] == 3.0
    assert cum[2] == -3.0
    # 段2 [2,6]: 从2重置, -3, -7, -2, -8, -1 (逐根: -3,-4,+5,-6,+7)
    assert cum[3] == -7.0
    assert cum[4] == -2.0
    assert cum[5] == -8.0
    # 尾随段 [6,9]: 从6重置, 7, 15, 6, 16 (7+8-9+10)
    assert cum[6] == 7.0
    assert cum[7] == 15.0
    assert cum[8] == 6.0
    assert cum[9] == 16.0


def test_wave_cum_volume_fallback_global():
    """无波浪点时回退为全局 OBV 式累计。"""
    up = np.array([True, False, True])
    vol = np.array([5, 2, 4], dtype=float)
    cum, segs = _wave_cum_volume(3, [], up, vol)
    assert segs is None
    assert list(cum) == [5.0, 3.0, 7.0]
