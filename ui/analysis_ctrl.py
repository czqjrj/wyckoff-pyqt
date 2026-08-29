"""分析控制器: 从 MainWindow 提取分析启动/完成/错误处理逻辑。"""
from __future__ import annotations

from datetime import datetime as _dt
from typing import Any, TYPE_CHECKING

from wyckoff._log import log_exc, log_msg
from wyckoff.analysis import run_analysis
from wyckoff.settings_keys import S
from wyckoff.storage import save_settings

from . import theme
from .threads import AnalysisThread, AnalysisTickerThread
from .chart_manager import ChartManager

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class AnalysisController:
    """分析控制器: 管理分析启动/完成/错误处理/缓存。
    
    用法:
        ctrl = AnalysisController(main_window, settings, chart_manager)
        ctrl.start_analysis(code, force_refresh=False)
    """
    
    def __init__(self, main_window: MainWindow, settings: dict, chart_manager: ChartManager) -> None:
        self._mw = main_window
        self._settings = settings
        self._chart_mgr = chart_manager
        self._analyzing = False
        self._thread: AnalysisThread | None = None
        self._analysis_threads: dict[str, AnalysisThread] = {}
        self._analysis_cache: dict[str, Any] = {}
        self._analysis_ticker_th: AnalysisTickerThread | None = None
    
    @property
    def analyzing(self) -> bool:
        return self._analyzing
    
    def start_analysis(self, code: str, force_refresh: bool = False) -> None:
        """启动分析。"""
        if self._analyzing or not code:
            return
        self._analyzing = True
        self._mw.btn_analyze.setEnabled(False)
        self._sync_analyze_btn(True)
        
        scale_key = self._mw.cb_scale.currentText()
        period_key = self._mw.cb_period.currentText()
        from wyckoff.config import SCALE_OPTIONS, PERIOD_OPTIONS
        scale = SCALE_OPTIONS.get(scale_key, 240)
        datalen = PERIOD_OPTIONS.get(period_key, 700)
        self._mw._status(f"正在分析 {code} ({scale_key}/{period_key}) ...", theme.C_AMBER)
        
        # 设置图表占位
        self._chart_mgr.set_placeholder(f"正在分析 {code} … 完成后自动刷新")
        
        self._thread = AnalysisThread(code, datalen, scale, self._settings, force_refresh)
        self._thread.done.connect(self._on_done)
        self._thread.failed.connect(self._on_error)
        self._analysis_threads[self._thread] = self._thread
        self._thread.finished.connect(
            lambda: self._analysis_threads.pop(self._thread, None))
        self._thread.start()
    
    def _sync_analyze_btn(self, analyzing):
        """切换"开始分析"按钮的 分析中/空闲 状态。"""
        b = self._mw.btn_analyze
        b.setText("正在分析中…" if analyzing else "开始分析")
        b.setProperty("analyzing", analyzing)
        b.style().unpolish(b)
        b.style().polish(b)
    
    def _on_done(self, r):
        """分析完成回调: 委托给主窗口做完整渲染 (图表 + 结论面板)。"""
        self._analyzing = False
        self._mw._done(r)
    
    def _on_error(self, msg, tb):
        """分析失败回调。"""
        self._analyzing = False
        self._mw.btn_analyze.setEnabled(True)
        self._sync_analyze_btn(False)
        # 设置错误占位
        self._chart_mgr.set_error_placeholder()
        self._mw._error(msg, tb)
    
    def get_cached(self, code, scale_key, period_key):
        """获取缓存的分析结果。"""
        from wyckoff.config import SCALE_OPTIONS, PERIOD_OPTIONS
        key = (code, SCALE_OPTIONS.get(scale_key, 240), PERIOD_OPTIONS.get(period_key, 700))
        return self._analysis_cache.get(key)
    
    def push_ticker(self, r):
        """刚分析的标的若有高实测命中信号 → 并入状态栏头条。"""
        try:
            df = r.get("df")
            code = r.get("code") or ""
            if df is None or not code:
                return
            name = r.get("name") or ""
            prev = self._analysis_ticker_th
            if prev is not None and prev.isRunning():
                try:
                    prev.stop()
                except Exception:
                    pass
            th = AnalysisTickerThread(df, code, name, self._mw)
            th.result.connect(self._on_ticker_msgs)
            self._analysis_ticker_th = th
            th.start()
        except Exception as e:
            log_exc("push_ticker 启动线程失败", e)
    
    def _on_ticker_msgs(self, msgs):
        if msgs:
            try:
                self._mw.status_ticker.add_messages(msgs)
            except Exception as e:
                log_exc("_on_ticker_msgs 状态栏写消息失败", e)
