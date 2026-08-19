# -*- coding: utf-8 -*-
"""AI 反向证伪 (wyckoff/falsify.py) 回归测试。

不调用真实 API: 验证不可用时的优雅降级与 JSON 解析/门控逻辑。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from wyckoff.falsify import (_parse_json, falsify_structure, llm_client,
                             fal_lines)


def _mk_df(n=40):
    closes = np.linspace(10, 12, n)
    return pd.DataFrame({
        "day": pd.date_range("2024-01-01", periods=n),
        "open": closes, "close": closes, "high": closes + 0.1,
        "low": closes - 0.1, "volume": np.full(n, 1e6),
    })


def test_llm_client_unavailable_when_disabled():
    assert llm_client({"ai_falsify_enabled": False}) is None
    assert llm_client({"ai_falsify_enabled": True, "ai_api_key": ""}) is None


def test_falsify_returns_none_without_key():
    df = _mk_df()
    res = falsify_structure(df, [], settings={"ai_falsify_enabled": True, "ai_api_key": ""})
    assert res is None


def test_falsify_returns_none_when_disabled():
    df = _mk_df()
    res = falsify_structure(df, [], settings={})
    assert res is None


def test_parse_json_variants():
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前缀 {"a": 1} 后缀') == {"a": 1}
    assert _parse_json("不是JSON") is None


def test_fal_lines_disabled():
    lines = fal_lines(None)
    assert lines and "未启用" in lines[0]


def test_fal_lines_with_result():
    fal = {
        "result": "SUCCEEDED", "confidence": 80,
        "violated": [{"condition": "缩量ST", "expected": "缩量",
                      "actual": "放量", "severity": "CRITICAL"}],
        "alternative": {"phase": "派发", "reasoning": "量价不符", "confidence": 75},
        "assessment": "判断可疑", "advice_gate": "BLOCK",
    }
    lines = fal_lines(fal)
    joined = "\n".join(lines)
    assert "被推翻" in joined
    assert "BLOCK" in joined
    assert "替代假设: 派发" in joined


def test_falsify_prompt_long_only_constraint():
    """AI 证伪 prompt 必须注入 A股只能做多约束: 允许质疑结构, 禁止做空指令。"""
    from wyckoff.falsify import _FALSIFY_PROMPT
    assert "只能做多、不能做空" in _FALSIFY_PROMPT
    assert "减仓/离场/回避/不追高/不接飞刀" in _FALSIFY_PROMPT
    for w in ("做空", "放空", "开空仓", "空头可入场", "裸卖空", "空头回补"):
        assert w in _FALSIFY_PROMPT, f"违禁词未列入: {w}"
