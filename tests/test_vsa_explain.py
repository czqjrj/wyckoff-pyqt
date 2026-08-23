"""VSA 信号解释系统测试: 覆盖全部标签、字段完整性、去重前缀与摘要。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.vsa import _PRIORITY
from wyckoff.vsa_explain import (
    DIR_SYMBOL,
    EVENT_EXPLAIN,
    LONG_ONLY_NOTE,
    VSA_EXPLAIN,
    explain,
    explain_lines,
    explain_summary,
    meaning_pure,
)


def test_all_labels_explained():
    """vsa.py 全部 20 类标签都应有关联解释。"""
    missing = set(_PRIORITY) - set(VSA_EXPLAIN)
    assert not missing, f"以下标签无解释: {missing}"
    assert len(VSA_EXPLAIN) >= len(_PRIORITY)


def test_all_events_explained():
    """events.py 全部事件标签 (EVENT_COLORS) 都应有关联解释。"""
    from wyckoff.config import EVENT_COLORS
    for lb in EVENT_COLORS:
        assert explain(lb), f"事件 {lb} 无解释"
    missing = set(EVENT_COLORS) - set(EVENT_EXPLAIN) - set(VSA_EXPLAIN)
    assert not missing, f"事件无解释: {missing}"


def test_event_explain_lines():
    """事件标签解释行格式与 VSA 一致。"""
    for lb in EVENT_EXPLAIN:
        lines = explain_lines(lb)
        assert len(lines) == 6, f"{lb} 解释行数异常"
        assert lines[0].startswith("含义:")
        assert lines[-1].startswith("失效:")
        s = explain_summary(lb)
        assert s.startswith(tuple(DIR_SYMBOL.values())), f"{lb} 摘要无方向符号"


def test_field_completeness():
    """每类解释都应含 四要素+流程角色+失效条件 (6字段)。"""
    required = ("meaning", "direction", "watch", "advice", "role", "fail")
    for lb, e in VSA_EXPLAIN.items():
        for k in required:
            assert e.get(k), f"{lb}.{k} 缺失"
            # direction 可为纯方向词 (2字), 其余字段应提供足够说明
            min_len = 2 if k == "direction" else 6
            assert len(str(e[k])) >= min_len, f"{lb}.{k} 内容过短"


def test_direction_valid():
    """direction 以 偏多/偏空/中性 之一开头, 且 DIR_SYMBOL 可映射。"""
    for lb, e in VSA_EXPLAIN.items():
        d = e["direction"]
        assert any(d.startswith(k) for k in DIR_SYMBOL), \
            f"{lb} 方向非法: {d}"


def test_meaning_no_dup_prefix():
    """含义不应以标签中文名开头 (避免与结论区 VSA_CN 重复)。"""
    from wyckoff.config import VSA_CN
    for lb, e in VSA_EXPLAIN.items():
        cn = VSA_CN.get(lb, lb)
        pure = meaning_pure(e["meaning"])
        assert not pure.startswith(cn), f"{lb} 含义与中文名重复: {pure[:20]}"
        # meaning_pure 应已剥离冗余标签词前缀
        assert not pure.startswith(("强势需求:", "强势供给:", "无需求:",
                                    "无供给:", "停止量:", "上冲量:", "弹簧量:")), \
            f"{lb} meaning_pure 未剥离前缀"


def test_explain_lines_format():
    """explain_lines 返回 6 行, 每行带字段标签。"""
    lines = explain_lines("DEM")
    assert len(lines) == 6
    assert lines[0].startswith("含义:")
    assert lines[1].startswith("方向:")
    assert lines[-1].startswith("失效:")


def test_explain_unknown():
    assert explain("NOPE") is None
    assert explain_lines("NOPE")[0].startswith("未收录")


def test_advice_no_shorting_directives():
    """A股单边做多市场: 所有『建议』字段不得出现做空指令。"""
    banned = ("做空", "放空", "开空", "裸卖空", "空头可", "空头入场",
              "空头回补", "空头可布局", "空头减仓")
    for lb, e in VSA_EXPLAIN.items():
        a = e["advice"]
        assert not any(b in a for b in banned), f"{lb} 建议含做空指令: {a}"
    for lb, e in EVENT_EXPLAIN.items():
        a = e["advice"]
        assert not any(b in a for b in banned), f"{lb} 建议含做空指令: {a}"


def test_long_only_note_present():
    """A股单边做多提示常量存在且可附加到解释行末尾。"""
    assert LONG_ONLY_NOTE and "单边做多" in LONG_ONLY_NOTE
    lines = explain_lines("DEM") + [LONG_ONLY_NOTE]
    assert lines[-1] == LONG_ONLY_NOTE


def test_explain_summary():
    s = explain_summary("DEM")
    assert s.startswith(DIR_SYMBOL["偏多"])
    assert "高量" in s or "买盘" in s


def test_all_signals_in_conclusion():
    """结论区 VSA 信号段应能引用解释 (build_conclusion 不抛错)。"""
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import pandas as pd

    from wyckoff.conclusion import build_conclusion
    from wyckoff.indicators import add_indicators
    from wyckoff.vsa import vsa_classify

    np.random.seed(1)
    n = 400
    close = np.linspace(50, 90, n) + np.random.randn(n) * 0.2
    o = close + np.random.randn(n) * 0.3
    h = np.maximum(o, close) + np.abs(np.random.randn(n) * 0.7) + 0.1
    l = np.minimum(o, close) - np.abs(np.random.randn(n) * 0.7) - 0.1
    vol = np.random.rand(n) * 2e6 + 1e5
    df = add_indicators(pd.DataFrame({
        "open": o, "close": close, "high": h, "low": l, "volume": vol,
        "day": pd.date_range("2024-01-01", periods=n),
    }), symbol="600104")
    sigs = vsa_classify(df, scale=240)
    assert sigs
    sections = build_conclusion(df, pivots=[], events=[], phase="底部整固",
                                detail="低位收敛筑底", vsa_signals=sigs)
    titles = [t for t, _ in sections]
    assert "VSA 信号" in titles
    vsa_lines = next(l for t, l in sections if t == "VSA 信号")
    joined = "\n".join(vsa_lines)
    assert "信号解释:" in joined
    assert "失效:" in joined
    assert "流程:" in joined
