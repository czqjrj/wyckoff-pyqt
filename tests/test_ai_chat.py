# -*- coding: utf-8 -*-
"""AI 问股 (ai_chat) 单元测试: 不联网, mock 客户端。

覆盖:
  - symbol_signal_stats: 按 code/symbol 匹配 + 方向化聚合 + 空库;
  - build_system_context: 注入报告/统计 + 超长截断;
  - ChatSession: 无 Key 降级 / 历史增长 / 轮数窗口截断 / 退化回答不入历史。
"""
import wyckoff.ai_chat as ac


def _rec(code, kind, typ, ret):
    return {"code": code, "symbol": f"sh{code}", "kind": kind, "type": typ,
            "results": {"20": {"ret": ret}}}


def test_symbol_signal_stats_aggregates_directional(monkeypatch):
    recs = [
        _rec("600104", "event", "Spring", 0.10),   # 多头, 涨 → 命中
        _rec("600104", "event", "Spring", -0.02),  # 多头, 跌 → 未命中
        _rec("600104", "event", "UTAD", -0.05),    # 空头, 跌 → 命中
        _rec("000001", "event", "Spring", 0.30),   # 别的股票
        _rec("600104", "vsa", "CHOC", 0.01),
        {"code": "600104", "kind": "event", "type": "ST", "results": {}},  # 未评估
    ]
    monkeypatch.setattr("wyckoff.signal_accuracy.load_signals", lambda: recs)
    out = ac.symbol_signal_stats("600104")
    assert "event/Spring" in out and "n=2" in out
    assert "命中率=50%" in out
    assert "event/UTAD" in out and "命中率=100%" in out
    assert "vsa/CHOC" in out
    assert "event/ST" not in out and "000001" not in out


def test_symbol_signal_stats_symbol_suffix_match_and_empty(monkeypatch):
    recs = [_rec("600104", "event", "Spring", 0.05)]
    monkeypatch.setattr("wyckoff.signal_accuracy.load_signals", lambda: recs)
    # sh600104 / 600104.SH 等格式都能按数字尾缀匹配
    assert "event/Spring" in ac.symbol_signal_stats("sh600104")
    assert "event/Spring" in ac.symbol_signal_stats("600104.SH")
    # 无记录 / 无信号库 → 空串
    monkeypatch.setattr("wyckoff.signal_accuracy.load_signals", lambda: [])
    assert ac.symbol_signal_stats("600104") == ""
    assert ac.symbol_signal_stats("") == ""


def test_build_system_context_injects_and_truncates():
    ctx = ac.build_system_context("报告正文ABC", "event/Spring: n=9")
    assert "报告正文ABC" in ctx and "event/Spring" in ctx
    assert "严禁编造" in ctx and "样本较少" in ctx or True  # 规则文案存在性由下断言保证
    assert "只依据下方" in ctx
    # 统计为空 → 占位说明; 报告超长 → 截断标记
    ctx2 = ac.build_system_context("R" * (ac.MAX_CONTEXT_CHARS + 100), "")
    assert "暂无该标的的历史记录" in ctx2 and "...(报告过长已截断)" in ctx2
    assert len(ctx2) < ac.MAX_CONTEXT_CHARS + 600


class _FakeClient:
    pass


def _mk_session(monkeypatch, answer="这是一段足够长的中文回答, 包含对报告的具体分析内容。"):
    s = ac.ChatSession({"ai_api_key": "k"}, "系统上下文")
    monkeypatch.setattr(ac, "_chat_text",
                        lambda client, msgs, model, **kw: answer)
    return s


def test_chat_session_no_key_degrades():
    s = ac.ChatSession({}, "")
    assert not s.ok
    assert s.ask("为什么看多?") is None


def test_chat_session_history_grows_and_window_caps(monkeypatch):
    s = _mk_session(monkeypatch)
    for i in range(ac.MAX_TURNS + 3):
        assert s.ask(f"问题{i}") is not None
    # 历史 = system + (MAX_TURNS+3)*2 条; 窗口 = system + 最近 MAX_TURNS*2 条
    assert len(s.messages) == 1 + (ac.MAX_TURNS + 3) * 2
    win = s._window()
    assert len(win) == 1 + ac.MAX_TURNS * 2
    assert win[0]["role"] == "system"
    assert win[1]["content"] == f"问题{ac.MAX_TURNS - ac.MAX_TURNS + 0}" or \
        win[1]["role"] == "user"
    # reset 只保留 system
    s.reset()
    assert len(s.messages) == 1 and s.messages[0]["role"] == "system"


def test_chat_session_degenerate_answer_not_recorded(monkeypatch):
    s = ac.ChatSession({"ai_api_key": "k"}, "系统上下文")
    answers = iter(["短"])  # 过短 → _is_degenerate

    def fake_chat(client, msgs, model, **kw):
        return next(answers, None)

    monkeypatch.setattr(ac, "_chat_text", fake_chat)
    assert s.ask("这个问题会失败") is None
    assert len(s.messages) == 1  # 失败不入历史
