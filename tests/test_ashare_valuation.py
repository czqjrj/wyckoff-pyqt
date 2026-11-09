"""指数估值层单元测试 (不触网)。"""

import os
from datetime import date, timedelta

import pandas as pd

from wyckoff import ashare_valuation as av


def _csindex_frame(n=20):
    """模拟中证官网: 倒序 (最新在前), 10 列。"""
    dates = list(pd.date_range("2026-10-12", periods=n, freq="B").strftime("%Y-%m-%d"))
    rows = []
    for i, d in enumerate(dates):
        pe = 14.0 + i * 0.1
        rows.append(
            [d, "000300", "沪深300指数", "沪深300", "CSI 300", "CSI 300", pe + 1.0, pe, 2.7, 2.5]
        )
    return pd.DataFrame(rows[::-1])


def test_fetch_csindex_parse(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_zh_index_value_csindex", lambda symbol: _csindex_frame())
    df = av._fetch_csindex("000300")
    assert df is not None
    assert len(df) == 20
    assert list(df.columns) == ["date", "pe", "pe_static", "div"]
    assert df["date"].is_monotonic_increasing
    assert abs(df["pe"].iloc[-1] - 15.9) < 1e-9
    assert abs(df["pe_static"].iloc[-1] - 16.9) < 1e-9
    assert df["div"].iloc[-1] == 2.7


def test_build_valuation_accumulates(tmp_path, monkeypatch):
    import akshare as ak

    path = str(tmp_path / "val.json")
    monkeypatch.setattr(ak, "stock_zh_index_value_csindex", lambda symbol: _csindex_frame())
    out = av.build_valuation_context(bond10=1.6, path=path)
    assert out["ok"] is True
    assert out["as_of"] == "2026-11-06"
    it = out["indices"]["sh000300"]
    assert it["n"] == 20
    assert it["ready"] is False
    assert it["label"] == "样本累积中"
    assert it["pe_pct"] is None
    assert abs(it["erp"] - round(100.0 / 15.9 - 1.6, 2)) < 1e-9
    assert it["erp_pct"] is None  # ERP 本地样本不足
    assert os.path.exists(path)

    # 重复刷新: 同日去重, 样本数不翻倍
    out2 = av.build_valuation_context(bond10=1.6, path=path)
    assert out2["indices"]["sh000300"]["n"] == 20


def test_valuation_percentile_when_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "val.json")
    rows = [
        {
            "date": (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
            "pe": 10 + i * 0.05,
            "pe_static": 10 + i * 0.05,
            "div": 2.0,
        }
        for i in range(260)
    ]
    av._save(
        {"version": 1, "indices": {"sh000300": {"name": "沪深300", "rows": rows}}},
        path,
    )
    monkeypatch.setattr(av, "_fetch_csindex", lambda code: None)
    out = av.build_valuation_context(bond10=1.5, path=path)
    it = out["indices"]["sh000300"]
    assert it["ready"] is True
    assert it["n"] == 260
    assert it["pe_pct"] is not None
    assert it["label"] in ("低估", "合理", "偏高")
    assert it["erp"] is not None
    assert out["note"].find("已可给分位") >= 0


def test_valuation_missing(tmp_path, monkeypatch):
    path = str(tmp_path / "none.json")
    monkeypatch.setattr(av, "_fetch_csindex", lambda code: None)
    out = av.build_valuation_context(bond10=1.5, path=path)
    assert out["ok"] is False
    assert out["indices"] == {}
