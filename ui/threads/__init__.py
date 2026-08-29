"""后台线程模块 — 将 MainWindow 中的各类 QThread 子类集中管理。

主要线程类:
- AnalysisThread: 单股票完整分析 (K线/P&F/指标/资金/结论)
- WatchRTThread: 自选股实时行情刷新 + 阶段分类
- LabelAiThread: K线标签 AI 解读后台生成
- StatusTicker: 状态栏滚动头条 (QLabel 子类, 含动画逻辑)
- WatchScanThread: 自选股定时威科夫信号扫描 + 准确度更新
- AutoSyncThread: 校准数据 Git 同步 (pull/merge/push)
- ScanMarketThread: 全市场活跃股列表获取
- AnalysisTickerThread: 分析完成后的近期事件/VSA + 头条构建
"""

from .analysis_thread import AnalysisThread
from .analysis_ticker_thread import AnalysisTickerThread
from .auto_sync_thread import AutoSyncThread
from .entries_scan_thread import EntriesScanThread
from .label_ai_thread import LabelAiThread
from .scan_market_thread import ScanMarketThread
from .status_ticker import StatusTicker, build_ticker_msgs, signal_color
from .watch_rt_thread import WatchRTThread
from .watch_scan_thread import WatchScanThread

__all__ = [
    "AnalysisThread",
    "WatchRTThread",
    "LabelAiThread",
    "StatusTicker",
    "build_ticker_msgs",
    "signal_color",
    "WatchScanThread",
    "AutoSyncThread",
    "ScanMarketThread",
    "AnalysisTickerThread",
    "EntriesScanThread",
]
