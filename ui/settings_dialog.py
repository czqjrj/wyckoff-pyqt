"""设置对话框: 编辑与 wyckoff.config.DEFAULT_SETTINGS 同键的界面设置 dict。"""
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QVBoxLayout,
)

from .settings_pages import AIPage, BacktestPage, ChartPage, GeneralPage, TTSPage


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(560, 500)
        self._s = dict(settings)

        tabs = QTabWidget(self)
        self._general_page = GeneralPage(self._s)
        self._chart_page = ChartPage(self._s)
        self._backtest_page = BacktestPage(self._s)
        self._ai_page = AIPage(self._s)
        self._tts_page = TTSPage(self._s)

        tabs.addTab(self._general_page, "基本")
        tabs.addTab(self._chart_page, "图表")
        tabs.addTab(self._backtest_page, "回测与风控")
        tabs.addTab(self._ai_page, "AI")
        tabs.addTab(self._tts_page, "语音播报")

        # 已有 Key 但开关未开 (如旧版本只填了 Key) → 打开设置即自动补齐开关,
        # 避免"填了 Key 却报 AI 不可用"的困惑; 用户仍可手动取消。
        if (self._s.get("ai_api_key") or "").strip():
            self._ai_page.cb_interpret.setChecked(True)
            self._ai_page.cb_falsify.setChecked(True)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(btns)

    def _on_accept(self):
        self._general_page.collect()
        self._chart_page.collect()
        self._backtest_page.collect()
        self._ai_page.collect()
        self._tts_page.collect()
        self.accept()

    def settings(self):
        return self._s
