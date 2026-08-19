# -*- coding: utf-8 -*-
"""验证图表平移/缩放边界: 越界输入不得产生空切片 (回归测试) 。"""


def _clamp_pan_x0(x0, x1, full0, full1, frac):
    """复制 _pan_by 的边界逻辑。"""
    full_span = full1 - full0
    span = x1 - x0
    if full_span <= 0:
        return None
    if span >= full_span - 0.5:
        return (full0, full1)
    n_x0 = x0 + span * frac
    n_x0 = min(max(n_x0, full0), full1 - span)
    return (n_x0, n_x0 + span)


def _clamp_zoom(new_span, center, x0, x1, full0, full1):
    """复制 _zoom_about 的边界逻辑。"""
    span = x1 - x0
    full_span = full1 - full0
    new_span = min(max(new_span, 15.0), full_span)
    if new_span >= full_span - 0.5:
        return (full0, full1)
    t = min(max((center - x0) / span, 0.0), 1.0) if span > 0 else 0.5
    new_x0 = center - new_span * t
    new_x1 = new_x0 + new_span
    if new_x0 < full0:
        new_x0 = full0
        new_x1 = full0 + new_span
    if new_x1 > full1:
        new_x1 = full1
        new_x0 = full1 - new_span
    return (new_x0, new_x1)


def _slice_bounds(x0, x1, n):
    """复制 _rescale_y 的切片计算, 返回切片是否为空。"""
    i0 = max(0, int(np_floor(x0)))
    i1 = min(n - 1, int(np_ceil(x1)))
    return i0, i1, i0 <= i1 and i1 >= 0 and i0 < n


def np_floor(x):
    return int(x // 1) if x >= 0 else -int((-x + 0.9999) // 1)


def np_ceil(x):
    return -int((-x) // 1)


def test_pan_within_bounds():
    """全幅视图平移不应越界 (之前 full1-span < full0 导致空切片)。"""
    r = _clamp_pan_x0(0.0, 700.0, 0.0, 700.0, 0.2)
    assert r == (0.0, 700.0), r


def test_pan_zoomed_no_escape():
    """缩放后的视图连续平移不应超出全幅边界。"""
    full0, full1 = 0.0, 700.0
    x0, x1 = 100.0, 250.0
    for _ in range(50):
        r = _clamp_pan_x0(x0, x1, full0, full1, 0.2)
        x0, x1 = r
        assert x0 >= full0 - 1e-9
        assert x1 <= full1 + 1e-9
        # 切片不空
        i0, i1, ok = _slice_bounds(x0, x1, 700)
        assert ok, (x0, x1, i0, i1)


def test_zoom_stays_inside():
    """缩放迭代应始终留在全幅范围内。"""
    full0, full1 = 0.0, 700.0
    x0, x1 = 0.0, 700.0
    for i in range(40):
        center = (x0 + x1) / 2
        factor = 0.8 if i % 2 == 0 else 1.25
        span = x1 - x0
        x0, x1 = _clamp_zoom(span * factor, center, x0, x1, full0, full1)
        assert x0 >= full0 - 1e-9, (x0, x1)
        assert x1 <= full1 + 1e-9, (x0, x1)
        i0, i1, ok = _slice_bounds(x0, x1, 700)
        assert ok, (x0, x1, i0, i1)


def test_zoom_min_span_respected():
    """缩放下限 15 根内不应产生无效切片。"""
    full0, full1 = 0.0, 700.0
    x0, x1 = 0.0, 700.0
    for _ in range(30):
        center = (x0 + x1) / 2
        span = x1 - x0
        x0, x1 = _clamp_zoom(span * 0.5, center, x0, x1, full0, full1)
        assert x1 - x0 >= 14.9
