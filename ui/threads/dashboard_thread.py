"""大盘仪表盘后台刷新线程。"""
from PyQt6.QtCore import QThread, pyqtSignal

from wyckoff._log import log_exc


class DashboardThread(QThread):
    """后台聚合仪表盘全量数据, 完成后发射 result(dict)。"""
    result = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            from wyckoff.market_dashboard import build_dashboard_data
            data = build_dashboard_data()
            self.result.emit(data or {})
        except Exception as e:
            log_exc("仪表盘数据聚合失败", e)
            self.result.emit({})
