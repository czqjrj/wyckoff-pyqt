"""新功能测试: 预警 / 持仓存储 / 备注 / CSV 导出。

覆盖:
  - alerts: 添加/去重/触发/一次性停用/信号预警
  - storage: 持仓与备注的存取
  - CSV 导出: signal_accuracy.export_signals_csv / accuracy.export_accuracy_csv
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_alerts_crud_and_trigger(tmp_path, monkeypatch):
    import wyckoff.alerts as al
    al.ALERTS_FILE = str(tmp_path / "wx_alerts.json")
    assert al.add_alert("600104", "price_up", 12.5, name="上汽集团") is True
    # 重复添加去重
    assert al.add_alert("600104", "price_up", 12.5, name="上汽集团") is False
    # 价格阈值命中
    hits = al.check_price_alerts({"600104": {"name": "上汽集团", "price": 13.0}})
    assert len(hits) == 1
    assert "突破" in hits[0][2]
    # 触发后一次性停用
    assert al.check_price_alerts({"600104": {"name": "上汽集团", "price": 14.0}}) == []
    # 信号预警
    assert al.add_alert("600104", "signal", "Spring", name="上汽集团") is True
    hits2 = al.check_signal_alerts({"600104": ["Spring", "SOS"]})
    assert len(hits2) == 1 and "Spring" in hits2[0][2]
    # 删除
    assert al.remove_alert("600104", "price_up", 12.5) >= 0
    assert al.remove_alert("600104", "signal", "Spring") >= 0


def test_alerts_enable_toggle(tmp_path, monkeypatch):
    import wyckoff.alerts as al
    al.ALERTS_FILE = str(tmp_path / "wx_alerts.json")
    al.add_alert("000001", "price_down", 10.0, name="平安银行")
    al.enable_alert("000001", "price_down", 10.0, False)
    assert al.check_price_alerts({"000001": {"name": "平安银行", "price": 9.0}}) == []
    al.enable_alert("000001", "price_down", 10.0, True)
    hits = al.check_price_alerts({"000001": {"name": "平安银行", "price": 9.0}})
    assert len(hits) == 1 and "跌破" in hits[0][2]


def test_portfolio_and_notes_storage(tmp_path, monkeypatch):
    import wyckoff.storage as st
    from wyckoff.storage import load_notes, load_portfolio, save_notes, save_portfolio
    st.PORTFOLIO_FILE = str(tmp_path / "wx_portfolio.json")
    st.NOTES_FILE = str(tmp_path / "wx_notes.json")
    save_portfolio([{"code": "600104", "name": "上汽集团", "shares": 1000,
                     "cost": 10.5, "stop": 9.8, "buy_date": "2026-08-01",
                     "note": "测试"}])
    p = load_portfolio()
    assert len(p) == 1 and p[0]["code"] == "600104" and p[0]["shares"] == 1000
    save_notes({"600104": "威科夫吸筹阶段, 关注 Spring"})
    assert load_notes()["600104"] == "威科夫吸筹阶段, 关注 Spring"


def test_signals_csv_export(tmp_path):
    import wyckoff.signal_accuracy as sa
    sa.SIGNAL_ACCURACY_FILE = str(tmp_path / "wx_signal_accuracy.json")
    sa._WINRATE_CACHE = None
    import numpy as np
    import pandas as pd
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=60, freq="D"),
        "open": np.linspace(10, 15, 60), "high": np.linspace(11, 16, 60),
        "low": np.linspace(9, 14, 60), "close": np.linspace(10.5, 15.5, 60),
        "volume": np.random.rand(60) * 1e6,
    })
    ev = [{"idx": 5, "type": "SOS", "conf": 80, "price": 12.0,
           "date": df["day"].iloc[5].strftime("%Y-%m-%d %H:%M:%S")}]
    sa.record_signals(df, "sh600104", "600104", 240, 60,
                      events=ev, vsa_signals=[], name="测试", cooldown_bars=0)
    out = str(tmp_path / "sig.csv")
    p = sa.export_signals_csv(sa.load_signals(), out)
    txt = open(p, encoding="utf-8-sig").read()
    assert "600104" in txt and "SOS" in txt and "ret_20" in txt


def test_accuracy_csv_export(tmp_path, monkeypatch):
    import wyckoff.accuracy as acc
    acc.ACCURACY_FILE = str(tmp_path / "wx_accuracy.json")
    recs = [{"symbol": "sh600104", "code": "600104", "name": "上汽集团",
             "scale": 240, "ref_dt": "2026-08-01", "ref_close": 10.5,
             "phase": "底部整固", "phase_tone": "bullish", "pnf_dir": "range",
             "fusion_score": 30.0, "fusion_bias": "看多", "trade_dir": "观望",
             "up_target": 12.0, "down_target": 9.5, "events": ["Spring"],
             "status": "done", "created_ts": 0,
             "results": {"10": {"ret": 0.05, "up_hit": False, "down_hit": False}}}]
    out = str(tmp_path / "acc.csv")
    p = acc.export_accuracy_csv(recs, out)
    txt = open(p, encoding="utf-8-sig").read()
    assert "600104" in txt and "底部整固" in txt and "Spring" in txt
