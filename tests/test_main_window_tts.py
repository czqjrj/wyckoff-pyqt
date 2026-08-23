"""解读页语音朗读按钮 (MainWindow) 离屏测试。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def win():
    try:
        app = _app()
        import desktop.main_window as mw
        orig_load = mw.load_settings
        orig_watch = mw.load_watchlist
        safe = {**mw.load_settings(),
                "default_load": "",
                "auto_refresh": False,
                "refresh_interval": 0}
        mw.load_settings = lambda: safe
        mw.load_watchlist = lambda: []
        from desktop.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 不可用: {e}")
    w = MainWindow()
    w.resize(1600, 900)
    w.show()
    app.processEvents()
    yield app, w
    w.close()
    app.processEvents()
    mw.load_settings = orig_load
    mw.load_watchlist = orig_watch


def test_tts_btn_initial(win):
    """右侧分析结论栏有语音朗读按钮, 初始为未播放状态。"""
    app, w = win
    assert hasattr(w, "tts_btn")
    assert w.tts_btn.text() == "▶ 语音朗读"
    assert not w._tts_playing
    assert w.tts_btn.toolTip()


def test_tts_text_joins_sections(win):
    """朗读文本 = 当前选中标签的正文; 空态/占位不计入。"""
    app, w = win
    w._render_sections([("趋势", ["趋势解读第一行", "趋势解读第二行"]),
                        ("量价", ["量价解读内容"])])
    assert w._section_texts == ["趋势解读第一行\n趋势解读第二行",
                                "量价解读内容"]
    assert w.section_list.currentRow() == 0, "默认选中第一个标签"
    assert w._tts_parts() == [("趋势", "趋势解读第一行\n趋势解读第二行")]

    w.section_list.setCurrentRow(1)
    assert w._tts_parts() == [("量价", "量价解读内容")]

    w._render_sections([])
    assert w._section_texts == []
    assert w._tts_parts() == []


def test_tts_disabled_click_noop(win):
    """TTS 未启用时点击只提示, 不进入播放状态。"""
    app, w = win
    w.settings["tts_enabled"] = False
    w._on_tts_click()
    app.processEvents()
    assert not w._tts_playing
    assert w.tts_btn.text() == "▶ 语音朗读"


def test_tts_play_and_done(win, monkeypatch):
    """启用后点击启动逐标签顺序播报; 完成回调回到未播放状态。"""
    import wyckoff.tts as tts
    app, w = win
    called = {}

    def fake_is_enabled(settings=None):
        return True

    def fake_speak_sequence(parts, settings=None, on_done=None):
        called["parts"] = parts
        if on_done:
            on_done(True, None)
        return True

    monkeypatch.setattr(tts, "is_enabled", fake_is_enabled)
    monkeypatch.setattr(tts, "speak_sequence", fake_speak_sequence)
    w._render_sections([("趋势", ["趋势解读"]), ("量价", ["量价解读"])])
    w.settings["tts_enabled"] = True
    w.section_list.setCurrentRow(1)
    w._on_tts_click()
    app.processEvents()
    assert called["parts"] == [("量价", "量价解读")], "切换标签后只朗读当前标签内容"
    assert not w._tts_playing, "完成后应回到未播放状态"
    assert w.tts_btn.text() == "▶ 语音朗读"


def test_tts_click_again_stops(win, monkeypatch):
    """播放中再次点击 → 调用 stop 并回到未播放状态。"""
    import wyckoff.tts as tts
    app, w = win
    stopped = {"n": 0}

    def fake_stop():
        stopped["n"] += 1

    monkeypatch.setattr(tts, "stop", fake_stop)
    w._tts_playing = True
    w._sync_tts_btn()
    assert w.tts_btn.text() == "■ 停止"
    w._on_tts_click()
    app.processEvents()
    assert stopped["n"] == 1
    assert not w._tts_playing
    assert w.tts_btn.text() == "▶ 语音朗读"


# ── 解读页 (AI解读) 语音朗读 ──

def test_interp_tts_btn_initial(win):
    """解读页有独立的语音朗读按钮, 与右侧按钮同步状态。"""
    app, w = win
    assert hasattr(w, "interp_tts_btn")
    assert w.interp_tts_btn.text() == "▶ 语音朗读"
    assert w.interp_tts_btn.toolTip()


def test_interp_tts_text_uses_browser(win):
    """朗读文本取自解读页浏览器内容。"""
    app, w = win
    w.interp_text.setPlainText("AI解读第一行\nAI解读第二行")
    assert w._interp_tts_text() == "AI解读第一行\nAI解读第二行"
    w.interp_text.clear()
    assert w._interp_tts_text() == ""


def test_interp_tts_play_and_done(win, monkeypatch):
    """启用后点击解读页按钮启动播报并回调完成; 长文本不被小上限截断。"""
    import wyckoff.tts as tts
    app, w = win
    called = {}

    def fake_is_enabled(settings=None):
        return True

    def fake_speak(text, settings=None, on_done=None):
        called["text"] = text
        if on_done:
            on_done(True, None)
        return True

    monkeypatch.setattr(tts, "is_enabled", fake_is_enabled)
    monkeypatch.setattr(tts, "speak", fake_speak)
    long_text = "AI解读内容" * 120
    w.interp_text.setPlainText(long_text)
    w.settings["tts_enabled"] = True
    w.settings["tts_max_chars"] = 800
    w._on_interp_tts_click()
    app.processEvents()
    assert called["text"] == long_text, "AI 解读朗读不应受 800 字上限截断"
    assert not w._tts_playing, "完成后应回到未播放状态"
    assert w.interp_tts_btn.text() == "▶ 语音朗读"


def test_interp_tts_syncs_both_buttons(win):
    """状态同步同时更新右侧按钮与解读页按钮。"""
    app, w = win
    w._tts_playing = True
    w._sync_tts_btn()
    assert w.tts_btn.text() == "■ 停止"
    assert w.interp_tts_btn.text() == "■ 停止"
    w._tts_playing = False
    w._sync_tts_btn()
    assert w.tts_btn.text() == "▶ 语音朗读"
    assert w.interp_tts_btn.text() == "▶ 语音朗读"


def test_interp_tts_no_content_noop(win, monkeypatch):
    """解读内容为空时点击只提示, 不启动播报。"""
    import wyckoff.tts as tts
    app, w = win
    monkeypatch.setattr(tts, "is_enabled", lambda settings=None: True)
    spoke = {"n": 0}
    monkeypatch.setattr(tts, "speak",
                        lambda text, settings=None, on_done=None: spoke.__setitem__("n", spoke["n"] + 1) or True)
    w.interp_text.clear()
    w._on_interp_tts_click()
    app.processEvents()
    assert spoke["n"] == 0
    assert not w._tts_playing
