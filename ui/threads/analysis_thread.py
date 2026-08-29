"""单股票完整分析后台线程。

在 worker 线程中运行 run_analysis(), 收集 K线/P&F/指标/资金/结论等数据,
完成后通过 done 信号将结果字典发回主线程渲染。
"""
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from wyckoff._log import log_exc
from wyckoff.analysis import _ANALYSIS_CACHE, _ANALYSIS_LOCK, run_analysis
from wyckoff.utils import normalize_symbol


class AnalysisThread(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str, str)

    def __init__(self, code, datalen, scale, settings, force_refresh, parent=None):
        super().__init__(parent)
        self._code = code
        self._datalen = datalen
        self._scale = scale
        self._settings = settings
        self._force_refresh = force_refresh
        self._kline_data = {}
        self._pnf_data = {}
        self._ind_data = {}
        self._mkt_data = {}

    @staticmethod
    def _snapshot_cb(df, symbol, code, scale, datalen, name, phase_label, conf_q, precomputed):
        from wyckoff.accuracy import record_analysis
        try:
            record_analysis(df, symbol, code, scale, datalen, name,
                            phase_label=phase_label or None, conf_q=conf_q or None,
                            precomputed=precomputed)
        except Exception as e:
            log_exc("_snapshot_cb 记录准确度快照失败", e)

    def run(self):
        try:
            resolved = self._resolve(self._code)
            if not resolved:
                self.failed.emit("无法识别的股票代码", "")
                return
            text, fig, pnf_fig, ind_fig, summary, sections, market, segs = run_analysis(
                resolved, datalen=self._datalen, scale=self._scale,
                draw_waves=bool(self._settings.get("draw_waves", True)),
                draw_locks=bool(self._settings.get("draw_locks", True)),
                force_refresh=self._force_refresh,
                confirm_enabled=bool(self._settings.get("confirm_enabled", True)),
                settings=self._settings,
                precomputed_cb=self._snapshot_cb,
                kline_engine="pyqtgraph", kline_data=self._kline_data,
                pnf_engine="pyqtgraph", pnf_data=self._pnf_data,
                ind_engine="pyqtgraph", ind_data=self._ind_data,
                mkt_engine="pyqtgraph", mkt_data=self._mkt_data)
            with _ANALYSIS_LOCK:
                cached = _ANALYSIS_CACHE.get((normalize_symbol(resolved), self._datalen, self._scale))
            df = cached[0] if cached else None
            name = cached[1] if cached else ""
            vsa = cached[2] if cached and len(cached) > 2 else None
            self.done.emit({
                "code": resolved, "text": text, "fig": fig, "pnf_fig": pnf_fig,
                "ind_fig": ind_fig, "summary": summary, "sections": sections,
                "market": market, "segs": segs, "df": df, "name": name,
                "vsa_signals": vsa, "kline_data": self._kline_data,
                "pnf_data": self._pnf_data, "ind_data": self._ind_data,
                "mkt_data": self._mkt_data,
            })
        except Exception as e:
            self.failed.emit(str(e), traceback.format_exc())

    def _resolve(self, input_text):
        if " " in input_text:
            input_text = input_text.split()[0]
        from wyckoff.indices import find_index, search_index
        from wyckoff.pinyin import search_stock
        idx = find_index(input_text)
        if idx:
            return idx["symbol"]
        idx_hits = search_index(input_text, limit=1)
        if idx_hits:
            return idx_hits[0]["symbol"]
        try:
            return normalize_symbol(input_text)
        except ValueError:
            pass
        results = search_stock(input_text, limit=1)
        if results:
            try:
                return normalize_symbol(results[0]["code"])
            except ValueError:
                return results[0]["code"]
        return None
