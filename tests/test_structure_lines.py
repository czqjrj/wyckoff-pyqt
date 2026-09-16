"""辅助画线/结构标签 structure_lines 及其检测器测试。

威科夫画法补全: Creek 小溪 (吸筹上沿) / Ice 冰线 (派发下沿) / S·R 支撑阻力 /
Throwback 突破后回踩 / SOT 推力衰减。算法见 wyckoff/market.py。
"""
import numpy as np
import pandas as pd

from wyckoff.market import (
    _detect_sot,
    _detect_throwbacks,
    _node_bands,
    structure_lines,
    volume_profile,
)


def _kline(closes, open_offs=None, high_mult=1.005, low_mult=0.995,
           volumes=None):
    """按收盘路径构造 OHLC 序列。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    prev = np.concatenate([[closes[0]], closes[:-1]])
    opn = prev + (closes - prev) * 0.2
    hi = np.maximum(closes, opn) * high_mult
    lo = np.minimum(closes, opn) * low_mult
    if volumes is None:
        volumes = np.ones(n) * 1e5
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": opn, "close": closes, "high": hi, "low": lo,
        "volume": volumes, "range": hi - lo,
    })


def test_creek_ice_from_tr():
    """creek/ice 优先采用 TR 上下轨, 且未传 TR 时退化为价格聚类。"""
    df = _kline(np.linspace(30, 40, 80))
    pivots = [{"idx": 10, "type": "low", "price": 30.0},
              {"idx": 20, "type": "high", "price": 40.5},
              {"idx": 30, "type": "high", "price": 40.4},
              {"idx": 40, "type": "low", "price": 30.1},
              {"idx": 50, "type": "high", "price": 40.3}]
    tr = {"top": 40.5, "bottom": 30.0, "top_tests": 3, "bottom_tests": 2}
    s = structure_lines(df, pivots, tr=tr, window=80)
    assert s["creek"]["price"] == 40.5 and s["creek"]["tests"] == 3
    assert s["ice"]["price"] == 30.0 and s["ice"]["tests"] == 2
    assert any(x["role"] == "S" for x in s["s_r"])
    assert any(x["role"] == "R" for x in s["s_r"])


def test_sr_band_tests_and_title():
    """s_r 只列现价附近 (±25%) 的触及带, spot 聚合为空。"""
    last = 40.0
    df = _kline(np.linspace(35, last, 80))
    pivots = [{"idx": 15, "type": "low", "price": last - 2.0},
              {"idx": 30, "type": "low", "price": last - 2.1},
              {"idx": 45, "type": "low", "price": last - 2.0},
              {"idx": 55, "type": "high", "price": last + 2.0},
              {"idx": 60, "type": "low", "price": last - 20.0}]  # 超出带
    s = structure_lines(df, pivots, window=80)
    sup = [x for x in s["s_r"] if x["role"] == "S"]
    res = [x for x in s["s_r"] if x["role"] == "R"]
    assert sup and abs(sup[0]["price"] - 38.0) < 0.05 and sup[0]["tests"] == 3
    assert res and res[0]["tests"] == 1
    assert not any(abs(x["dist_pct"]) > 25 for x in s["s_r"])


def test_throwback_detected_after_joc():
    """JOC 放量突破上沿后的缩量浅回踩 (守在上沿上) 应标 Throwback。"""
    rng = np.random.RandomState(7)
    n_range, n_after = 95, 40
    closes = list(rng.uniform(87.5, 92.5, n_range))
    closes += [95.0, 99.0, 104.0, 108.0, 110.0,   # JOC 突破上行
               107.0, 104.0, 102.5, 101.8]         # 回踩
    for _ in range(12):
        closes.append(105.0 + rng.uniform(-1.5, 1.5))  # 站稳上沿上方
    closes += [106.0] * (n_after - len(closes) + n_range)
    vols = np.ones(len(closes)) * 1e5
    vols[n_range] = 3e5                 # JOC 放量
    vols[n_range + 4:n_range + 9] = 0.4e5  # 回踩缩量
    df = _kline(closes, volumes=vols,
                high_mult=1.004, low_mult=0.996)
    tr = {"top": 93.0, "bottom": 86.0}
    joc = {"type": "JOC", "idx": n_range, "price": 95.0}
    evs = _detect_throwbacks(df, [joc], tr)
    assert evs, "应有 Throwback"
    e = evs[0]
    assert e["type"] == "Throwback"
    assert e["idx"] > n_range + 2 and e["idx"] < n_range + 15  # 峰值之后找回踩
    assert 93.0 < e["price"] <= 104.0   # 回踩守住上沿, 但已从前峰值明显回落
    assert "守住" in e["desc"]
    assert e["conf"] >= 60


def test_throwback_no_pullback_skipped():
    """突破后直接强上行、没有真实回踩 → 不标 Throwback (防误标)。"""
    closes = list(np.random.RandomState(3).uniform(87, 92, 90))
    closes += [95, 100, 105, 112, 118, 122, 126]
    # 纯拉升, low 止损只回落到峰值以下 0.3 (< 0.01×TR宽=0.14 OK?), 检查无回踩
    df = _kline(closes)
    tr = {"top": 93.0, "bottom": 86.0}
    joc = {"type": "JOC", "idx": 90, "price": 95.0}
    assert _detect_throwbacks(df, [joc], tr) == []


def test_sot_in_markdown():
    """下跌相位中创新低但推力递减 (末腿 ≤ 0.8×均) → 标 SOT, 量缩上调置信度。"""
    n = 160
    # 分段构造: 高点之间回落幅度递减 (推力 0.18 → 0.154 → 0.085)
    segs = list(np.linspace(100, 82, 40))
    segs += list(np.linspace(80, 68, 40))
    segs += list(np.linspace(66, 60, 40))
    segs += list(np.linspace(60, 62, 40))
    closes = np.asarray(segs)
    vols = np.ones(n) * 1e5
    vols[115:121] = 0.3e5                        # 末腿量大幅收缩
    df = _kline(closes, volumes=vols, high_mult=1.05, low_mult=0.97)
    pivots = [
        {"idx": 20, "type": "low", "price": 82.0},
        {"idx": 60, "type": "low", "price": 68.0},
        {"idx": 100, "type": "low", "price": 60.0},
    ]
    evs = _detect_sot(df, pivots, phase="下跌 (Markdown)")
    assert evs and evs[0]["type"] == "SOT"
    assert evs[0]["idx"] == 100 and evs[0]["conf"] >= 70
    assert "量能收缩" in evs[0]["desc"]


def test_sot_skipped_in_markup_and_without_new_lows():
    """上升相位 / 无逐级新低 → 不标 SOT。"""
    df = _kline(np.linspace(40, 60, 100))
    pivots = [{"idx": 20, "type": "low", "price": 45.0},
              {"idx": 50, "type": "low", "price": 52.0},
              {"idx": 80, "type": "low", "price": 58.0}]
    assert _detect_sot(df, pivots, phase="上升 (Markup)") == []
    assert _detect_sot(df, pivots, phase=None) == []


def test_node_bands_merges_contiguous():
    """HVN/LVN 节点带: 相邻超阈桶合并为连续 [lo,hi], 峰值取桶内最大密度。"""
    grid = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9.0])
    dist = np.array([10, 20, 90, 5, 6, 7, 80, 2, 1.0])
    hvn = _node_bands(dist, grid, dist >= 40, min_bins=1)
    assert len(hvn) == 2
    assert (round(hvn[0]["lo"], 4), round(hvn[0]["hi"], 4)) == (2.5, 3.5)
    assert (round(hvn[1]["lo"], 4), round(hvn[1]["hi"], 4)) == (6.5, 7.5)
    assert hvn[1]["peak"] == 80.0  # 桶内最高密度
    # 默认 min_bins=2: 单桶短带被滤噪
    assert _node_bands(dist, grid, dist >= 40) == []


def test_volume_profile_extended_fields():
    """volume_profile 输出 HVN/LVN/POC/价值区 val/vah。"""
    rng = np.random.RandomState(0)
    n = 200
    closes = np.linspace(30, 50, n) + rng.uniform(-0.4, 0.4, n)
    vols = rng.uniform(0.5e5, 2e5, n)
    vols[150:162] = 8e5     # 短期爆量 → HVN
    hi = closes * 1.01
    lo = closes * 0.99
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes - 0.1, "close": closes, "high": hi, "low": lo,
        "volume": vols, "range": hi - lo,
    })
    p = volume_profile(df)
    assert {"poc", "grid", "dist", "avg", "hvn", "lvn", "val", "vah"} <= set(p)
    a, b = p["val"], p["vah"]
    assert a <= p["poc"] <= b      # POC 在价值区内
    assert [x["lo"] for x in p["hvn"]]       # 至少 1 个 HVN 带
    assert [x["lo"] for x in p["lvn"]]
    assert a < b                  # 价值区非退化
