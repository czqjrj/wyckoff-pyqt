"""K线标签 AI 解读后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal

from wyckoff._log import log_exc


class LabelAiThread(QThread):
    """后台生成 K 线标签的 AI 解读 (避免阻塞对话框)。"""
    result = pyqtSignal(object)

    def __init__(self, work_fn, parent=None):
        super().__init__(parent)
        self._work = work_fn

    def run(self):
        try:
            out = self._work()
        except Exception as e:
            log_exc("AI 解读后台任务失败", e)
            out = None
        self.result.emit(out)
