"""backfill_analysis 分析层回填测试 (离线: conftest 设 WYCKOFF_NO_NET=1 + 临时数据目录)。"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.accuracy import HORIZONS, _key, load_accuracy
from wyckoff.backfill_analysis import (
    align_bench,
    backfill_analysis,
    backfill_one_symbol,
    cut_points,
    load_symbols,
)
from wyckoff.indicators import add_indicators


def _kline(n=620, seed=7, start="2022-01-03"):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 10 + np.sin(t / 22.0) * 1.2 + np.sin(t / 7.0) * 0.3 \
        + rng.normal(0, 0.06, n)
    close = np.maximum(close, 1.0)
    high = close + np.abs(rng.normal(0.10, 0.04, n))
    low = np.maximum(close - np.abs(rng.normal(0.10, 0.04, n)), 0.5)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.abs(1e6 + rng.normal(0, 1.2e5, n))
    day = pd.date_range(start, periods=n, freq="B")
    return add_indicators(pd.DataFrame({
        "day": day, "open": open_, "high": high, "low": low,
        "close": close, "volume": vol}))


# 决策字段: 这些必须与"未来数据是否存在"无关 (无前视的判定面)
_DECISION = ("ref_dt", "ref_close", "phase", "phase_tone", "pnf_dir",
             "fusion_score", "fusion_bias", "trade_dir", "up_target",
             "down_target", "events")


@pytest.fixture(autouse=True)
def clean_accuracy_file():
    """每个测试前清空分析准确度库。

    conftest 的 isolated_cache 只重置 sqldb, ACCURACY_FILE 在整个 session
    共享 → 本模块的幂等/干跑/写空库断言会互相污染 (去重键含 ref_dt, 不同
    seed 的合成 K 线日期相同 → 天然撞键)。
    """
    from wyckoff.paths import ACCURACY_FILE
    if os.path.exists(ACCURACY_FILE):
        os.remove(ACCURACY_FILE)
    yield
    if os.path.exists(ACCURACY_FILE):
        os.remove(ACCURACY_FILE)


# ── 切点选取 ──
def test_cut_points_bounds_and_tail():
    pts = cut_points(1000, step=20, warmup=250, tail=40, max_points=None)
    assert pts, "应产出切点"
    assert min(pts) >= 250, "不得早于预热 (MA200 等长窗口)"
    assert max(pts) <= 1000 - 40, f"须留出 max(HORIZONS)={max(HORIZONS)} 根未来行情"
    assert all(b - a == 20 for a, b in zip(pts, pts[1:])), "步长应恒定"


def test_cut_points_caps_by_max_points():
    pts = cut_points(5000, step=5, warmup=250, max_points=7)
    assert len(pts) == 7
    assert pts == sorted(pts) and len(set(pts)) == 7, "抽样后应保持升序去重"
    assert min(pts) >= 250 and max(pts) <= 5000 - max(HORIZONS)


def test_cut_points_empty_when_series_too_short():
    assert cut_points(100, step=20, warmup=250) == []


# ── 基准按日期对齐 (而非按位置) ──
def test_align_bench_aligns_by_date_not_position():
    df = _kline(n=300, seed=11, start="2024-03-01")
    # 基准故意短很多且起点不同: 按位置对齐会整体错位
    bench = _kline(n=120, seed=12, start="2024-06-03")
    got = align_bench(df, bench)
    assert got is not None
    c = got["close"]
    d_day = df["day"].to_numpy()
    b_day = bench["day"].to_numpy()
    # 基准起点之前 / 终点之后都不应对齐 (不拿陈旧值长距离前向填充)
    assert np.isnan(c[d_day < b_day[0]]).all(), "基准起点之前应留 NaN"
    gap_days = (d_day - b_day[-1]) / np.timedelta64(1, "D")
    far = gap_days > 7          # 超出容忍窗 → 必须缺省
    assert far.any() and np.isnan(c[far]).all(), "超出 max_gap 应留 NaN, 不得长距离前向填充"
    near = (gap_days > 0) & (gap_days <= 7)   # 容忍窗内 (长假/停牌) 可用基准末值
    if near.any():
        assert np.isfinite(c[near]).all(), "容忍窗内应对齐到基准末值"
    # 重叠区必须逐点等于基准按日期的收盘价 (而非按位置)
    ov = np.where((d_day >= b_day[0]) & (d_day <= b_day[-1]))[0]
    assert len(ov) > 60, "重叠区应足够大, 才有检验意义"
    b_close = bench.set_index("day")["close"]
    for i in (ov[0], ov[len(ov) // 2], ov[-1]):
        assert c[i] == pytest.approx(b_close.loc[df["day"].iloc[i]], rel=1e-9)


def test_align_bench_positional_would_be_wrong():
    """证明"按位置对齐"确实是错的: 同长度但起点不同的两个序列, 按位置会错值。"""
    a = _kline(n=100, seed=41, start="2024-01-01")
    b = _kline(n=100, seed=42, start="2024-09-02")
    assert len(a) == len(b)
    naive = a["close"].to_numpy() == b["close"].to_numpy()
    assert not naive.any(), "合成数据不应逐点相等"


def test_align_bench_none_when_disjoint():
    df = _kline(n=200, seed=13, start="2024-01-01")
    bench = _kline(n=200, seed=14, start="2030-01-01")
    assert align_bench(df, bench) is None


def test_align_bench_none_inputs():
    df = _kline(n=100, seed=15)
    assert align_bench(df, None) is None
    assert align_bench(None, df) is None


# ── 单标的回填 ──
def test_backfill_one_symbol_records_history_with_results():
    df = _kline()
    recs = backfill_one_symbol(df, "sh600104", name="测试", step=40,
                               warmup=250, max_points=5)
    assert recs, "应产出历史快照"
    assert all(r.get("backfill") for r in recs)
    for r in recs:
        assert r["symbol"] == "sh600104" and r["scale"] == 240
        assert r["ref_dt"] in df["day"].dt.strftime("%Y-%m-%d %H:%M").values
        # 距末端 >= max(HORIZONS) → 三个周期都应能评估出来
        assert len(r["results"]) == len(HORIZONS), \
            f"应有 {HORIZONS} 全部周期, 实际 {sorted(r['results'])}"
        assert r["status"] == "done"
        for h in HORIZONS:
            assert r["results"][str(h)]["ret"] is not None
    # ref_dt 必须互不相同 (去重键的前提)
    assert len({_key(r) for r in recs}) == len(recs)


def test_backfill_one_symbol_is_causal():
    """核心: 截断未来数据后, 同一 ref_dt 的决策字段必须逐字不变。"""
    df = _kline(n=700, seed=21)
    full = backfill_one_symbol(df, "sh600104", step=60, warmup=250, max_points=4)
    assert full
    k = full[-1]["ref_dt"]
    cut = int(np.where(df["day"].dt.strftime("%Y-%m-%d %H:%M").values == k)[0][0])
    # 只保留到该切点 + 刚好够评估的长度: 未来只剩评估必需的那几根
    trimmed = df.iloc[:cut + max(HORIZONS) + 1]
    again = backfill_one_symbol(trimmed, "sh600104", step=60, warmup=250,
                                max_points=200)
    match = [r for r in again if r["ref_dt"] == k]
    assert match, "截断后应仍能产出同一时点的快照"
    a, b = full[-1], match[0]
    for key in _DECISION:
        assert a.get(key) == b.get(key), f"{key} 受未来数据影响: {a.get(key)} vs {b.get(key)}"


def test_backfill_one_symbol_empty_inputs():
    assert backfill_one_symbol(None, "sh600104") == []
    assert backfill_one_symbol(_kline(n=100), "sh600104", warmup=250) == []


# ── 批量编排 ──
def test_backfill_analysis_persists_and_is_idempotent():
    df = _kline()
    calls = []

    def kfn(sym):
        calls.append(sym)
        return df

    st = backfill_analysis(["sh600104", "sz000001"], kfn, step=80, warmup=250,
                           max_points=3, sleep=0, bench_df=df, log=lambda *_: None)
    assert st["scan"] == 2 and st["sym_ok"] == 2
    assert st["added"] == 6 and st["evaluated"] == 6, st
    assert len(load_accuracy()) == 6

    st2 = backfill_analysis(["sh600104", "sz000001"], kfn, step=80, warmup=250,
                            max_points=3, sleep=0, bench_df=df,
                            log=lambda *_: None)
    assert st2["added"] == 0, "重跑必须幂等 (按 _key 去重)"
    assert len(load_accuracy()) == 6


def test_backfill_analysis_dry_run_does_not_write():
    df = _kline()
    st = backfill_analysis(["sh600104"], lambda s: df, step=80, warmup=250,
                           max_points=2, sleep=0, log=lambda *_: None,
                           dry_run=True)
    assert st["added"] == 2
    assert load_accuracy() == [], "dry-run 不得写盘"


def test_backfill_analysis_bench_gives_excess_return():
    df = _kline(seed=31)
    st = backfill_analysis(["sh600104"], lambda s: df, step=80, warmup=250,
                           max_points=2, sleep=0, bench_df=df,
                           log=lambda *_: None)
    assert st["added"] == 2
    for r in load_accuracy():
        for h in HORIZONS:
            assert r["results"][str(h)]["bench"] is not None, "有基准时应落超额收益"


def test_backfill_analysis_no_bench_skips_excess_return():
    df = _kline(seed=32)
    backfill_analysis(["sh600104"], lambda s: df, step=80, warmup=250,
                      max_points=1, sleep=0, log=lambda *_: None)
    assert load_accuracy()[0]["results"]["20"]["bench"] is None


def test_backfill_analysis_survives_fetch_failure_and_short_df():
    def kfn(sym):
        if sym == "sh600519":
            raise RuntimeError("网络炸了")
        return _kline(n=80)      # 长度不足

    st = backfill_analysis(["sh600519", "sz000001"], kfn, step=80, warmup=250,
                           max_points=2, sleep=0, log=lambda *_: None)
    assert st["failed"] == 1 and st["skip"] == 1, st
    assert st["added"] == 0
    assert load_accuracy() == [], "全失败不得写空库"


# ── 标的来源 ──
def test_load_symbols_filters_index_and_etf(tmp_path):
    p = tmp_path / "codes.json"
    p.write_text('{"codes": ["sh600104", "sh000001", "sz399001", "sh510300",'
                 ' "bj430047", "sz000001", "sh600104"]}', encoding="utf-8")
    got = load_symbols(source=str(p))
    assert got == ["sh600104", "sz000001"], got


def test_load_symbols_reads_plain_text(tmp_path):
    p = tmp_path / "codes.txt"
    p.write_text("sh600104\nsz000001\n\nsh600519\n", encoding="utf-8")
    assert load_symbols(source=str(p)) == ["sh600104", "sz000001", "sh600519"]


def test_load_symbols_max_symbols(tmp_path):
    p = tmp_path / "codes.json"
    p.write_text('["sh600104", "sz000001", "sh600519"]', encoding="utf-8")
    assert load_symbols(source=str(p), max_symbols=2) == ["sh600104", "sz000001"]
