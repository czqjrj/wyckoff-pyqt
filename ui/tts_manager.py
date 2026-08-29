"""语音播报管理器: 从 MainWindow 提取 TTS 相关逻辑。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtWidgets import QPushButton

from wyckoff._log import log_exc
from wyckoff.settings_keys import S

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class TtsManager(QObject):
    """语音播报管理器: 统一管理 K线标签 / AI解读 的 TTS 播放。
    
    用法:
        tts = TtsManager(main_window, settings)
        tts.sync_btn()  # 同步按钮状态
        tts.on_click()  # 处理点击事件
    """
    
    # 完成信号 (跨线程)
    done_sig = pyqtSignal(bool, str)
    
    def __init__(self, main_window: MainWindow, settings: dict) -> None:
        super().__init__(main_window)
        self._mw = main_window
        self._settings = settings
        self._playing = False
        self._btns: list[QPushButton] = []
        self.done_sig.connect(self._on_done)
    
    @property
    def playing(self) -> bool:
        return self._playing
    
    def register_btn(self, btn: QPushButton) -> None:
        """注册需要同步状态的 TTS 按钮。"""
        self._btns.append(btn)
    
    def sync_btn(self) -> None:
        """同步所有 TTS 按钮的文本/样式。"""
        for b in self._btns:
            b.setText("■ 停止" if self._playing else "▶ 语音朗读")
            b.setProperty("playing", self._playing)
            b.style().unpolish(b)
            b.style().polish(b)
    
    def _cap_text(self, text: str, force_cap: int | None = None) -> str:
        """按 tts_max_chars 限制播报字数, 尽量在句末截断。"""
        cap = int(force_cap if force_cap is not None
                  else self._settings.get(S.TTS.MAX_CHARS, 3000) or 0)
        if cap <= 0 or len(text) <= cap:
            return text
        cut = text[:cap]
        for sep in ("。", "！", "？", "；", "\n"):
            idx = cut.rfind(sep)
            if idx > cap // 2:
                return cut[:idx + 1]
        return cut
    
    def get_parts(self):
        """返回当前选中标签的朗读文本 [(标题, 正文), ...]。"""
        mw = self._mw
        if getattr(mw, "_sections_empty", True):
            return []
        titles = getattr(mw, "_section_titles", [])
        texts = getattr(mw, "_section_texts", [])
        row = mw.section_list.currentRow() if mw.section_list.count() > 0 else 0
        if 0 <= row < len(texts):
            t = (texts[row] or "").strip()
            if t:
                title = titles[row] if row < len(titles) else ""
                return [(title, t)]
        if not texts:
            cur = mw.section_text.toPlainText().strip()
            if cur:
                return [("", cur)]
        return []
    
    def get_interp_text(self):
        """获取 AI 解读文本。"""
        return (self._mw.interp_text.toPlainText() or "").strip()
    
    def on_click(self):
        """处理语音播报按钮点击。"""
        mw = self._mw
        if self._playing:
            self.stop()
            mw._status("语音播报已停止")
            return
        
        from wyckoff.tts import is_enabled, speak_sequence
        if not is_enabled(self._settings):
            mw._status("语音未启用: 请在 设置→语音播报 中启用并配置引擎")
            return
        
        parts = self.get_parts()
        if not parts:
            mw._status("暂无可朗读的解读内容")
            return
        
        capped = [(t, self._cap_text(x)) for t, x in parts]
        self._playing = True
        self.sync_btn()
        ok = speak_sequence(capped, self._settings,
                           on_done=lambda ok_, err: self.done_sig.emit(ok_, err))
        if not ok:
            self._playing = False
            self.sync_btn()
            mw._status("语音播报未能启动: 无可用引擎")
    
    def on_interp_click(self):
        """处理 AI 解读语音播报按钮点击。"""
        mw = self._mw
        if self._playing:
            self.stop()
            mw._status("语音播报已停止")
            return
        
        from wyckoff.tts import is_enabled, speak
        if not is_enabled(self._settings):
            mw._status("语音未启用: 请在 设置→语音播报 中启用并配置引擎")
            return
        
        text = self.get_interp_text()
        if not text:
            mw._status("暂无可朗读的 AI 解读")
            return
        
        text = self._cap_text(text, force_cap=6000)
        self._playing = True
        self.sync_btn()
        ok = speak(text, self._settings,
                  on_done=lambda ok_, err: self.done_sig.emit(ok_, err))
        if not ok:
            self._playing = False
            self.sync_btn()
            mw._status("语音播报未能启动: 无可用引擎")
    
    def stop(self):
        """停止语音播报。"""
        from wyckoff.tts import stop
        stop()
        self._playing = False
        self.sync_btn()
    
    def _on_done(self, ok, err):
        self._playing = False
        self.sync_btn()
        if ok:
            self._mw._status("语音播报完成")
        elif err:
            self._mw._status(f"语音播报失败: {err}")
