"""波浪计数模块 (wyckoff.wavecount) 测试: 完美5浪/ABC/无效结构。"""


from wyckoff.wavecount import _is_abc, _is_impulse, count_waves


def _piv(types, prices, idxs=None):
    idxs = idxs or list(range(len(types)))
    return [{"type": t, "price": p, "idx": i}
            for t, p, i in zip(types, prices, idxs)]


def test_is_impulse_up():
    """上升推动浪: 低-高-低-高-低, 浪2/浪4交替, 浪3最长。"""
    pts = [("low", 10.0, 0), ("high", 15.0, 1), ("low", 11.0, 2),
           ("high", 20.0, 3), ("low", 12.0, 4)]
    assert _is_impulse(pts)


def test_is_impulse_wave2_overlaps():
    """浪2破浪1起点 → 非法推动。"""
    pts = [("low", 10.0, 0), ("high", 15.0, 1), ("low", 9.5, 2),
           ("high", 20.0, 3), ("low", 12.0, 4)]
    assert not _is_impulse(pts)


def test_is_impulse_wave3_shortest():
    """浪3为最短 → 非法 (艾略特规则: 浪3非最短)。"""
    pts = [("low", 10.0, 0), ("high", 12.0, 1), ("low", 11.0, 2),
           ("high", 12.5, 3), ("low", 12.0, 4)]
    assert not _is_impulse(pts)


def test_is_abc_up_correction():
    """上升修正 ABC: 高-低-高-低。"""
    pts = [("high", 20.0, 0), ("low", 16.0, 1), ("high", 18.0, 2),
           ("low", 15.0, 3)]
    assert _is_abc(pts)


def test_count_impulse_up():
    """完整上升推动浪计数: 返回 impulse, 方向 up, 5浪, 浪3最长。"""
    piv = _piv(["low", "high", "low", "high", "low"],
               [10.0, 15.0, 11.0, 20.0, 12.0])
    wc = count_waves(piv)
    assert wc.kind == "impulse"
    assert wc.direction == "up"
    assert len(wc.waves) == 4
    assert wc.position_wave == "5"
    assert wc.invalidation == 10.0
    assert any(w["label"] == "3浪" for w in wc.waves)


def test_count_abc():
    """ABC 修正计数 (下跌修正 high-low-high-low)。"""
    piv = _piv(["high", "low", "high", "low"],
               [20.0, 16.0, 18.0, 15.0])
    wc = count_waves(piv)
    assert wc.kind == "corrective"
    assert wc.direction == "down"
    assert len(wc.waves) == 3
    assert wc.waves[0]["wave"] == "A"


def test_count_none_insufficient():
    """枢轴不足 → none。"""
    piv = _piv(["low", "high"], [10.0, 15.0])
    wc = count_waves(piv)
    assert wc.kind == "none"


def test_count_none_no_pattern():
    """枢轴够但无合法结构 → none (C浪反向突破, ABC 不成立)。"""
    piv = _piv(["high", "low", "high", "low"],
               [20.0, 15.0, 16.0, 18.0])
    wc = count_waves(piv)
    assert wc.kind == "none"


def test_fib_confluence_present():
    """推动浪含回撤+扩展汇聚位。"""
    piv = _piv(["low", "high", "low", "high", "low"],
               [10.0, 15.0, 11.0, 20.0, 12.0])
    wc = count_waves(piv)
    kinds = {c["kind"] for c in wc.fib_confluence}
    assert kinds == {"回撤", "扩展"}
    assert wc.next_target is not None
