# -*- coding: utf-8 -*-
"""导出报告构建 (wyckoff/report.py) 回归测试。

核心: 报告必须包含 完整分节结论 + 关键量化速览 + 近期K线明细,
喂给大模型时不能让"当前选中单个标签"那种稀疏文本导致解读偏离。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.report import (build_export_report, build_quick_snapshot,
                            build_recent_bars, build_signal_summary_text,
                            build_indicator_table, build_vsa_table,
                            build_event_table)


def _mk_df(n=220):
    closes = np.linspace(20, 22, n) + np.sin(np.arange(n) / 5) * 0.2
    o = closes * 0.999
    df = pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": o, "close": closes, "high": np.maximum(closes, o) * 1.005,
        "low": np.minimum(closes, o) * 0.995, "volume": np.full(n, 500e4),
    })
    df["direction"] = np.where(df["close"] >= df["open"], 1, -1)
    from wyckoff.indicators import add_indicators
    return add_indicators(df)


def _sections():
    return [("概览", ["最新价 21.00 (2024-06-30)   当前阶段: 底部整固",
                      "解读: 低点上移, Spring 已现",
                      "数据源: 新浪"]),
            ("交易计划", ["  方向: 多头/低吸", "  现价: 21.00",
                          "  止损: 20.50", "  目标1: 22.30",
                          "  盈亏比: 2.60 : 1"]),
            ("近期事件", ["  2024-06-01  Spring  @ 20.20  弹簧 - 低点缩量",
                          "  2024-06-10  SOS     @ 21.50  强势信号"])]


def test_export_contains_all_blocks():
    df = _mk_df()
    text = build_export_report("sz000001", "平安银行", "日线", "近3年", df,
                               _sections(),
                               summary_cards=[{"label": "综合", "value": "综合偏多 +20 (高置信)",
                                               "tone": "bullish"}])
    assert "威科夫分析报告" in text
    assert "【关键量化速览】" in text
    assert "【信号汇总】" in text
    assert "【完整分析结论】" in text
    assert "【近期K线明细】" in text
    # 全部分析内容必须进报告: 指标序列 / VSA 全量 / 事件全量
    assert "【指标明细】" in text
    assert "【VSA信号全量】" in text
    assert "【威科夫事件全量】" in text
    # 完整分节必须都进来, 而不是只导出现选中的一个标签
    assert "── 交易计划 ──" in text
    assert "方向: 多头/低吸" in text
    # 量化数值精确锚点
    assert "最新价:" in text
    assert "MA20:" in text
    assert "ATR(14):" in text


def test_quick_snapshot_precise_numbers():
    df = _mk_df()
    lines = build_quick_snapshot(df)
    joined = "\n".join(lines)
    assert "最新价:" in joined
    assert "MA5:" in joined and "MA200:" in joined
    assert "当日涨跌" not in joined or "%" in joined
    # 精确到两位小数
    for ln in lines:
        if "价:" in ln or "MA" in ln:
            assert ".2f" not in ln  # 占位符没有被原样输出


def test_recent_bars_layout():
    df = _mk_df()
    lines = build_recent_bars(df, rows=10)
    assert len(lines) == 11  # 表头 + 10 根
    assert "日期" in lines[0]
    row = lines[1].split()
    assert len(row) >= 6


def test_recent_bars_truncates_to_df_len():
    df = _mk_df()
    lines = build_recent_bars(df, rows=500)
    # 表头 + 全部 bar, 不多也不少
    assert len(lines) == len(df) + 1


def test_indicator_table_has_all_series():
    df = _mk_df()
    lines = build_indicator_table(df, rows=30)
    assert len(lines) == 31  # 表头 + 30 根
    assert "MA5" in lines[0] and "MACD柱" in lines[0] and "量Z20" in lines[0]
    row = lines[1].split()
    assert len(row) >= 18


def test_indicator_table_truncates_to_df_len():
    df = _mk_df()
    lines = build_indicator_table(df, rows=5000)
    assert len(lines) == len(df) + 1


def test_vsa_table_lists_non_neutral_only():
    df = _mk_df()
    vsa = [{"idx": 1, "date": pd.Timestamp("2024-01-02"), "label": "N",
            "desc": "中性"}, {"idx": 2, "date": pd.Timestamp("2024-01-03"),
                              "label": "SC", "desc": "卖出高潮"}]
    lines = build_vsa_table(df, vsa_signals=vsa)
    assert "SC" in "".join(lines)
    assert "中性" not in "".join(lines[1:])


def test_vsa_table_graceful_when_empty():
    df = _mk_df()
    lines = build_vsa_table(df, vsa_signals=[])
    assert lines and lines[0].startswith("(无")


def test_event_table_includes_confirmation():
    df = _mk_df()
    events = [{"type": "Spring", "idx": 5, "date": pd.Timestamp("2024-01-06"),
               "price": 20.2, "desc": "弹簧", "confirmed": True},
              {"type": "UTAD", "idx": 6, "date": pd.Timestamp("2024-01-07"),
               "price": 21.5, "desc": "上冲派发", "confirmed": False}]
    lines = build_event_table(df, events=events)
    joined = "\n".join(lines)
    assert "Spring" in joined and "✓确认" in joined
    assert "UTAD" in joined and "✗未确认" in joined


def test_signal_summary_text_marks_tone():
    cards = [{"label": "综合", "value": "综合偏多 +20 (高置信)", "tone": "bullish"},
             {"label": "大盘", "value": "中性", "tone": "neutral"}]
    lines = build_signal_summary_text(cards)
    assert lines[0].startswith("- 综合:")
    assert "[偏多]" in lines[0]
    assert "[中性]" in lines[1]


def test_export_graceful_with_empty_inputs():
    text = build_export_report("sz000001", "", "日线", "近3年", None, [], rows=5)
    assert "威科夫分析报告" in text
    assert "近期K线明细" in text