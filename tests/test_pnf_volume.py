"""点数图成交量聚合 (pnf_volume) 的单元测试。

验证: 列级量与箱体量 (Volume-at-Price) 的归属口径与 build_pnf 完全一致,
列数对齐、量守恒 (每根K线量完整计入其所属列, 并按高低价箱体均分)。
"""
import numpy as np
import pandas as pd

from wyckoff.pnf import build_pnf, build_pnf_data, pnf_volume


def _df(n=300, seed=7):
    rng = np.random.default_rng(seed)
    closes = 20 + np.cumsum(rng.normal(0, 0.4, n))
    closes = np.clip(closes, 8, None)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.003, n)),
        "close": closes * (1 + rng.normal(0, 0.003, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": rng.uniform(1e5, 2e6, n),
    })


def _dense(n=120, seed=3):
    """高重叠行情 → 列数较少、每列多根K线, 考验列归属。"""
    rng = np.random.default_rng(seed)
    closes = 50 + rng.normal(0, 0.05, n).cumsum()
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes, "close": closes,
        "high": closes + 0.08, "low": closes - 0.08,
        "volume": rng.uniform(1e5, 2e6, n),
    })


def test_volume_total_conserved():
    """列级量之和 = 箱体量之和 = df 总成交量 (每根K线量守恒)。"""
    for df in (_df(), _dense()):
        cols, box = build_pnf(df)
        vol = pnf_volume(df, cols, box)
        assert len(vol["col_vols"]) == len(cols), "列级量须与列对齐"
        total = float(df["volume"].sum())
        assert abs(sum(vol["col_vols"]) - total) < 1e-6
        assert abs(sum(vol["row_vols"].values()) - total) < 1e-6
        assert abs(vol["total"] - total) < 1e-6
        assert abs(vol["col_max"] - max(vol["col_vols"])) < 1e-6
        assert abs(vol["row_max"] - max(vol["row_vols"].values())) < 1e-6


def test_single_bar_box_distribution():
    """单根K线: 量按高低价跨越的箱体均分, 列级量完整落在唯一列。"""
    df = pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=1),
        "open": [100.0], "close": [100.5],
        "high": [101.0], "low": [99.0],
        "volume": [300.0],
    })
    cols, box = build_pnf(df)
    assert len(cols) == 1

    def r(p):
        return int(round(p / box))

    lo, hi = r(99.0), r(101.0)
    rows = list(range(min(lo, hi), max(lo, hi) + 1))
    share = 300.0 / len(rows)
    vol = pnf_volume(df, cols, box)
    assert vol["col_vols"] == [300.0]
    assert vol["row_vols"] == {row: share for row in rows}
    assert abs(vol["total"] - 300.0) < 1e-9


def test_each_column_volume_is_bar_sum():
    """每列的列级量 = 归属该列的所有K线量之和 (与 build_pnf 同序归属)。"""
    df = _df()
    cols, box = build_pnf(df)
    vol = pnf_volume(df, cols, box)
    # 复算归属核对: 以同一循环规则把每根K线量累加到其列
    _, bar_col = _rebuild_bar_col(df, box)
    expected = [0.0] * len(cols)
    for i, v in enumerate(df["volume"].astype(float).values):
        j = bar_col[i]
        if 0 <= j < len(expected):
            expected[j] += float(v)
    for a, b in zip(vol["col_vols"], expected):
        assert abs(a - b) < 1e-6


def _rebuild_bar_col(df, box, reversal=3):
    """测试内独立复现 build_pnf 的列归属 (不依赖 _build_pnf, 防同源偏差)。"""
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    n = len(df)

    def r(p):
        return int(round(p / box))

    first_rows = list(range(r(lows[0]), r(highs[0]) + 1))
    cur = {"type": "X" if closes[0] >= opens[0] else "O", "rows": first_rows}
    cols = []
    bar_col = [0] * n
    for i in range(1, n):
        hi, lo = r(highs[i]), r(lows[i])
        if cur["type"] == "X":
            top = cur["rows"][-1]
            if hi > top:
                cur["rows"].extend(range(top + 1, hi + 1))
            elif lo <= top - reversal:
                cols.append(cur)
                cur = {"type": "O", "rows": list(range(lo, top))}
                bar_col[i] = len(cols)
        else:
            bottom = cur["rows"][0]
            if lo < bottom:
                cur["rows"] = list(range(lo, bottom)) + cur["rows"]
            elif hi >= bottom + reversal:
                cols.append(cur)
                cur = {"type": "X", "rows": list(range(bottom + 1, hi + 1))}
                bar_col[i] = len(cols)
    cols.append(cur)
    out = [c for c in cols if c["rows"]]
    return out, bar_col


def test_no_volume_column_returns_zeros():
    """df 缺 volume 列 → 返回全零数据 (不抛错)。"""
    df = _df().drop(columns=["volume"])
    cols, box = build_pnf(df)
    vol = pnf_volume(df, cols, box)
    assert vol["col_vols"] == [] and vol["row_vols"] == {}
    assert vol["col_max"] == 0.0 and vol["row_max"] == 0.0 and vol["total"] == 0.0


def test_build_pnf_data_attaches_volumes():
    """build_pnf_data 传 df 时附加 volumes, 不传时不附加。"""
    df = _df()
    cols, box = build_pnf(df)
    data = build_pnf_data(cols, box, "t", df=df)
    assert "volumes" in data and data["volumes"]["col_max"] > 0
    data2 = build_pnf_data(cols, box, "t")
    assert "volumes" not in data2
