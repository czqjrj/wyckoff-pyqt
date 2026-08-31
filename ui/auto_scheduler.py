"""自动扫描调度器: 从 MainWindow 提取 auto_scan 相关逻辑。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QTimer

from wyckoff._log import log_exc
from wyckoff.settings_keys import S

from . import theme

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class AutoScheduler:
    """自动扫描调度器: 管理自选股定时扫描 / 启动即扫描。
    
    用法:
        scheduler = AutoScheduler(main_window, settings)
        scheduler.schedule()  # 启动定时扫描
        scheduler.startup_scan()  # 启动后一次性扫描
    """

    def __init__(self, main_window: MainWindow, settings: dict) -> None:
        self._mw = main_window
        self._settings = settings
        self._scan_timer: QTimer | None = None
        self._startup_timer: QTimer | None = None

    def schedule(self) -> None:
        """后台定期重算自选股威科夫信号 (设置 auto_scan 时启用)。"""
        self.cancel()
        if not bool(self._settings.get(S.Auto.AUTO_SCAN, False)):
            return
        try:
            interval = max(30, int(self._settings.get(S.Auto.SCAN_INTERVAL, 3600)))
        except (TypeError, ValueError):
            interval = 3600
        timer = QTimer(self._mw)
        timer.setSingleShot(True)
        timer.timeout.connect(self._scan_watchlist)
        timer.start(interval * 1000)
        self._scan_timer = timer

    def cancel(self) -> None:
        """取消定时扫描。"""
        if self._scan_timer is not None:
            self._scan_timer.stop()
        self._scan_timer = None

    def startup_scan(self) -> None:
        """启动后一次性扫描自选股, 立即填充状态栏头条。"""
        try:
            watchlist = getattr(self._mw, "_watchlist", None)
            if not watchlist:
                return
            if self._mw._scan_threads:
                return  # 已在扫描
            self._mw.status_ticker.set_messages(
                [(f"正在扫描 {len(watchlist)} 只自选股...", theme.C_MUTED, "")])
            self._scan_watchlist()
        except Exception as e:
            log_exc("startup_scan 失败", e)

    def _scan_watchlist(self):
        """后台扫描自选股信号 (静默), 完成后重新调度。"""
        self.cancel()
        codes = list(getattr(self._mw, "_watchlist", []))
        if codes:
            try:
                from .threads import WatchScanThread
                th = WatchScanThread(codes, self._mw)
                th.result.connect(self._mw._on_watch_scan)
                self._mw._scan_threads[th] = th
                th.start()
            except Exception as e:
                log_exc("启动定时扫描线程失败", e)
        else:
            self.schedule()
