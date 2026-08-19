# -*- coding: utf-8 -*-
"""AI 报告解读 (wyckoff/interpret.py) 回归测试。

不调用真实 API: 验证不可用时的优雅降级与文本清洗逻辑。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.interpret import (llm_client, interpret_report, _clean_text,
                               _is_degenerate, interpret_tag,
                               _contradictory_plan)


def test_llm_client_unavailable_when_disabled():
    assert llm_client({"ai_interpret_enabled": False}) is None
    assert llm_client({"ai_interpret_enabled": True, "ai_api_key": ""}) is None
    assert llm_client({"ai_interpret_enabled": True}) is None


def test_interpret_returns_none_without_key():
    res = interpret_report("测试报告",
                           settings={"ai_interpret_enabled": True, "ai_api_key": ""})
    assert res is None


def test_interpret_returns_none_when_disabled():
    res = interpret_report("测试报告", settings={})
    assert res is None


def test_clean_text_strips_fences():
    assert _clean_text("```\n你好\n```") == "你好"
    assert _clean_text("```text\n你好\n```") == "你好"
    assert _clean_text("  普通文本  ") == "普通文本"
    assert _clean_text("") == ""


def test_interpret_truncates_long_report(capsys):
    # 超长报告应被截断而不是抛错 (无 key 时仍返回 None, 不触发网络)
    long_report = "A" * 20000
    res = interpret_report(long_report,
                           settings={"ai_interpret_enabled": True, "ai_api_key": ""})
    assert res is None


def test_is_degenerate_rejects_header_echo():
    assert _is_degenerate("【一句话总览】")
    assert _is_degenerate("【一句话总览】\n【核心信号】\n【目标与空间】")
    assert _is_degenerate("太短")
    assert _is_degenerate("")
    assert _is_degenerate("no chinese here at all")
    assert not _is_degenerate("该股当前处于底部整固阶段, 整体偏多。核心依据是近期出现Spring弹簧信号...")


def test_interpret_tag_none_without_key():
    """未配置 Key / 未启用时, 单标签解读优雅返回 None。"""
    assert interpret_tag("SPR", settings={}) is None
    assert interpret_tag("SPR",
                         settings={"ai_interpret_enabled": True, "ai_api_key": ""}) is None


def test_interpret_tag_unknown_label():
    assert interpret_tag("NOPE",
                         settings={"ai_interpret_enabled": True, "ai_api_key": ""}) is None


def test_tag_prompt_long_only_constraint():
    """单标签解读 prompt 必须注入 A股单边做多约束。"""
    from wyckoff.interpret import _TAG_INTERPRET_PROMPT
    assert "只能做多、不能做空" in _TAG_INTERPRET_PROMPT
    assert "减仓/离场/回避/不追高/不接飞刀" in _TAG_INTERPRET_PROMPT
    # 偏空标签的示例列表必须覆盖 (UT/BC/UPT/TRU/SUP/ETF/UTAD/ER)
    for t in ("UTAD", "SUP", "UPT", "BC", "TRU"):
        assert t in _TAG_INTERPRET_PROMPT


def test_report_prompt_long_only_and_consistency():
    """整份报告解读 prompt 必须含 A股铁律 + 操作自洽要求。"""
    from wyckoff.interpret import _INTERPRET_PROMPT
    assert "只能做多、不能做空" in _INTERPRET_PROMPT
    assert "止损价 < 入场价 < 目标价" in _INTERPRET_PROMPT
    assert "报告\"交易计划\"一节" in _INTERPRET_PROMPT
    assert "观望" in _INTERPRET_PROMPT
    # 空头/减仓 是离场指引而非可执行空单: prompt 必须禁止把它当止损/目标交易
    assert "空头/减仓" in _INTERPRET_PROMPT
    assert "不构成做空指令" in _INTERPRET_PROMPT or "做空指令" in _INTERPRET_PROMPT


def test_contradictory_plan_detects_stop_above_entry():
    """止损高于入场的解读应被判定矛盾 (用户实测复现)。"""
    bad = ("操作上, 报告给出的建议是'观望'。如果非要参与, 激进者可在价格回踩10.15元附近"
           "且不跌破时轻仓试探, 止损设在10.37元下方, 目标先看11.09元前高。")
    assert _contradictory_plan(bad)


def test_contradictory_plan_accepts_sane_long():
    """止损低于入场的多头解读不应误判。"""
    good = ("激进者可在价格回踩10.15元附近且不跌破时轻仓试探, 止损设在9.93元下方, "
            "目标先看11.09元前高。")
    assert not _contradictory_plan(good)


def test_contradictory_plan_ignores_missing_numbers():
    """缺入场或止损数字时返回 False (不误伤定性描述)。"""
    assert not _contradictory_plan("当前方向未明, 建议观望, 等待放量突破前高后再考虑。")
    assert not _contradictory_plan("激进者可轻仓试探, 突破后看上方空间。")
    assert not _contradictory_plan("")
