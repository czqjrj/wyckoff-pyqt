# -*- coding: utf-8 -*-
"""pyqtgraph 技术指标控件 (替换原 matplotlib 技术指标图, 排版/配色与原版一致)。

数据由 AnalysisThread 在 worker 线程通过 chart.build_ind_data() 收集 (指标计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

排版 (4×2 网格, 高度比 2.0:1.3:1.3:1.5):
  第一行: MACD (12,26,9) (跨两列, 最宽)
  第二行: 量能 (万手) | 价格 · 布林带 (20,2) · 大盘对比
  第三行: KDJ (9,3,3)  | RSI (6,12,24)
  第四行: OBV 能量潮   | 量价分布 (Volume Profile)
每个面板下方一行 "当前信号 → 预示" 解读 (颜色随信号红/绿/橙/灰, 与原版 set_xlabel 一致)。
默认显示全幅 (与原版一致, 无初始缩放)。

交互 (与原版一致):
  - 滚轮    以光标为锚点缩放 X 轴 (各面板 Y 为全量数据固定范围)
  - 左键拖拽 平移
  - 双击    复位到全幅
  - 键盘    + / - 缩放, 左/右箭头平移, Home/r 复位, Backspace/f 视图历史
"""
import datetime as _dt

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui
from pyqtgraph.Qt.QtCore import Qt
from PyQt6.QtWidgets import QScrollArea

from wyckoff.config import FONT_CANDIDATES

from . import theme
from .crosshair import Crosshair

# 与原版 matplotlib figsize=(8.5, 13.5) 的宽高比一致: 宽度铺满可视区, 高度按此比例。
_IND_ASPECT = 13.5 / 8.5

# 面板定义: (key, 初始标题, 所在行, 列, 跨列数, 行伸缩权重)
# 行号/列号对应 GraphicsLayout, 0-based; 解读行占下一行。
_PANEL_DEFS = [
    ("macd", "MACD (12,26,9)", 0, 0, 2, 20),
    ("volume", "量能 (万手)", 2, 0, 1, 13),
    ("price", "价格 · 布林带 (20,2) · 大盘对比", 2, 1, 1, 13),
    ("kdj", "KDJ (9,3,3)", 4, 0, 1, 13),
    ("rsi", "RSI (6,12,24)", 4, 1, 1, 13),
    ("obv", "OBV 能量潮", 6, 0, 1, 15),
    ("vp", "量价分布 (Volume Profile)", 6, 1, 1, 15),
    ("rs", "相对强度 RS (20日) vs 上证指数", 8, 0, 2, 11),
]


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


class _IndViewBox(pg.ViewBox):
    """指标面板 ViewBox: 滚轮只缩放 X, 双击复位。"""

    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def wheelEvent(self, ev, axis=None):
        pos = self.mapSceneToView(ev.scenePos())
        self._host.zoom_x_about(pos.x(), 0.8 if ev.delta() > 0 else 1.25)
        ev.accept()

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and ev.double():
            self._host.reset_view()
            ev.accept()
            return
        super().mouseClickEvent(ev)


class IndScroll(QScrollArea):
    """技术指标滚动容器: 宽度铺满可视区, 高度按 13.5:8.5 比例放大 (与原版一致)。

    与原版 _sync_ind_fig 相同的"宽度铺满、高度按比例、竖向滚动"行为, 保证
    面板长宽比例不随窗口形状被压扁。
    """

    def __init__(self, widget, aspect=_IND_ASPECT, parent=None):
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


class IndWidget(pg.GraphicsLayoutWidget):
    """pyqtgraph 技术指标: 4×2 网格 (与原版 matplotlib 排版一致), X 轴联动。"""

    def __init__(self, parent=None, font_size=11):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._n = 0
        self._days = []
        self._is_minute = False
        self._full_x = (0.0, 1.0)
        self._hist = []
        self._hist_pos = -1
        self._date_axes = {}
        self.plots = {}
        self.cap_labels = {}
        self._yranges = {}
        self._crosshairs = []

        self.setBackground(pg.mkColor(theme.C_PANEL))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._sync_lock = False
        self._build_plots()
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
        for key, title, row, col, colspan, stretch in _PANEL_DEFS:
            self._date_axes[key] = _DateAxis("bottom")
            vb = _IndViewBox(self, enableMenu=True)
            plot = pg.PlotItem(viewBox=vb,
                               axisItems={"bottom": self._date_axes[key]})
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
            plot.getViewBox().sigXRangeChanged.connect(self._on_x_range_changed)
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
        self._detach_crosshairs()
        self.ci.clear()
        self._n = 0
        self._days = []
        self._hist = []
        self._hist_pos = -1
        if not n or not close:
            self._build_plots()
            self.plots["price"].setTitle("暂无技术指标数据")
            return
        self._n = int(n)
        self._days = [_parse_day(d) for d in (day or [])]
        self._is_minute = bool(is_minute)
        x = np.arange(self._n) if x is None else np.asarray(x, dtype=float)
        self._full_x = (float(x[0]), float(x[-1]))

        self._build_plots()
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
        self.apply_view(*self._full_x, push=False)
        self._attach_crosshairs()

    # ── 十字光标 ──
    def _detach_crosshairs(self):
        for ch in self._crosshairs:
            ch.detach()
        self._crosshairs = []

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
        self._detach_crosshairs()
        if self._n <= 0:
            return
        fmt_x = self._fmt_x()
        fmt_vol_x = lambda v: f"{v:.1f}万手"
        for key in self.plots:
            fmt_y = {
                "price": lambda v: f"{v:.2f}",
                "volume": lambda v: f"{v:.1f}万手",
                "macd": lambda v: f"{v:.3f}",
                "kdj": lambda v: f"{v:.1f}",
                "rsi": lambda v: f"{v:.1f}",
                "obv": lambda v: f"{v:.0f}",
                "vp": lambda v: f"{v:.2f}",
                "rs": lambda v: f"{v:+.1f}%",
            }[key]
            fx = fmt_vol_x if key == "vp" else fmt_x
            self._crosshairs.append(
                Crosshair(self.plots[key], fx, fmt_y))

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
        close_line = plot.plot(x, c, pen=_pen("#1f2937", 1.1),
                               connect="finite")
        legend.addItem(close_line, "收盘")
        if boll_up is not None and boll_dn is not None:
            bu = np.asarray(boll_up, dtype=float)
            bd = np.asarray(boll_dn, dtype=float)
            plot.addItem(pg.FillBetweenItem(
                pg.PlotCurveItem(x, bu, connect="finite"),
                pg.PlotCurveItem(x, bd, connect="finite"),
                brush=_brush_alpha("#2563eb", 0.06)))
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
            idx_line = plot.plot(ix, ic, pen=_pen("#f08c00", 1.1),
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
                           pen=_pen("#f783ac", 0.9), connect="finite")
            legend.addItem(m5, "量MA5")
        if vol_ma10 is not None and len(vol_ma10):
            m10 = plot.plot(x, np.asarray(vol_ma10, dtype=float),
                            pen=_pen("#12b886", 0.9), connect="finite")
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
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen("#adb5bd", 0.7)))
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
                           pen=_pen("#1971c2", 0.9), connect="finite")
            legend.addItem(dl, "DIF")
        if dea is not None and len(dea):
            ea = plot.plot(x, np.asarray(dea, dtype=float),
                           pen=_pen("#f08c00", 0.9), connect="finite")
            legend.addItem(ea, "DEA")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yranges["macd"], padding=0)
        plot.setXRange(*self._full_x, padding=0)

    def _draw_kdj(self, x, k, d, j):
        plot = self.plots["kdj"]
        for yv in (20, 80):
            plot.addItem(pg.InfiniteLine(pos=yv, angle=0,
                                         pen=_pen("#adb5bd", 0.7,
                                                  Qt.PenStyle.DashLine,
                                                  alpha=0.6)))
        legend = plot.addLegend(offset=(12, 4))
        if k is not None and len(k):
            kl = plot.plot(x, np.asarray(k, dtype=float),
                           pen=_pen("#1971c2", 0.9), connect="finite")
            legend.addItem(kl, "K")
        if d is not None and len(d):
            dl = plot.plot(x, np.asarray(d, dtype=float),
                           pen=_pen("#f08c00", 0.9), connect="finite")
            legend.addItem(dl, "D")
        if j is not None and len(j):
            jl = plot.plot(x, np.asarray(j, dtype=float),
                           pen=_pen("#9c36b5", 0.7), connect="finite")
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
        for col, lab, r in (("#f783ac", "RSI6", r6),
                            ("#1971c2", "RSI12", r12),
                            ("#f08c00", "RSI24", r24)):
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
                           pen=_pen("#495057", 1.0), connect="finite")
            legend.addItem(ol, "OBV")
        if obv_ma is not None and len(obv_ma):
            ml = plot.plot(x, np.asarray(obv_ma, dtype=float),
                           pen=_pen("#1971c2", 0.9,
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
                                         pen=_pen("#d97706", 1.0,
                                                  Qt.PenStyle.DotLine)))
            plot.addItem(pg.InfiniteLine(pos=last, angle=0,
                                         pen=_pen("#1f2937", 1.0,
                                                  Qt.PenStyle.DotLine)))
            xmax = float(np.nanmax(vols))
            if not np.isfinite(xmax) or xmax <= 0:
                xmax = 1.0
            plot.setXRange(0.0, xmax * 1.15, padding=0)
            plot.setYRange(y0, y1, padding=0)
            plot.enableAutoRange(x=False, y=False)
            pad_y = (y1 - y0) * 0.03
            self._text(plot, xmax * 0.02, y1 - pad_y, f"POC {poc:.2f}",
                       "#d97706", anchor=(0, 1), delta=-2)
            self._text(plot, xmax * 1.13, y0 + pad_y, f"现价 {last:.2f}",
                       "#1f2937", anchor=(1, 0), delta=-2)
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
        if self._n == 0:
            return
        x0, x1 = self.plots["price"].getViewBox().viewRange()[0]
        full0, full1 = self._full_x
        span = x1 - x0
        full_span = full1 - full0
        new_span = min(max(span * factor, 15.0), full_span)
        if new_span >= full_span - 0.5:
            self.apply_view(full0, full1)
            return
        t = min(max((cx - x0) / span, 0.0), 1.0) if span > 0 else 0.5
        nx0 = cx - new_span * t
        nx1 = nx0 + new_span
        if nx0 < full0:
            nx0, nx1 = full0, full0 + new_span
        if nx1 > full1:
            nx1, nx0 = full1, full1 - new_span
        self.apply_view(nx0, nx1)

    def apply_view(self, x0, x1, push=True):
        if self._n == 0:
            return None
        x0 = max(float(x0), self._full_x[0])
        x1 = min(float(x1), self._full_x[1])
        if x1 - x0 < 2:
            return None
        self.plots["price"].getViewBox().setXRange(x0, x1, padding=0)
        if push:
            self._push_view(x0, x1)
        return (x0, x1)

    def reset_view(self):
        if self._n:
            self.apply_view(*self._full_x)

    def pan_by(self, frac):
        if self._n == 0:
            return
        x0, x1 = self.plots["price"].getViewBox().viewRange()[0]
        full0, full1 = self._full_x
        full_span = full1 - full0
        span = x1 - x0
        if full_span <= 0 or span <= 0:
            return
        if span >= full_span - 0.5:
            self.apply_view(full0, full1)
            return
        nx0 = min(max(x0 + span * frac, full0), full1 - span)
        self.apply_view(nx0, nx0 + span)

    def _push_view(self, x0, x1):
        key = (float(x0), float(x1))
        if self._hist and self._hist[self._hist_pos] == key:
            return
        self._hist = self._hist[:self._hist_pos + 1]
        self._hist.append(key)
        self._hist_pos = len(self._hist) - 1

    def nav_hist(self, step):
        if not self._hist:
            return
        pos = self._hist_pos + step
        if 0 <= pos < len(self._hist):
            self._hist_pos = pos
            x0, x1 = self._hist[pos]
            self.plots["price"].getViewBox().setXRange(x0, x1, padding=0)

    def _on_x_range_changed(self, vb, xrange):
        if self._n == 0 or self._sync_lock:
            return
        self._sync_lock = True
        try:
            x0, x1 = float(xrange[0]), float(xrange[1])
            for key, plot in self.plots.items():
                if key == "vp":
                    continue  # 量价分布 X 轴是成交量 (万手), 不随日期联动
                pvb = plot.getViewBox()
                if pvb is not vb:
                    pvb.setXRange(x0, x1, padding=0)
        finally:
            self._sync_lock = False

    def keyPressEvent(self, ev):
        if self._n == 0:
            return super().keyPressEvent(ev)
        key = ev.key()
        x0, x1 = self.plots["price"].getViewBox().viewRange()[0]
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_x_about((x0 + x1) / 2, 0.8)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_x_about((x0 + x1) / 2, 1.25)
        elif key == Qt.Key.Key_Left:
            self.pan_by(-0.2)
        elif key == Qt.Key.Key_Right:
            self.pan_by(0.2)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_R):
            self.reset_view()
        elif key == Qt.Key.Key_Backspace:
            self.nav_hist(-1)
        elif key in (Qt.Key.Key_F, Qt.Key.Key_End):
            self.nav_hist(1)
        else:
            super().keyPressEvent(ev)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
