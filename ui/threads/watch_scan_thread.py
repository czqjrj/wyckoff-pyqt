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
        from wyckoff.utils import normalize_symbol
        ok = False
        sig_by_code = {}
        rich = {}

        def _scan_one(c):
            if self._stopped:
                return None
            try:
                from wyckoff.backtest import scan_stock_signals
                sym = normalize_symbol(c)
                # confirm_enabled=False: 不触 K线外的 EM 确认抓取, 无 EM 熔断共享态,
                # 可安全并行 (并发写由 accuracy._snapshot_cb 自带锁保护)
                r = scan_stock_signals(c, datalen=500, confirm_enabled=False,
                                       on_result=AnalysisThread._snapshot_cb)
                return (sym, r)
            except Exception:
                return None

        from wyckoff._shared import parallel_map
        for sym, r in parallel_map(self._codes, _scan_one, workers=6):
            if sym is None:
                continue
            # 只要某只股票成功完成扫描 (返回非 None), 视为本次运行成功
            if r is not None:
                ok = True
            if r and r.get("signals"):
                sig_by_code[sym[2:]] = list(r["signals"])
                rich[sym] = r
        # 末尾: 在后台线程构建头条消息 (避免 UI 线程跑 N*M 次 win_rate_of 被卡)
        msgs = []
        try:
            if not self._stopped:
                from ui.threads.status_ticker import build_ticker_msgs
                msgs = build_ticker_msgs(rich)
        except Exception:
            msgs = []
        self.result.emit((ok, sig_by_code, rich, msgs))
