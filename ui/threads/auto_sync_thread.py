"""校准数据自动同步后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal


class AutoSyncThread(QThread):
    """后台校准数据同步: pull → merge → 有新增时重训 → push (git 传输在线程内)。"""
    result = pyqtSignal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work

    def run(self):
        try:
            self.result.emit(self._work())
        except Exception as e:
            self.result.emit({"error": str(e)})
