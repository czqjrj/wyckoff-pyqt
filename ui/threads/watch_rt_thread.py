"""自选股实时行情刷新后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal

from wyckoff._log import log_exc


class WatchRTThread(QThread):
    result = pyqtSignal(dict, dict)

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = codes

    def run(self):
        from wyckoff.backtest import classify_phase
        from wyckoff.datasource import fetch_realtime
        from wyckoff.utils import normalize_symbol
        rt = {}
        try:
            rt = fetch_realtime(self._codes) or {}
        except Exception as e:
            log_exc("自选股实时行情刷新失败", e)
            rt = {}
        phases = {}
        for c in self._codes:
            try:
                normalize_symbol(c)
                cls = classify_phase(c)
                if cls:
                    phases[c] = cls
            except Exception:
                continue
        self.result.emit(rt, phases)
