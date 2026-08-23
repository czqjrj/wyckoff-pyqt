"""AI 解读面板测试: 工具窗口 (国家队持仓 / ETF 三因子) 与 K线标签的 prompt 构造。

覆盖:
  - _holdings_ai_prompt / _etf_ai_prompt: 有数据/无数据时的 prompt 内容与要点。
  - _LabelAiThread / _AiPanel 数据绑定 (离线, 不调用真实模型)。
  - interpret_tag 无 Key 时优雅返回 None (不炸 UI)。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


_APP = None


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_holdings_prompt_builds():
    from desktop.extra_windows import _holdings_ai_prompt
    holders = [{"name": "中央汇金资产管理有限责任公司", "category": "中央汇金",
                "shares": 1.5e8, "pct": 5.2, "change_ratio": 0.5, "status": "加仓",
                "cost": 10.2, "price": 11.5, "pnl_pct": 12.7,
                "first_report": "20260331"}]
    exited = [{"name": "证金公司", "category": "证金", "last_report": "20251231"}]
    p = _holdings_ai_prompt("sh600104", holders, "20260331", exited)
    assert "sh600104" in p
    assert "中央汇金" in p and "加仓" in p
    assert "12.7%" in p  # 盈亏
    assert "20260331" in p  # 建仓季度
    assert "证金公司" in p and "已退出" in p
    # 铁律: 明确禁止做空指令
    assert "不能做空" in p or "无法做空" in p
    # 空数据
    p_empty = _holdings_ai_prompt("sh600104", [], "20260331", [])
    assert "暂无数据" in p_empty


def test_etf_prompt_builds():
    from desktop.extra_windows import _etf_ai_prompt
    etf = [{"name": "华夏上证50ETF", "symbol": "sh510050", "signal": "高确信买入",
            "strength": 0.8, "vol_ratio": 2.1, "share_1d": 1.2, "share_5d": 3.5,
            "etf_ret5": 2.0, "bench_ret5": -0.5}]
    p = _etf_ai_prompt(etf)
    assert "sh510050" in p and "高确信买入" in p
    assert "3.50%" in p  # 份额5日
    assert "国家队" in p
    # 铁律: A股只能做多, ETF 解读同样禁止做空指令
    assert "不能做空" in p
    p_empty = _etf_ai_prompt([])
    assert "暂无数据" in p_empty


def test_ai_panel_bind_prompt():
    _app()
    from desktop.extra_windows import _AiPanel
    p = _AiPanel("AI 解读 · 测试")
    p.bind_prompt(lambda: "测试 prompt")
    assert p._prompt_fn() == "测试 prompt"
    p.deleteLater()


def test_interpret_tag_no_key_graceful():
    """未配置 API Key 时 interpret_tag 返回 None, 不抛异常 (UI 优雅降级)。"""
    from wyckoff.interpret import interpret_tag
    assert interpret_tag("Spring", {"ai_interpret_enabled": True, "ai_api_key": ""},
                         context="测试语境") is None
    assert interpret_tag("Spring", {"ai_interpret_enabled": False}, context="") is None
    # 未知标签
    assert interpret_tag("不存在的标签", {"ai_interpret_enabled": True,
                                          "ai_api_key": "sk-test"}) is None
