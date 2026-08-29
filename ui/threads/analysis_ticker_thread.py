"""分析完成后的近期事件/VSA + 头条构建后台线程。"""
from PyQt6.QtCore import QThread, pyqtSignal


class AnalysisTickerThread(QThread):
    """后台线程: 对刚分析完的标的做近期事件/VSA + ticker 头条构建。

    原本这段(find_pivots / detect_all / vsa_classify / build_ticker_msgs)
    在 UI 线程里跑, 几百根 K 线下会让状态栏/整个 UI 卡几十毫秒~几百毫秒。
    挪到后台后, UI 线程只收到 msgs 并 add_messages, 耗时 <1ms。
    """
    result = pyqtSignal(object)  # msgs: List[(text, color, code)]

    def __init__(self, df, code, name, parent=None):
        super().__init__(parent)
        self._df = df
        self._code = code
        self._name = name
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        if self._stopped or self._df is None or not self._code:
            self.result.emit([])
            return
        try:
            from wyckoff.events import detect_all
            from wyckoff.indicators import find_pivots
            from wyckoff.vsa import vsa_classify
            df = self._df
            code = self._code
            name = self._name or str(code)[-6:]
            pivots = find_pivots(df, order=6)
            events = detect_all(df, pivots)
            if self._stopped:
                self.result.emit([])
                return
            recent_e = [e for e in events if e["idx"] >= len(df) - 20]
            recent_v = [s for s in vsa_classify(df, scale=240)
                        if s["idx"] >= len(df) - 20]
            if self._stopped:
                self.result.emit([])
                return
            rich = {code: {
                "name": name,
                "events": [{"type": e["type"], "date": str(e["date"].date()),
                            "price": float(e["price"]),
                            "conf": int(e.get("conf", 50))} for e in recent_e],
                "vsa": [{"label": s["label"], "date": str(s["date"].date()),
                         "desc": s["desc"]} for s in recent_v]}}
            from ui.threads.status_ticker import build_ticker_msgs
            msgs = build_ticker_msgs(rich)
            self.result.emit(msgs)
        except Exception:
            self.result.emit([])
