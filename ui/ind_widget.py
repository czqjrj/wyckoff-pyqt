"""pyqtgraph 技术指标控件 (替换原 matplotlib 技术指标图, 排版/配色与原版一致)。

数据由 AnalysisThread 在 worker 线程通过 chart.build_ind_data() 收集 (指标计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

排版 (4×2 网格, 高度比 1.3:1.3:1.3:1.5):
  第一行: MACD (12,26,9) (跨两列, 最宽)
  第二行: 量能 (万手) | 价格 · 布林带 (20,2) · 大盘对比
  第三行: KDJ (9,3,3)  | RSI (6,12,24)
  第四行: OBV 能量潮   | 量价分布 (Volume Profile)
每个面板下方一行 "当前信号 → 预示" 解读 (颜色随信号红/绿/橙/灰, 与原版 set_xlabel 一致)。
默认显示全幅 (与原版一致, 无初始缩放)。

交互 (由 ui.base_plot.BasePlotWidget 基类统一提供):
  - 滚轮    以光标为锚点缩放 X 轴 (各面板 Y 为全量数据固定范围)
  - 左键拖拽 平移 (范围限制在数据全幅内)
  - 双击    复位到全幅
  - 键盘    上箭头 / + 放大, 下箭头 / - 缩小 (以视图中心为锚点),
            左/右箭头平移, Home/r 复位, Backspace/f 视图历史
"""
import datetime as _dt

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QScrollArea
from pyqtgraph.Qt import QtGui
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import FONT_CANDIDATES

from . import theme
from .base_plot import BasePlotWidget, HoverHighlightMixin
from .constants import IND_ASPECT, IND_DEFAULT_BARS
from pyqtgraph.Qt.QtCore import pyqtSignal

# 面板注册表 (声明式, 支持扩展) — 见 ui.ind_panels
from .ind_panels import get_panels  # noqa: E402


def _pen(color, width=1.0, style=None, alpha=1.0):
    pen = pg.mkPen(color, width=width)
    if style is not None:
        pen.setStyle(style)
    if alpha < 1.0:
        c = pen.color()
        c.setAlphaF(float(alpha))
        pen.setColor(c)
    return pen


def _brush_alpha(color, alpha):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    return pg.mkBrush(c)


def _yminmax(*series):
    """多个等长/不等长序列的整体 min/max (NaN 忽略)。"""
    parts = []
    for s in series:
        if s is None:
            continue
        a = np.asarray(s, dtype=float).ravel()
        if a.size:
            parts.append(a)
    if not parts:
        return 0.0, 1.0
    allv = np.concatenate(parts)
    lo = float(np.nanmin(allv))
    hi = float(np.nanmax(allv))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi - lo < 1e-12:
        return 0.0, 1.0
    return lo, hi


def _padded(lo, hi, frac):
    pad = (hi - lo) * frac
    return lo - pad, hi + pad


def _parse_day(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    return None


class _DateAxis(pg.AxisItem):
    """X 轴日期刻度: 按整数柱索引回查日期渲染标签 (与原版 _ticks 一致)。"""

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._days = []
        self._minute = False

    def set_days(self, days, minute):
        self._days = list(days)
        self._minute = bool(minute)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._days):
                d = self._days[i]
                if d is None:
                    out.append("")
                elif self._minute:
                    out.append(d.strftime("%m-%d %H:%M"))
                else:
                    out.append(d.strftime("%y-%m-%d"))
            else:
                out.append("")
        return out


class IndScroll(QScrollArea):
    """技术指标滚动容器: 宽度铺满可视区, 高度按 13.5:8.5 比例放大 (与原版一致)。

    与原版 _sync_ind_fig 相同的"宽度铺满、高度按比例、竖向滚动"行为, 保证
    面板长宽比例不随窗口形状被压扁。
    """

    def __init__(self, widget, aspect=IND_ASPECT, parent=None):
        super().__init__(parent)
        self._aspect = float(aspect)
        self.setWidgetResizable(True)
        self.setFrameShape(self.Shape.NoFrame)
        self.setWidget(widget)
        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(
            f"QScrollArea {{ background: {theme.C_PANEL}; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: {theme.C_PANEL}; }}")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        w = self.viewport().width()
        if w > 50:
            self.widget().setMinimumHeight(int(w * self._aspect))
        self.widget().setMinimumWidth(w)


class IndWidget(HoverHighlightMixin, BasePlotWidget):
    """pyqtgraph 技术指标: 4×2 网格 (与原版 matplotlib 排版一致), X 联动。

    继承 BasePlotWidget 获得统一交互 (滚轮/键盘/拖拽边界/双击复位/视图历史),
    HoverHighlightMixin 提供面板悬停重点描边。默认视野聚焦最近 IND_DEFAULT_BARS
    根 (由设置 Chart.IND_DEFAULT_BARS 覆盖, 0=全幅)。
    """

    # 技术指标原版无缩放动画, 关闭以保证即时响应 (与测试/原版一致)
    ANIM_MS = 0

    # 默认视野柱数 (0=全幅); 外部按设置覆盖
    default_bars = IND_DEFAULT_BARS

    crosshair_moved = pyqtSignal(str, float, float)
    status_message = pyqtSignal(str)

    def __init__(self, parent=None, font_size=11):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._n = 0
        self._days = []
        self._is_minute = False
        self._full_x = (0.0, 1.0)
        self._date_axes = {}
        self.plots = {}
        self.cap_labels = {}
        self._yranges = {}
        self._focused = None
        self._crosshair_values = {}
        self._sync_x = True
        self._build_plots()
        self._hh_start()
        self.plots["price"].setTitle(
            "输入 A 股代码 (如 600104 / sh600104 / 000001), 点击\"开始分析\""
            "加载技术指标。")

    # ── 布局 ──
    def _build_plots(self):
        self.ci.clear()
        self.ci.setContentsMargins(8, 8, 8, 8)
        self.ci.setSpacing(4)
        self.plots = {}
        self.cap_labels = {}
        self._date_axes = {}
        for panel in get_panels():
            key, title, row, col, colspan, stretch = (
                panel.key, panel.title, panel.row, panel.col,
                panel.colspan, panel.stretch)
            self._date_axes[key] = _DateAxis("bottom")
            plot = pg.PlotItem(axisItems={"bottom": self._date_axes[key]})
            plot.setTitle(title, color=theme.C_TEXT, size=f"{self._fs(2)}pt")
            plot.hideButtons()
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            plot.showGrid(x=True, y=True, alpha=0.35)
            plot.getViewBox().setMouseEnabled(x=True, y=False)
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                axis.setPen(_pen(theme.C["axis"], 1))
                axis.setTextPen(_pen(theme.C_TEXT, 1))
            self.ci.addItem(plot, row, col, colspan=colspan)
            self.ci.layout.setRowStretchFactor(row, stretch)
            cap = self.ci.addLabel("", row=row + 1, col=col, colspan=colspan,
                                   justify="center", color=theme.C_MUTED,
                                   size=f"{self._fs(1)}pt")
            cap.setFont(self._font(1))
            self.cap_labels[key] = cap
            # vp (量价分布) X 轴是成交量 (万手), 不随日期联动
            self.register_plot(plot, sync=(key != "vp"),
                               primary=(key == "price"))
            self.plots[key] = plot

    # ── 主题 ──
    def apply_theme(self):
        """主题切换时刷新控件/视口/坐标轴配色。"""
        self.setBackground(pg.mkColor(theme.C_PANEL))
        for plot in self.plots.values():
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                if axis is not None:
                    axis.setPen(_pen(theme.C["axis"], 1))
                    axis.setTextPen(_pen(theme.C_TEXT, 1))

    # ── 数据入口 ──
    def set_data(self, n=0, day=None, is_minute=False, x=None, close=None,
                 open=None, volume=None, index_ov=None, caps=None,
                 boll_up=None, boll_mid=None, boll_dn=None, vol_ma5=None,
                 vol_ma10=None, vol_ratio=None, macd_hist=None, macd_dif=None,
                 macd_dea=None, kdj_k=None, kdj_d=None, kdj_j=None,
                 rsi_6=None, rsi_12=None, rsi_24=None, obv=None, obv_ma=None,
                 vp=None, rs_series=None, **extra):
        self.clear_plots()
        self._hh_hide()
        self._focused = None
        self._n = 0
        self._days = []
        if not n or not close:
            self._build_plots()
            self.plots["price"].setTitle("暂无技术指标数据")
            return
        self._n = int(n)
        self._has_data = True
        self._days = [_parse_day(d) for d in (day or [])]
        self._is_minute = bool(is_minute)
        x = np.arange(self._n) if x is None else np.asarray(x, dtype=float)
        self._full_x = (float(x[0]), float(x[-1]))

        self._build_plots()
        for key, plot in self.plots.items():
            if key != "vp":
                self.set_full_x(plot, self._full_x)
        for axis in self._date_axes.values():
            axis.set_days(self._days, self._is_minute)
        self._yranges = self._compute_yranges(
            close, boll_up, boll_dn, index_ov, volume, vol_ma5, vol_ma10,
            macd_hist, macd_dif, macd_dea, kdj_k, kdj_d, kdj_j,
            rsi_6, rsi_12, rsi_24, obv, obv_ma, vp, rs_series)
        self._draw_price(x, close, boll_up, boll_dn, index_ov)
        self._draw_volume(x, volume, open, close, vol_ma5, vol_ma10, vol_ratio)
        self._draw_macd(x, macd_hist, macd_dif, macd_dea)
        self._draw_kdj(x, kdj_k, kdj_d, kdj_j)
        self._draw_rsi(x, rsi_6, rsi_12, rsi_24)
        self._draw_obv(x, obv, obv_ma)
        self._draw_vp(vp)
        self._draw_rs(x, rs_series)
        self._set_caps(caps or {})
        # 与原版一致: 初始显示全幅 (不做默认缩放)
        self.apply_default_view()
        self._attach_crosshairs()

    # ── 十字光标 ──
    def _fmt_x(self):
        days = self._days
        minute = self._is_minute

        def fmt(i):
            idx = int(round(i))
            if 0 <= idx < len(days):
                d = days[idx]
                if d is None:
                    return ""
                try:
                    return (d.strftime("%m-%d %H:%M") if minute
                            else d.strftime("%Y-%m-%d"))
                except Exception:
                    return str(d)
            return ""

        return fmt

    def _attach_crosshairs(self):
        self.detach_crosshairs()
        if self._n <= 0:
            return
        fmt_x = self._fmt_x()
        def fmt_vol_x(v):
            return f"{v:.1f}万手"
        for panel in get_panels():
            key = panel.key
            if key not in self.plots:
                continue
            fx = fmt_vol_x if panel.is_volume_x else fmt_x
            # 日期面板吸附整数柱位 (读数对齐柱心), 量价分布 X 是成交量不吸附
            snap = not panel.is_volume_x
            ch = self.attach_crosshair(self.plots[key], fx, panel.fmt_y, snap=snap)
            ch._panel_key = key
            ch.sigPositionChanged.connect(
                lambda ch=ch: self._on_crosshair_moved(ch))

    def _compute_yranges(self, close, boll_up, boll_dn, index_ov, volume,
                         vol_ma5, vol_ma10, macd_hist, macd_dif, macd_dea,
                         kdj_k, kdj_d, kdj_j, rsi_6, rsi_12, rsi_24,
                         obv, obv_ma, vp, rs_series=None):
        yr = {}
        c = np.asarray(close, dtype=float)
        idx_c = None
        if index_ov:
            idx_c = np.asarray(index_ov["close"], dtype=float)
        # 价格: 布林带 + 收盘 + 大盘归一 (与原版 autoscale 一致)
        lo, hi = _yminmax(boll_dn, c, idx_c)
        if boll_up is not None:
            hi = max(hi, float(np.nanmax(np.asarray(boll_up, dtype=float))))
        yr["price"] = _padded(lo, hi, 0.05)
        # 量能: 0 ~ 量/均量 * 1.15
        vmax = 0.0
        for s in (volume, vol_ma5, vol_ma10):
            if s is not None and len(s):
                vmax = max(vmax, float(np.nanmax(np.asarray(s, dtype=float))))
        yr["volume"] = (0.0, max(vmax * 1.15, 1.0))
        # MACD / KDJ / OBV: 全数据 min/max 垫边 (与原版 autoscale 一致)
        lo, hi = _yminmax(macd_hist, macd_dif, macd_dea)
        yr["macd"] = _padded(lo, hi, 0.08)
        lo, hi = _yminmax(kdj_k, kdj_d, kdj_j)
        yr["kdj"] = _padded(lo, hi, 0.08)
        # RSI: 固定 [-5, 105] (与原版 set_ylim 一致)
        yr["rsi"] = (-5.0, 105.0)
        lo, hi = _yminmax(obv, obv_ma)
        yr["obv"] = _padded(lo, hi, 0.08)
        # 量价分布: 价格桶范围 (全幅)
        if vp:
            yr["vp"] = (float(vp["edges"][0]), float(vp["edges"][-1]))
        else:
            yr["vp"] = (0.0, 1.0)
        # 相对强度: 全数据 min/max 垫边, 保证 0 轴线可见
        lo, hi = _yminmax(rs_series)
        if rs_series is not None:
            lo = min(lo, 0.0)
            hi = max(hi, 0.0)
        yr["rs"] = _padded(lo, hi, 0.08)
        return yr

    # ── 各面板绘制 (配色/线型/标题与原版 matplotlib 逐一对应) ──
    def _draw_price(self, x, close, boll_up, boll_dn, index_ov):
        plot = self.plots["price"]
        c = np.asarray(close, dtype=float)
        legend = plot.addLegend(offset=(12, 4))
        close_line = plot.plot(x, c, pen=_pen(theme.C_TEXT, 1.1),
                               connect="finite")
        legend.addItem(close_line, "收盘")
        if boll_up is not None and boll_dn is not None:
            bu = np.asarray(boll_up, dtype=float)
            bd = np.asarray(boll_dn, dtype=float)
            plot.addItem(pg.FillBetweenItem(
                pg.PlotCurveItem(x, bu, connect="finite"),
                pg.PlotCurveItem(x, bd, connect="finite"),
                brush=_brush_alpha(theme.C_ACCENT, 0.06)))
            up_line = plot.plot(x, bu, pen=_pen(theme.C_UP, 0.9,
                                                Qt.PenStyle.DashLine,
                                                alpha=0.85),
                                connect="finite")
            dn_line = plot.plot(x, bd, pen=_pen(theme.C_DOWN, 0.9,
                                                Qt.PenStyle.DashLine,
                                                alpha=0.85),
                                connect="finite")
            legend.addItem(up_line, "BOLL上轨")
            legend.addItem(dn_line, "BOLL下轨")
        if index_ov:
            ix = np.asarray(index_ov["x"], dtype=float)
            ic = np.asarray(index_ov["close"], dtype=float)
            idx_line = plot.plot(ix, ic, pen=_pen(theme.C_ACCENT, 1.1),
                                 connect="finite")
            legend.addItem(idx_line, "上证(归一)")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["price"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_volume(self, x, volume, open_, close, vol_ma5, vol_ma10,
                     vol_ratio):
        plot = self.plots["volume"]
        if volume is not None and len(volume):
            vol = np.asarray(volume, dtype=float)
            finite = np.isfinite(vol)
            xs = x[finite]
            heights = vol[finite]
            if close is not None and open_ is not None and len(close) \
                    and len(open_):
                o = np.asarray(open_, dtype=float)[finite]
                c = np.asarray(close, dtype=float)[finite]
                up_mask = c >= o
            else:
                up_mask = np.ones(len(xs), dtype=bool)
            brushes = [pg.mkBrush(theme.C_UP if u else theme.C_DOWN) for u in up_mask]
            plot.addItem(pg.BarGraphItem(x=xs, height=heights, width=0.6,
                                         brushes=brushes, pen=None))
        legend = plot.addLegend(offset=(12, 4))
        if vol_ma5 is not None and len(vol_ma5):
            m5 = plot.plot(x, np.asarray(vol_ma5, dtype=float),
                           pen=_pen(theme.C_UP, 0.9), connect="finite")
            legend.addItem(m5, "量MA5")
        if vol_ma10 is not None and len(vol_ma10):
            m10 = plot.plot(x, np.asarray(vol_ma10, dtype=float),
                            pen=_pen(theme.C_DOWN, 0.9), connect="finite")
            legend.addItem(m10, "量MA10")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        title = "量能 (万手)"
        if vol_ratio is not None:
            title += f" · 量比 {float(vol_ratio):.2f}"
        plot.setTitle(title, color=theme.C_TEXT, size=f"{self._fs(2)}pt")
        plot.setYRange(*self._yranges["volume"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_macd(self, x, hist, dif, dea):
        plot = self.plots["macd"]
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen(theme.C_MUTED, 0.7)))
        if hist is not None and len(hist):
            h = np.asarray(hist, dtype=float)
            pos = h >= 0
            if pos.any():
                xs = x[pos]
                plot.addItem(pg.BarGraphItem(
                    x=xs, height=h[pos], width=0.6, pen=None,
                    brushes=[pg.mkBrush(theme.C_UP)] * len(xs)))
            if (~pos).any():
                xs = x[~pos]
                plot.addItem(pg.BarGraphItem(
                    x=xs, height=h[~pos], width=0.6, pen=None,
                    brushes=[pg.mkBrush(theme.C_DOWN)] * len(xs)))
        legend = plot.addLegend(offset=(12, 4))
        if dif is not None and len(dif):
            dl = plot.plot(x, np.asarray(dif, dtype=float),
                           pen=_pen(theme.C_DOWN, 0.9), connect="finite")
            legend.addItem(dl, "DIF")
        if dea is not None and len(dea):
            ea = plot.plot(x, np.asarray(dea, dtype=float),
                           pen=_pen(theme.C_ACCENT, 0.9), connect="finite")
            legend.addItem(ea, "DEA")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["macd"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_kdj(self, x, k, d, j):
        plot = self.plots["kdj"]
        for yv in (20, 80):
            plot.addItem(pg.InfiniteLine(pos=yv, angle=0,
                                         pen=_pen(theme.C_MUTED, 0.7,
                                                  Qt.PenStyle.DashLine,
                                                  alpha=0.6)))
        legend = plot.addLegend(offset=(12, 4))
        if k is not None and len(k):
            kl = plot.plot(x, np.asarray(k, dtype=float),
                           pen=_pen(theme.C_DOWN, 0.9), connect="finite")
            legend.addItem(kl, "K")
        if d is not None and len(d):
            dl = plot.plot(x, np.asarray(d, dtype=float),
                           pen=_pen(theme.C_ACCENT, 0.9), connect="finite")
            legend.addItem(dl, "D")
        if j is not None and len(j):
            jl = plot.plot(x, np.asarray(j, dtype=float),
                           pen=_pen(theme.C_ACCENT, 0.7), connect="finite")
            legend.addItem(jl, "J")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["kdj"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_rsi(self, x, r6, r12, r24):
        plot = self.plots["rsi"]
        for yv, col in ((70, theme.C_UP), (30, theme.C_DOWN)):
            plot.addItem(pg.InfiniteLine(pos=yv, angle=0,
                                         pen=_pen(col, 0.8,
                                                  Qt.PenStyle.DashLine,
                                                  alpha=0.5)))
        legend = plot.addLegend(offset=(12, 4))
        for col, lab, r in ((theme.C_UP, "RSI6", r6),
                            (theme.C_DOWN, "RSI12", r12),
                            (theme.C_ACCENT, "RSI24", r24)):
            if r is not None and len(r):
                rl = plot.plot(x, np.asarray(r, dtype=float),
                               pen=_pen(col, 0.9), connect="finite")
                legend.addItem(rl, lab)
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["rsi"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_obv(self, x, obv, obv_ma):
        plot = self.plots["obv"]
        legend = plot.addLegend(offset=(12, 4))
        if obv is not None and len(obv):
            ol = plot.plot(x, np.asarray(obv, dtype=float),
                           pen=_pen(theme.C_MUTED, 1.0), connect="finite")
            legend.addItem(ol, "OBV")
        if obv_ma is not None and len(obv_ma):
            ml = plot.plot(x, np.asarray(obv_ma, dtype=float),
                           pen=_pen(theme.C_DOWN, 0.9,
                                    Qt.PenStyle.DashLine),
                           connect="finite")
            legend.addItem(ml, "OBV均线")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["obv"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_vp(self, vp):
        plot = self.plots["vp"]
        y0, y1 = self._yranges["vp"]
        xmax = 1.0
        if vp and vp.get("vols") and len(vp["vols"]):
            vols = np.asarray(vp["vols"], dtype=float)
            mid = np.asarray(vp["mid"], dtype=float)
            edges = np.asarray(vp["edges"], dtype=float)
            h = (edges[1] - edges[0]) * 0.9
            last = float(vp.get("last", mid[np.argmax(mid)]))
            brushes = [pg.mkBrush(theme.C_UP if m >= last else theme.C_DOWN) for m in mid]
            plot.addItem(pg.BarGraphItem(x0=np.zeros(len(mid)), x1=vols,
                                         y=mid - h / 2, height=h,
                                         brushes=brushes, pen=None))
            poc = float(vp["poc"])
            plot.addItem(pg.InfiniteLine(pos=poc, angle=0,
                                         pen=_pen(theme.C.get("poc", theme.C_AMBER), 1.0,
                                                  Qt.PenStyle.DotLine)))
            plot.addItem(pg.InfiniteLine(pos=last, angle=0,
                                         pen=_pen(theme.C_TEXT, 1.0,
                                                  Qt.PenStyle.DotLine)))
            xmax = float(np.nanmax(vols))
            if not np.isfinite(xmax) or xmax <= 0:
                xmax = 1.0
            plot.setXRange(0.0, xmax * 1.15, padding=0)
            plot.setYRange(y0, y1, padding=0)
            plot.enableAutoRange(x=False, y=False)
            pad_y = (y1 - y0) * 0.03
            self._text(plot, xmax * 0.02, y1 - pad_y, f"POC {poc:.2f}",
                       theme.C.get("poc", theme.C_AMBER), anchor=(0, 1), delta=-2)
            self._text(plot, xmax * 1.13, y0 + pad_y, f"现价 {last:.2f}",
                       theme.C_TEXT, anchor=(1, 0), delta=-2)
        else:
            plot.setXRange(0.0, 1.0, padding=0)
            plot.setYRange(y0, y1, padding=0)
            plot.enableAutoRange(x=False, y=False)

    def _draw_rs(self, x, rs_series):
        plot = self.plots["rs"]
        if rs_series is not None and len(rs_series):
            r = np.asarray(rs_series, dtype=float)
            plot.addItem(pg.InfiniteLine(pos=0.0, angle=0,
                                         pen=_pen(theme.C_MUTED, 0.9,
                                                  Qt.PenStyle.DotLine)))
            valid = np.isfinite(r)
            color = theme.C_UP if np.nansum(r[valid]) >= 0 else theme.C_DOWN
            line = plot.plot(x, r, pen=_pen(color, 1.1), connect="finite")
            legend = plot.addLegend(offset=(12, 4))
            legend.addItem(line, "RS 20日 (%)")
            legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
            cur = r[valid][-1] if valid.any() else 0.0
            self._text(plot, x[-1], cur, f"{cur:+.1f}%", color, anchor=(1, 0),
                       delta=-2)
        else:
            plot.setTitle("相对强度 RS (20日) vs 上证指数 (需日线+大盘数据)",
                          color=theme.C_MUTED, size=f"{self._fs(2)}pt")
        plot.setYRange(*self._yranges["rs"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _set_caps(self, caps):
        for key, label in self.cap_labels.items():
            entry = caps.get(key)
            if entry:
                msg, color = entry[0], entry[1]
                label.setText(msg, color=color)
                label.setFont(self._font(1, bold=True))
            else:
                label.setText("")

    # ── 占位 / 加载反馈 ──
    def set_placeholder(self, msg):
        """显示占位文案 (分析中/失败), 清空面板数据并回到完整网格。"""
        self.clear_plots()
        self._hh_hide()
        self._focused = None
        self._n = 0
        self._days = []
        self._build_plots()
        self._has_data = False
        for lab in self.cap_labels.values():
            lab.setText("")
        if msg:
            self.plots["price"].setTitle(msg, color=theme.C_MUTED,
                                         size=f"{self._fs(2)}pt")

    # ── 默认视野 (聚焦最近 N 根, 由设置覆盖) ──
    def apply_default_view(self):
        """重新应用默认视野: default_bars>0 时聚焦最近 N 根, 否则全幅。"""
        if self._n <= 0 or len(self.plots) == 0:
            return
        n_bars = int(getattr(self, "default_bars", IND_DEFAULT_BARS) or 0)
        lo, hi = self._full_x
        if n_bars > 0 and (hi - lo) + 1 > n_bars:
            start = max(lo, hi - (n_bars - 1))
            self.apply_view(start, hi, push=False)
        else:
            self.apply_view(lo, hi, push=False)

    # ── 面板化独立控制: 单个面板放大 / 恢复网格 ──
    def focus_panel(self, key):
        """把指定面板放大为单面板视图 (其余面板隐藏, 交互仍完整)。"""
        if key not in self.plots:
            return
        if self._focused == key:
            return
        self._focused = key
        self._apply_panel_layout()
        self.reset_view()
        # 添加聚焦高亮
        self._add_focus_highlight(self.plots[key])

    def show_grid(self):
        """从单面板放大态恢复 4×2 完整网格。"""
        if self._focused is None:
            return
        self._focused = None
        self._apply_panel_layout()
        self.apply_default_view()
        self._attach_crosshairs()
        # 移除聚焦高亮
        self._remove_focus_highlight()

    def _remove_focus_highlight(self):
        """移除聚焦面板的高亮边框。"""
        if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
            try:
                self._focus_highlight.scene().removeItem(self._focus_highlight)
            except Exception:
                pass
            self._focus_highlight = None

    def is_focused(self):
        """是否处于单面板放大态; 返回当前面板 key 或 None。"""
        return self._focused

    @property
    def focused_panel(self):
        return self._focused

    def _add_focus_highlight(self, plot):
        """为聚焦面板添加高亮边框。"""
        from PyQt6.QtWidgets import QGraphicsRectItem
        if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
            try:
                self._focus_highlight.scene().removeItem(self._focus_highlight)
            except Exception:
                pass
        pen = pg.mkPen(theme.C_ACCENT, width=2.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._focus_highlight = QGraphicsRectItem()
        self._focus_highlight.setPen(pen)
        self._focus_highlight.setZValue(30)
        vb = plot.getViewBox()
        self._focus_highlight.setRect(vb.sceneBoundingRect())
        self.scene().addItem(self._focus_highlight)
        # 监听视图变化更新高亮框
        def _update_highlight():
            if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
                self._focus_highlight.setRect(vb.sceneBoundingRect())
        vb.sigXRangeChanged.connect(_update_highlight)
        vb.sigYRangeChanged.connect(_update_highlight)

    def _apply_panel_layout(self):
        """按 _focused 重置 ci 布局: None=完整网格, key=单面板。
        pyqtgraph GraphicsLayout.removeItem/addItem 支持摘挂 PlotItem,
        数据/坐标轴/十字光标均在 PlotItem 内部随之保留。"""
        self.ci.clear()
        self.ci.setContentsMargins(8, 8, 8, 8)
        self.ci.setSpacing(4)
        focused = self._focused
        if focused:
            plot = self.plots.get(focused)
            if plot is None:
                self._focused = None
                focused = None
        if focused:
            self.ci.addItem(self.plots[focused], 0, 0, colspan=2)
            cap = self.cap_labels.get(focused)
            if cap is not None:
                self.ci.addItem(cap, 1, 0, colspan=2)
            self.ci.layout.setRowFixedHeight(0, -1)
            self._add_focus_highlight(self.plots[focused])
        else:
            for panel in get_panels():
                key = panel.key
                plot = self.plots.get(key)
                if plot is None:
                    continue
                self.ci.addItem(plot, panel.row, panel.col,
                                colspan=panel.colspan)
                self.ci.layout.setRowStretchFactor(panel.row, panel.stretch)
                cap = self.cap_labels.get(key)
                if cap is not None:
                    self.ci.addItem(cap, panel.row + 1, panel.col,
                                    colspan=panel.colspan)
        self.ci.layout.invalidate()
        self.update()

    # ── 视图 / 交互 ──
    def _fs(self, delta=0):
        return max(6, self._font_size + delta)

    def _font(self, delta=0, bold=False):
        f = QtGui.QFont()
        f.setFamily(FONT_CANDIDATES[0])
        f.setPointSize(self._fs(delta))
        f.setBold(bold)
        return f

    def _text(self, plot, x, y, text, color, anchor=(0.5, 0.5),
              delta=0, bold=False):
        ti = pg.TextItem(text, color=color, anchor=anchor)
        ti.setFont(self._font(delta, bold))
        ti.setPos(float(x), float(y))
        plot.addItem(ti)
        return ti

    def zoom_x_about(self, cx, factor):
        """兼容旧接口: 以数据坐标 cx 为锚点缩放 X (基类实现)。"""
        self.zoom_about(cx, factor)

    def _on_crosshair_moved(self, crosshair):
        """十字光标移动时更新状态栏显示当前面板数值。"""
        key = getattr(crosshair, "_panel_key", "")
        if key and hasattr(self, "_crosshair_values"):
            self._crosshair_values[key] = (crosshair._x, crosshair._y)
            self.crosshair_moved.emit(key, crosshair._x, crosshair._y)

    def keyPressEvent(self, ev):
        """键盘快捷键:
        - 1-8: 聚焦对应指标面板 (MACD/量能/价格/KDJ/RSI/OBV/量价分布/RS)
        - 0 / Escape / G: 恢复网格视图
        - H: 显示快捷键帮助
        - S: 同步所有面板缩放/平铺
        - C: 复制当前面板数据
        - 其余交给基类 (缩放/平移/复位/视图历史/十字光标步进)
        """
        if not self._has_data:
            return super().keyPressEvent(ev)
        key = ev.key()
        if key == Qt.Key.Key_1:
            self.focus_panel("macd")
            ev.accept()
            return
        if key == Qt.Key.Key_2:
            self.focus_panel("volume")
            ev.accept()
            return
        if key == Qt.Key.Key_3:
            self.focus_panel("price")
            ev.accept()
            return
        if key == Qt.Key.Key_4:
            self.focus_panel("kdj")
            ev.accept()
            return
        if key == Qt.Key.Key_5:
            self.focus_panel("rsi")
            ev.accept()
            return
        if key == Qt.Key.Key_6:
            self.focus_panel("obv")
            ev.accept()
            return
        if key == Qt.Key.Key_7:
            self.focus_panel("vp")
            ev.accept()
            return
        if key == Qt.Key.Key_8:
            self.focus_panel("rs")
            ev.accept()
            return
        if key in (Qt.Key.Key_0, Qt.Key.Key_Escape, Qt.Key.Key_G):
            if self._focused is not None:
                self.show_grid()
            ev.accept()
            return
        if key == Qt.Key.Key_H:
            self._show_shortcuts_help()
            ev.accept()
            return
        if key == Qt.Key.Key_S and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._toggle_sync_mode()
            ev.accept()
            return
        if key == Qt.Key.Key_C and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._copy_panel_data()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()

    def _show_shortcuts_help(self):
        """显示快捷键帮助对话框。"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("技术指标快捷键")
        dlg.setModal(True)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        text = (
            "<b>面板聚焦:</b><br>"
            "&nbsp;&nbsp;1-8 &nbsp;聚焦对应面板 (MACD/量能/价格/KDJ/RSI/OBV/量价分布/RS)<br>"
            "&nbsp;&nbsp;0 / Esc / G &nbsp;恢复网格视图<br><br>"
            "<b>视图控制:</b><br>"
            "&nbsp;&nbsp;滚轮 &nbsp;以光标为锚点缩放 X 轴<br>"
            "&nbsp;&nbsp;左键拖拽 &nbsp;平移<br>"
            "&nbsp;&nbsp;双击 &nbsp;复位全幅<br>"
            "&nbsp;&nbsp;↑/+/= &nbsp;放大, ↓/- &nbsp;缩小 (视图中心锚点)<br>"
            "&nbsp;&nbsp;←/→ &nbsp;平移, Home/r &nbsp;复位<br>"
            "&nbsp;&nbsp;Backspace/f &nbsp;视图历史后退/前进<br><br>"
            "<b>功能键:</b><br>"
            "&nbsp;&nbsp;H &nbsp;显示此帮助<br>"
            "&nbsp;&nbsp;Ctrl+S &nbsp;切换面板联动缩放模式<br>"
            "&nbsp;&nbsp;Ctrl+C &nbsp;复制当前面板数据"
        )
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(lab)
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(dlg.accept)
        lay.addWidget(btn_box)
        dlg.exec()

    def _toggle_sync_mode(self):
        """切换面板 X 轴联动模式。"""
        self._sync_x = not getattr(self, "_sync_x", True)
        if self._sync_x:
            for plot in self.plots.values():
                self._synced.add(plot)
        else:
            self._synced.clear()
        self._status(f"面板联动: {'开启' if self._sync_x else '关闭'}")

    def _status(self, msg):
        """发送状态栏消息 (由主窗口连接)。"""
        if hasattr(self, "status_message"):
            self.status_message.emit(msg)

    def _copy_panel_data(self):
        """复制当前聚焦面板或主面板数据到剪贴板。"""
        from PyQt6.QtWidgets import QApplication
        key = self._focused or "price"
        plot = self.plots.get(key)
        if not plot:
            return
        items = plot.listDataItems()
        if not items:
            return
        lines = [f"面板: {key}"]
        for item in items:
            if hasattr(item, "getData"):
                x, y = item.getData()
                if x is not None and y is not None:
                    lines.append(f"  数据点: {len(x)} 个")
                    break
        QApplication.clipboard().setText("\n".join(lines))
        self._status("已复制面板数据摘要到剪贴板")
