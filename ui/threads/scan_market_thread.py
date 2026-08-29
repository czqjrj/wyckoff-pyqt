"""全市场活跃股列表获取后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal


class ScanMarketThread(QThread):
    """后台获取全市场活跃股列表 (东财成交额Top) — 网络请求不阻塞 UI 线程。

    结果: (codes, title); 抓取失败 codes=None。
    """
    result = pyqtSignal(object, str)

    def run(self):
        try:
            from wyckoff.backtest import MARKET_UNIVERSE
            from wyckoff.fundamental import universe
            codes, src = universe(100)
            if not codes:
                codes, src = MARKET_UNIVERSE, "builtin"
            label = {"top": "成交额Top", "local": "本地抽样",
                     "builtin": "内置"}.get(src, "宇宙") + str(len(codes))
            self.result.emit(codes, f"全市场风格扫描 ({label})")
        except Exception:
            self.result.emit(None, "")
