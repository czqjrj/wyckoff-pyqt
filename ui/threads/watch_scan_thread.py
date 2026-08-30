"""自选股定时威科夫信号扫描后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal

from ui.threads.analysis_thread import AnalysisThread


class WatchScanThread(QThread):
    """后台定时扫描自选股信号: 重算威科夫事件 + 构建 ticker 头条消息 (一并搬后台, 防 UI 线程被 win_rate_of 循环卡住)。

    结果: (ok, sig_by_code, rich, msgs) — msgs 可直接丢给 status_ticker.set_messages。
    """
    result = pyqtSignal(object)

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = codes
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        from wyckoff.backtest import _EM_LOCK, scan_stock_signals
        from wyckoff.utils import normalize_symbol
        ok = False
        sig_by_code = {}
        rich = {}
        for c in self._codes:
            if self._stopped:
                break
            try:
                sym = normalize_symbol(c)
                with _EM_LOCK:
                    r = scan_stock_signals(c, datalen=500, confirm_enabled=False,
                                           on_result=AnalysisThread._snapshot_cb)
                ok = True
                if r and r.get("signals"):
                    sig_by_code[sym[2:]] = list(r["signals"])
                    rich[sym] = r
            except Exception:
                continue
        # 末尾: 在后台线程构建头条消息 (避免 UI 线程跑 N*M 次 win_rate_of 被卡)
        msgs = []
        try:
            if not self._stopped:
                from ui.threads.status_ticker import build_ticker_msgs
                msgs = build_ticker_msgs(rich)
        except Exception:
            msgs = []
        self.result.emit((ok, sig_by_code, rich, msgs))
