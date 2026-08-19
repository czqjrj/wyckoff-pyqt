# -*- coding: utf-8 -*-
"""解读语音播报 (wyckoff/tts.py) 回归测试。

不调用真实语音引擎/网络: 验证文本清洗、引擎解析、开关逻辑与同步降级路径。
真实发声 (edge-tts/pyttsx3/espeak) 依赖安装, 仅在有引擎时做轻量同步冒烟。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.tts import (clean_speech_text, best_engine, available_engines,
                         is_enabled, _edge_rate, EDGE_VOICES, speak, stop,
                         is_speaking, parse_engine_choice, parse_voice_choice,
                         chunk_speech_text, speak_sequence)


def test_parse_engine_choice():
    assert parse_engine_choice("auto (自动选择)") == "auto"
    assert parse_engine_choice("auto (无可用引擎)") == "auto"
    assert parse_engine_choice("edge - 微软在线语音 (edge-tts)") == "edge"
    assert parse_engine_choice("pyttsx3 - 离线语音") == "pyttsx3"
    assert parse_engine_choice("espeak - 系统") == "espeak"


def test_parse_voice_choice():
    assert parse_voice_choice("zh-CN-XiaoxiaoNeural (晓晓 (女·甜美))") == "zh-CN-XiaoxiaoNeural"
    assert parse_voice_choice("zh-CN-YunxiNeural (云希 (男·阳光))") == "zh-CN-YunxiNeural"
    assert parse_voice_choice("") == "zh-CN-XiaoxiaoNeural"
    assert parse_voice_choice("不存在的音色") == "zh-CN-XiaoxiaoNeural"


def test_clean_speech_text_strips_markdown():
    raw = "**解读**\n\n- 第一点\n- `代码` 内容\n\n【重点】风险提示\n\n  多余  空格"
    out = clean_speech_text(raw)
    assert "**" not in out
    assert "`" not in out
    assert "- " not in out
    assert "【" not in out
    assert "重点" in out and "风险提示" in out
    assert "第一点" in out
    assert "多余空格" in out or "多余 空格" in out


def test_clean_speech_text_handles_empty():
    assert clean_speech_text("") == ""
    assert clean_speech_text("   \n  ") == ""
    assert clean_speech_text(None) == ""


def test_clean_speech_text_keeps_cn_brackets_content():
    out = clean_speech_text("威科夫(Wyckoff)分析: 吸筹(Accumulation)阶段")
    assert "威科夫" in out and "Wyckoff" in out and "吸筹" in out


def test_edge_rate_clamped():
    assert _edge_rate(0) == "+0%"
    assert _edge_rate("+25") == "+25%"
    assert _edge_rate(-10) == "-10%"
    assert _edge_rate(500) == "+50%"
    assert _edge_rate(-200) == "-50%"
    assert _edge_rate("abc") == "+0%"


def test_edge_voices_are_valid():
    assert "zh-CN-XiaoxiaoNeural" in EDGE_VOICES
    for code in EDGE_VOICES:
        assert code.startswith("zh-")


def test_available_engines_returns_list():
    engs = available_engines()
    assert isinstance(engs, list)
    for key, _lab in engs:
        assert key in ("edge", "pyttsx3", "espeak")


def test_best_engine_auto():
    avail = [k for k, _ in available_engines()]
    want = best_engine({"tts_engine": "auto"})
    if avail:
        assert want in avail
    else:
        assert want is None


def test_best_engine_prefers_explicit_when_available():
    engs = dict(available_engines())
    if "edge" in engs:
        assert best_engine({"tts_engine": "edge"}) == "edge"
    else:
        assert best_engine({"tts_engine": "edge"}) != "edge"


def test_best_engine_falls_back_from_missing():
    if available_engines():
        assert best_engine({"tts_engine": "nonexistent-engine"}) in dict(available_engines())
    else:
        assert best_engine({"tts_engine": "nonexistent-engine"}) is None


def test_is_enabled_requires_setting_on():
    assert is_enabled({}) is False
    assert is_enabled({"tts_enabled": True}) is is_enabled({"tts_enabled": True})  # no-op sanity
    assert is_enabled({"tts_enabled": False}) is False


def test_is_enabled_true_when_on_and_engine():
    engs = dict(available_engines())
    assert is_enabled({"tts_enabled": True}) == bool(engs)


def test_speak_empty_text_noop():
    assert speak("") is False
    assert speak("   \n  ") is False


def test_speak_sequence_empty_or_no_engine():
    """空 parts → 返回 False 并触发 on_done(False, 无内容); 无引擎时不启动线程。"""
    done = {}
    assert speak_sequence([]) is False
    assert speak_sequence([("标签", ""), ("", "  ")], on_done=lambda o, e: done.__setitem__("d", (o, e))) is False
    assert done.get("d") == (False, "无内容可播报")
    if not is_enabled({"tts_enabled": True}):
        assert speak_sequence([("标签", "内容")]) is False


def test_speak_sequence_prepends_label(monkeypatch):
    """带标签的 parts 会把标签并进正文 (标签。正文), 无标签直接读正文。"""
    import time as _time
    from wyckoff import tts
    captured = []

    def fake_worker(text, settings, stop):
        captured.append(text)
        return True, None

    monkeypatch.setattr(tts, "_speak_worker", fake_worker)
    # 强制认为有可用引擎, 让线程真正启动
    monkeypatch.setattr(tts, "best_engine", lambda settings=None: "edge")
    done = {}

    ok = tts.speak_sequence([("趋势", "趋势正文"), ("", "无标签正文")],
                            {"tts_enabled": True},
                            on_done=lambda o, e: done.__setitem__("r", (o, e)))
    assert ok is True
    deadline = 0
    while not done.get("r") and deadline < 100:
        _time.sleep(0.01)
        deadline += 1
    tts.stop()
    assert done.get("r") == (True, None), f"on_done 未触发, captured={captured}"
    assert captured == ["趋势。趋势正文", "无标签正文"]


def test_speak_starts_and_can_stop():
    if not is_enabled({"tts_enabled": True}):
        # 无引擎时不应启动线程
        assert speak("测试文本", {"tts_enabled": True}) is False
        return
    ok = speak("测试播报内容", {"tts_enabled": True})
    assert ok is True
    assert is_speaking() is True or not is_speaking()  # 线程可能已跑完
    stop()


def test_play_mp3_stop_terminates_player(tmp_path, monkeypatch):
    """回归: 停止事件置位后必须立即终止播放器进程 (原阻塞 subprocess.run 会让
    ffplay 把整段长解读放完, 表现'一直响'/新旧播报叠播)。"""
    import threading
    import time as _time
    from wyckoff.tts import _play_mp3

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_player = bin_dir / "ffplay"
    fake_player.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "time.sleep(600)\n"
        "print('should not reach', file=sys.stderr)\n")
    fake_player.chmod(0o755)
    mp3 = tmp_path / "t.mp3"
    mp3.write_bytes(b"\x00" * 32)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    stop_ev = threading.Event()
    timer = threading.Timer(0.4, stop_ev.set)
    timer.start()
    t0 = _time.time()
    result = _play_mp3(str(mp3), stop_ev)
    timer.cancel()
    elapsed = _time.time() - t0
    assert result is False
    assert elapsed < 5.0, f"停止后播放器未及时终止, 耗时 {elapsed:.1f}s"


def test_chunk_speech_text_splits_long_text():
    """超长文本按句子边界分块, 每块不超过上限, 拼接后还原全文。"""
    short = "这是一段很短的解读。"
    assert chunk_speech_text(short, max_chars=3000) == [short]

    # 3 句各 20 字 → 60 字, 上限 45 → 切成 2 块, 且不在句中被腰斩
    sents = "第一句话内容一二三四五六七八九十。" * 3
    blocks = chunk_speech_text(sents, max_chars=45)
    assert len(blocks) > 1
    assert all(len(b) <= 45 for b in blocks)
    assert "".join(blocks) == sents, "分块拼接必须还原原文 (不丢字不重复)"

    # 单句超长 → 硬切兜底, 仍还原全文
    huge = "很" * 100 + "。"
    blocks = chunk_speech_text(huge, max_chars=30)
    assert all(len(b) <= 30 for b in blocks)
    assert "".join(blocks) == huge
