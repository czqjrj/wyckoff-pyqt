"""图表管理器: 统一管理 K线/点数图/技术指标/资金透视 四大图表控件。
从 MainWindow 提取图表渲染、视图记忆、占位状态等逻辑。"""
from __future__ import annotations

from typing import Any
from datetime import datetime as _dt

from wyckoff._log import log_exc
from wyckoff.config import SCALE_OPTIONS, PERIOD_OPTIONS
from wyckoff.settings_keys import S
from wyckoff.storage import save_settings

from . import theme
from .base_plot import BasePlotWidget


class ChartManager:
    """图表管理器: 统一管理四大图表控件的渲染/视图记忆/占位状态。"""

    def __init__(self, main_window) -> None:
        self._mw = main_window
        self._view_mem: dict[tuple[str, int], tuple[float, float]] = {}
        self._last_kline: dict = {}
        self._last_pnf: dict = {}
        self._last_ind: dict = {}
        self._last_mkt: dict = {}
        self._pnf_box_mult = 1.0

    # ── 公开属性 (保持与 MainWindow 兼容) ──
    @property
    def view_mem(self) -> dict:
        return self._view_mem

    @property
    def last_kline(self) -> dict:
        return self._last_kline

    @last_kline.setter
    def last_kline(self, v: dict):
        self._last_kline = v

    @property
    def last_pnf(self) -> dict:
        return self._last_pnf

    @last_pnf.setter
    def last_pnf(self, v: dict):
        self._last_pnf = v

    @property
    def last_ind(self) -> dict:
        return self._last_ind

    @last_ind.setter
    def last_ind(self, v: dict):
        self._last_ind = v

    @property
    def last_mkt(self) -> dict:
        return self._last_mkt

    @last_mkt.setter
    def last_mkt(self, v: dict):
        self._last_mkt = v

    @property
    def pnf_box_mult(self) -> float:
        return self._pnf_box_mult

    @pnf_box_mult.setter
    def pnf_box_mult(self, v: float):
        self._pnf_box_mult = v

    # ── 视图记忆 ──
    def remember_kline_view(self):
        """记忆当前 K线图缩放/平移位置 (供刷新前调用)。"""
        if not self._mw._current_code:
            return
        try:
            sk = SCALE_OPTIONS.get(self._mw.cb_scale.currentText(), 240)
            vr = self._mw.kline_widget.get_view_range()
            if vr:
                self._view_mem[(str(self._mw._current_code), int(sk))] = vr
        except Exception:
            pass

    def restore_kline_view(self, code: str, scale: int):
        """恢复 K线图视图 (分析完成后调用)。"""
        vr = self._view_mem.get((str(code), int(scale)))
        if vr:
            self._mw.kline_widget.apply_view(*vr, push=False)

    # ── 占位/加载状态 ──
    def set_placeholder(self, msg: str):
        """为技术指标/资金透视设置占位文案 (分析开始时)。"""
        for w in (self._mw.ind_widget, self._mw.mkt_widget):
            try:
                w.set_placeholder(msg)
            except Exception:
                pass

    def set_error_placeholder(self, msg: str = "分析失败, 请重试"):
        """设置错误占位 (分析失败时)。"""
        for w in (self._mw.ind_widget, self._mw.mkt_widget):
            try:
                w.set_placeholder(msg)
            except Exception:
                pass

    # ── 渲染入口 ──
    def render_all(self, r: dict) -> dict[str, Any]:
        """批量渲染四大图表, 返回渲染所需数据供后续逻辑使用。"""
        # 禁用重绘, 避免逐个 set_data 触发多次 layout
        self._mw.setUpdatesEnabled(False)
        try:
            # 从分析结果获取新数据
            new_kline = r.get("kline_data") or {}
            new_pnf = r.get("pnf_data") or {}
            new_ind = r.get("ind_data") or {}
            new_mkt = r.get("mkt_data") or {}

            # 恢复 K线视图记忆
            self.restore_kline_view(r["code"], self._mw._last_scale)

            # 渲染四大图表 (使用新数据)
            self._mw.kline_widget.set_data(**new_kline)
            self._mw.pnf_widget.set_data(**new_pnf, code=r["code"])
            self._mw.ind_widget.set_data(**new_ind)
            self._mw.mkt_widget.set_data(**new_mkt)

            # 更新内部缓存
            self._last_kline = new_kline
            self._last_pnf = new_pnf
            self._last_ind = new_ind
            self._last_mkt = new_mkt

            # 产业链地图
            self._render_chain(r)

            return {
                "kline_data": new_kline,
                "pnf_data": new_pnf,
                "ind_data": new_ind,
                "mkt_data": new_mkt,
            }
        finally:
            self._mw.setUpdatesEnabled(True)

    def _render_chain(self, r: dict):
        """渲染产业链地图 (fail-soft)。"""
        try:
            market = r.get("market") or {}
            sec_info = market.get("sector") or {}
            self._mw.chain_widget.set_symbol(
                r["code"], r.get("name"), sec_info.get("name"))
        except Exception:
            pass

    # ── 缓存更新 ──
    def update_caches(self, r: dict, scale: int, datalen: int):
        """更新所有内部缓存 (分析完成后调用)。"""
        code = r.get("code", "")
        self._last_kline = r.get("kline_data") or {}
        self._last_pnf = r.get("pnf_data") or {}
        self._last_ind = r.get("ind_data") or {}
        self._last_mkt = r.get("mkt_data") or {}
        self._pnf_box_mult = 1.0

    # ── 导出/截图 ──
    def grab_all_pixmaps(self) -> dict[str, Any]:
        """获取所有图表快照 (供导出报告)。"""
        return {
            "kline": self._mw.kline_widget.grab_pixmap(),
            "pnf": self._mw.pnf_widget.grab_pixmap(),
            "ind": self._mw.ind_widget.grab_pixmap(),
            "mkt": self._mw.mkt_widget.grab_pixmap(),
        }

    def grab_pixmap(self, chart_type: str):
        """获取指定图表快照。"""
        mapping = {
            "kline": self._mw.kline_widget,
            "pnf": self._mw.pnf_widget,
            "ind": self._mw.ind_widget,
            "mkt": self._mw.mkt_widget,
        }
        w = mapping.get(chart_type)
        return w.grab_pixmap() if w else None

    # ── 视图重置/复位 ──
    def reset_all_views(self):
        """复位所有图表视图到全幅。"""
        for w in (self._mw.kline_widget, self._mw.pnf_widget,
                  self._mw.ind_widget, self._mw.mkt_widget):
            try:
                w.reset_view()
            except Exception:
                pass

    def apply_default_views(self):
        """应用默认视野 (聚焦最近 N 根)。"""
        self._mw.ind_widget.apply_default_view()
        self._mw.mkt_widget.apply_default_view()

    # ── P&F 格值同步 ──
    def sync_pnf_box_scale(self, box_mode: str, atr_factor: float):
        """同步 P&F 格值设置到图表与存储。"""
        self._pnf_box_mult = atr_factor if box_mode == "atr" else 1.0
        self._mw.pnf_widget.set_box_mode(box_mode, atr_factor)
        self._mw.settings[S.Chart.PNF_BOX_MODE] = box_mode
        self._mw.settings[S.Chart.PNF_ATR_FACTOR] = atr_factor
        try:
            from wyckoff.storage import save_settings
            save_settings(self._mw.settings)
        except Exception:
            pass

    def reset_pnf_box(self):
        """重置 P&F 格值到默认 (百分比模式)。"""
        self.sync_pnf_box_scale("pct", 0.5)

    # ── 默认视野同步 ──
    def sync_chart_defaults(self):
        """把设置里的默认柱数同步到图表控件。"""
        self._mw.ind_widget.default_bars = int(
            self._mw.settings.get(S.Chart.IND_DEFAULT_BARS, 250) or 0)
        self._mw.mkt_widget.default_bars = int(
            self._mw.settings.get(S.Chart.MKT_DEFAULT_BARS, 120) or 0)

    # ── 导出图片 ──
    def save_chart_png(self, chart_type: str, filename: str) -> bool:
        """保存指定图表为 PNG。"""
        pm = self.grab_pixmap(chart_type)
        if pm is None:
            return False
        try:
            pm.save(filename)
            return True
        except Exception:
            return False

    # ── 状态栏 tick ──
    def push_analysis_ticker(self, r: dict):
        """推送分析 tick 到状态栏 (后台线程)。"""
        try:
            df = r.get("df")
            code = r.get("code") or ""
            if df is None or not code:
                return
            name = r.get("name") or ""
            prev = getattr(self._mw, "_analysis_ticker_th", None)
            if prev is not None and prev.isRunning():
                try:
                    prev.stop()
                except Exception:
                    pass
            from .threads import AnalysisTickerThread
            th = AnalysisTickerThread(df, code, name, self._mw)
            th.result.connect(self._mw._on_analysis_ticker_msgs)
            self._mw._analysis_ticker_th = th
            th.start()
        except Exception as e:
            from wyckoff._log import log_exc
            log_exc("_push_analysis_ticker 启动线程失败", e)