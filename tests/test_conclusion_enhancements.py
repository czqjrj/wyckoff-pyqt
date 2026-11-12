"""结论/汇总对增强层 (反面证据/九大检验/AI证伪/仓位) 的渲染回归测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.conclusion import build_conclusion, build_signal_summary
from wyckoff.counterevidence import counter_evidence
from wyckoff.ninetests import nine_tests


def _mk_df(n=120):
    closes = np.linspace(20, 22, n) + np.sin(np.arange(n) / 5) * 0.2
    o = closes * 0.999
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": o, "close": closes, "high": np.maximum(closes, o) * 1.005,
        "low": np.minimum(closes, o) * 0.995, "volume": np.full(n, 1e6),
        "price_ma20": np.full(n, np.mean(closes)), "price_ma50": np.full(n, np.mean(closes)),
        "price_ma200": np.full(n, np.mean(closes)),
        "atr": np.full(n, 0.3),
    })
    df["direction"] = np.where(df["close"] >= df["open"], 1, -1)
    return df


def _ev(etype, idx, price):
    return {"type": etype, "idx": idx, "date": pd.Timestamp("2024-06-01"),
            "price": price, "desc": "", "conf": 90}


def _base(df):
    events = [_ev("SC", 20, 19.5), _ev("AR", 35, 21.0), _ev("ST", 50, 19.8),
              _ev("Spring", 80, 19.6), _ev("SOS", 100, 21.8)]
    phase = "底部整固 (Accumulation)"
    detail = "低点上移"
    structure = ("C", "弹簧 Spring / 震仓",
                 "吸筹 (Accumulation)\n当前进度: Phase C")
    return events, phase, detail, structure


def test_build_conclusion_has_enhancements():
    df = _mk_df()
    events, phase, detail, structure = _base(df)
    ce = counter_evidence(df, events, phase=phase, structure=structure)
    nt = nine_tests(df, events, pivots=[], phase=phase, structure=structure)
    fal = None
    risk_plan = ["  方向: 多头/低吸", "  止损: 19.50 (风险/股 0.30)",
                 "  建议仓位: 10.0% (3000股, 市值60000元)"]
    sections = build_conclusion(df, [], events, phase, detail, structure=structure,
                                ce=ce, nt=nt, fal=fal, risk_plan=risk_plan)
    titles = {t for t, _ in sections}
    assert "反面证据" in titles
    assert "九大检验" in titles
    assert "仓位建议" in titles
    # AI 未启用 → 不渲染 AI证伪 节
    assert "AI证伪" not in titles
    sec = dict(sections)
    assert any("假设:" in ln for ln in sec["反面证据"])
    assert any("九大" in ln for ln in sec["九大检验"])
    assert any("建议仓位" in ln for ln in sec["仓位建议"])


def test_build_conclusion_ai_section_when_enabled():
    df = _mk_df()
    events, phase, detail, structure = _base(df)
    fal = {"result": "FAILED", "confidence": 70, "violated": [],
           "alternative": {}, "assessment": "成立", "advice_gate": "PASS"}
    sections = build_conclusion(df, [], events, phase, detail, structure=structure,
                                fal=fal)
    titles = {t for t, _ in sections}
    assert "AI证伪" in titles
    joined = "\n".join(ln for _, ls in sections for ln in ls)
    assert "假设成立" in joined and "门控: PASS" in joined


def test_signal_summary_counter_card():
    df = _mk_df()
    events, phase, detail, structure = _base(df)
    ce = counter_evidence(df, events, phase=phase, structure=structure)
    summary = build_signal_summary(df, [], events, structure=structure, ce=ce)
    labels = [s["label"] for s in summary]
    assert "反面" in labels
    card = next(s for s in summary if s["label"] == "反面")
    assert card["tone"] in ("bullish", "bearish", "caution", "neutral")


def test_signal_summary_no_ce():
    df = _mk_df()
    events, phase, detail, structure = _base(df)
    summary = build_signal_summary(df, [], events, structure=structure)
    assert "反面" not in [s["label"] for s in summary]


def test_broken_spring_not_buy_signal():
    """Spring 已跌破低点 → 买点提示必须降级 (旧实现只看事件存在)。"""
    df = _mk_df()
    events = [_ev("SC", 20, 19.5), _ev("AR", 35, 21.0),
              _ev("ST", 50, 19.8), _ev("Spring", 80, 21.9)]
    sections = build_conclusion(df, [], events, "底部整固 (Accumulation)", "低点上移")
    buy = "\n".join(dict(sections)["买点建议"])
    assert "已失效" in buy
    assert "Spring 有效" not in buy


def test_invalid_utad_not_sell_signal():
    """孤立 UTAD (无 BC/UT 前置) → 卖点不得据此减仓。"""
    df = _mk_df()
    events = [_ev("UTAD", 80, 21.5)]
    sections = build_conclusion(df, [], events, "顶部构筑 (Distribution)", "上冲失败")
    sell = "\n".join(dict(sections)["卖点建议"])
    assert "证据不足" in sell
    assert "逢高减仓/离场" not in sell


def test_valid_spring_downgrades_sell_side():
    """买/卖互斥: 有效 Spring 在非派发阶段 → 卖点降级为风控口径, 不再喊减仓。"""
    df = _mk_df()
    events = [_ev("SC", 20, 19.5), _ev("AR", 35, 21.0),
              _ev("ST", 50, 19.8), _ev("Spring", 100, 20.5),
              _ev("BC", 110, 22.5), _ev("UTAD", 115, 22.6)]
    sections = build_conclusion(df, [], events, "底部整固 (Accumulation)", "低点上移")
    sec = dict(sections)
    buy, sell = "\n".join(sec["买点建议"]), "\n".join(sec["卖点建议"])
    assert "Spring 有效" in buy
    assert "卖点降级为风控口径" in sell
    assert "UTAD" not in sell


def test_bool_targets_not_rendered_as_price():
    """P&F 布尔命中字段不能被格式化成 1.00, 横向目标过远需标注仅供参考。"""
    df = _mk_df()
    pnf_t = {"上方hit_近端": True, "上方hit日期_近端": "2024-06-01",
             "近端上方目标": 23.0, "横向计数上方目标": 30.0}
    sections = build_conclusion(df, [], [], "底部整固 (Accumulation)", "",
                                pnf_t=pnf_t)
    joined = "\n".join(ln for _, ls in sections for ln in ls)
    assert "上方hit_近端" not in joined
    assert "近端上方目标: 23.00" in joined
    assert "仅供参考" in joined
