"""今日入场点扫描后台线程 (自选股 / 全市场活跃宇宙)。

唯一实现 (此前与 extra_windows 内嵌版重复); 基于 ScanThreadBase。"""
from PyQt6.QtCore import pyqtSignal

from ..components.workers import ScanThreadBase


class EntriesScanThread(ScanThreadBase):
    """按 scope 扫描确认制入场点。

    scope: "watch" / "top500" / "top1500"
    result 发送最终排序行列表; rows_found 增量命中行 (流式上表);
    error 发送失败文本。
    """
    rows_found = pyqtSignal(object)

    def __init__(self, scope, parent=None):
        super().__init__(parent)
        self._scope = scope

    def do_scan(self):
        if self._scope == "watch":
            from wyckoff.storage import load_watchlist
            codes = load_watchlist()
        else:
            from wyckoff.entries import market_universe
            codes = market_universe(500 if self._scope == "top500" else 1500)
        if not codes:
            self.error.emit("股票宇宙为空 (自选股未添加, 或全市场列表获取失败)")
            return []
        from wyckoff.entries import scan_entries_parallel
        return scan_entries_parallel(
            codes, workers=8,
            progress=lambda d, t, c: self.progress.emit(d, t, c),
            stopped=lambda: self.stopped,
            on_rows=lambda hit: self.rows_found.emit(hit))
