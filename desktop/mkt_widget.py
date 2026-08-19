# -*- coding: utf-8 -*-
"""pyqtgraph 资金透视控件 (替换原 matplotlib plot_market 资金透视图)。

数据由 AnalysisThread 在 worker 线程通过 chart.build_market_data() 收集 (文本/
标题/解读均在 worker 侧算好), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

排版与原版 matplotlib plot_market 一致:
  标题行 + 估值卡行
  主力资金流向 (全宽单行)
  资金分项 | 当前筹码堆积形态
  股东户数变化 | 供需强度
  底部综合文案 + 解读提示 (全宽)

交互 (原版为静态图, 这里各面板独立操作):
  - 滚轮    以光标为锚点缩放 X 轴
  - 左键拖拽 平移
  - 双击    复位到全幅
  - 右键菜单由 main_window 提供 (保存图片/复位视图)
"""
import datetime as _dt

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import (C_UP, C_DOWN, FONT_CANDIDATES)

from . import theme
from .crosshair import Crosshair

_UP = C_UP
_DN = C_DOWN


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


def _parse_day(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    return None


class _DateAxis(pg.AxisItem):
    """X 轴日期刻度: 按整数柱索引回查日期渲染标签。"""

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._days = []

    def set_days(self, days):
        self._days = list(days)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._days):
                d = self._days[i]
                if d is None:
                    out.append("")
                else:
                    out.append(d.strftime("%y-%m-%d"))
            else:
                out.append("")
        return out


class _MktViewBox(pg.ViewBox):
    """资金透视面板 ViewBox: 滚轮只缩放本面板 X, 双击复位本面板。"""

    def __init__(self, host, key, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host
        self._key = key

    def wheelEvent(self, ev, axis=None):
        pos = self.mapSceneToView(ev.scenePos())
        self._host._active_key = self._key
        self._host.zoom_x_about(self._key, pos.x(),
                                0.8 if ev.delta() > 0 else 1.25)
        ev.accept()

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and ev.double():
            self._host.reset_plot(self._key)
            ev.accept()
            return
        super().mouseClickEvent(ev)


class MktWidget(pg.GraphicsLayoutWidget):
    """pyqtgraph 资金透视: 标题 + 估值卡 + 主力资金流向全宽 + 4面板 2×2 + 底部总结。"""

    def __init__(self, parent=None, font_size=11):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._n = 0
        self._full_x = {}
        self._date_axes = {}
        self.plots = {}
        self._empty = True
        self._active_key = "main_flow"
        self._crosshairs = []

        self.setBackground(pg.mkColor(theme.C_PANEL))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 高度下限保证 5 面板完整展示(widgetResizable 下宽度随视口填满)
        self.setMinimumHeight(1500)
        self._build_plots(empty=True)

    # ── 布局 ──
    def _build_plots(self, empty=False):
        self.ci.clear()
        self.ci.setContentsMargins(18, 14, 18, 14)
        self.ci.setSpacing(18)
        self.plots = {}
        self._date_axes = {}
        self._full_x = {}
        # 标题行
        title = self.ci.addLabel("", row=0, col=0, colspan=2, justify="center",
                                 color=theme.C_TEXT, bold=True,
                                 size=f"{self._fs(5)}pt")
        self.title_label = title
        # 估值卡行
        head = self.ci.addLabel("", row=1, col=0, colspan=2, justify="center",
                                size=f"{self._fs(1)}pt")
        self.header_label = head
        self.ci.layout.setRowStretchFactor(0, 2)
        self.ci.layout.setRowStretchFactor(1, 2)

        # 主力资金流向单独一行全宽 + 其余 4 面板 2×2
        row2 = self._add_plot("main_flow", "主力资金流向", 2, 0, colspan=2, stretch=63)
        row3 = self._add_plot("sub_flow", "资金分项", 3, 0, stretch=63)
        row4 = self._add_plot("chips", "当前筹码堆积形态", 3, 1, stretch=63)
        row5 = self._add_plot("holders", "股东户数变化", 4, 0, stretch=63)
        row6 = self._add_plot("sd", "供需强度", 4, 1, stretch=63)
        self.ci.layout.setRowStretchFactor(row2, 63)
        self.ci.layout.setRowStretchFactor(row3, 63)
        self.ci.layout.setRowStretchFactor(row4, 63)
        self.ci.layout.setRowStretchFactor(row5, 63)
        self.ci.layout.setRowStretchFactor(row6, 63)

        cap = self.ci.addLabel("", row=5, col=0, colspan=2, justify="center",
                               size=f"{self._fs(1)}pt", bold=True)
        self.cap_label = cap
        self.ci.layout.setRowStretchFactor(5, 3)
        ins = self.ci.addLabel("", row=6, col=0, colspan=2, justify="center",
                               size=f"{self._fs(0)}pt")
        self.insights_label = ins
        self.ci.layout.setRowStretchFactor(6, 3)

        if empty:
            self._n = 0
            self.title_label.setText("资金透视", color=theme.C_TEXT, bold=True,
                                     size=f"{self._fs(5)}pt")
            self._show_empty(True)

    def _add_plot(self, key, title, row, col, colspan=1, stretch=13):
        is_date = key != "chips"
        axis = _DateAxis("bottom") if is_date else pg.AxisItem("bottom")
        self._date_axes[key] = axis
        vb = _MktViewBox(self, key, enableMenu=True)
        plot = pg.PlotItem(viewBox=vb, axisItems={"bottom": axis})
        plot.setTitle(title, color=theme.C_MUTED, size=f"{self._fs(1)}pt",
                      **{"bold": True})
        plot.hideButtons()
        plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.getViewBox().setMouseEnabled(x=True, y=False)
        for ax_name in ("left", "bottom"):
            ax = plot.getAxis(ax_name)
            ax.setPen(_pen(theme.C["axis"], 1))
            ax.setTextPen(_pen(theme.C_TEXT, 1))
        self.ci.addItem(plot, row, col, colspan=colspan)
        self.plots[key] = plot
        return row

    def _show_empty(self, flag=True):
        self._empty = bool(flag)
        for key, plot in self.plots.items():
            plot.setTitle("", color=theme.C_MUTED, size=f"{self._fs(1)}pt")
            plot.clear()
        if flag:
            self.header_label.setText("")
            self.cap_label.setText("")
            self.insights_label.setText("")

    def _wrap_bottom_labels(self):
        """限制底部文案行文本宽度, 超宽自动换行, 避免横向溢出画布。"""
        w = max(self.width() - 64, 280)
        for lab in (self.header_label, self.cap_label, self.insights_label):
            if lab is not None and lab.item is not None:
                lab.item.setTextWidth(float(w))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "cap_label"):
            self._wrap_bottom_labels()

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
    def set_data(self, data=None, **extra):
        if isinstance(data, dict):
            d = data
        elif extra:
            d = extra
        else:
            d = None
        self._detach_crosshairs()
        self._build_plots(empty=True)
        if not d:
            self.title_label.setText("资金透视", color=theme.C_TEXT, bold=True,
                                     size=f"{self._fs(5)}pt")
            self._show_empty(True)
            return
        self._empty = False
        self.title_label.setText(d.get("title") or "资金透视", color=theme.C_TEXT,
                                 bold=True, size=f"{self._fs(5)}pt")
        header = d.get("header")
        if header:
            self.header_label.setText(
                f"<div style=\"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
                f"border-radius:6px;padding:6px 16px;color:{theme.C_TEXT};"
                f"letter-spacing:1px;\">"
                f"{header}</div>", size=f"{self._fs(1)}pt")
        else:
            self.header_label.setText("")

        self._draw_main_flow(d.get("main_flow"))
        self._draw_sub_flow(d.get("sub_flow"))
        self._draw_chips(d.get("chips"))
        self._draw_holders(d.get("holders"))
        self._draw_sd(d.get("sd"))

        cap_text = d.get("caps")
        cap_color = d.get("caps_color") or theme.C_TEXT
        self.cap_label.setText(cap_text or "", color=cap_color, bold=True,
                               size=f"{self._fs(1)}pt")
        insights_text = d.get("insights") or ""
        if insights_text:
            self.insights_label.setText(
                f"<div style=\"color:{theme.C_MUTED};letter-spacing:0.5px;\">"
                f"{insights_text}</div>", size=f"{self._fs(0)}pt")
        else:
            self.insights_label.setText("")
        self._wrap_bottom_labels()
        self._set_panel_title("main_flow", (d.get("main_flow") or {}).get("title"))
        self._set_panel_title("sub_flow", (d.get("sub_flow") or {}).get("title"))
        self._set_panel_title("chips", (d.get("chips") or {}).get("title"))
        self._set_panel_title("holders", (d.get("holders") or {}).get("title"))
        self._set_panel_title("sd", (d.get("sd") or {}).get("title"))
        chips = d.get("chips") or {}
        if chips.get("shape_txt"):
            self._set_chips_shape(chips["shape_txt"], chips["shape_color"])
        self._attach_crosshairs(d)
        self._relayout()

    def _relayout(self):
        """重建后强制重新计算 GraphicsLayout 行高 (stretch 分配)。"""
        self.ci.layout.invalidate()
        self.ci.resize(self.width(), self.height())
        self.update()

    # ── 十字光标 ──
    def _detach_crosshairs(self):
        for ch in self._crosshairs:
            ch.detach()
        self._crosshairs = []

    def _fmt_x_for(self, days):
        ds = [_parse_day(d) for d in (days or [])]

        def fmt(i):
            idx = int(round(i))
            if 0 <= idx < len(ds) and ds[idx] is not None:
                return ds[idx].strftime("%Y-%m-%d")
            return ""

        return fmt

    def _attach_crosshairs(self, d):
        self._detach_crosshairs()
        if not d:
            return
        mf = d.get("main_flow") or {}
        sf = d.get("sub_flow") or {}
        ho = d.get("holders") or {}
        sd = d.get("sd") or {}
        self._crosshairs.append(
            Crosshair(self.plots["main_flow"], self._fmt_x_for(mf.get("days")),
                      lambda v: f"{v / 1e8:+.2f}亿"))
        self._crosshairs.append(
            Crosshair(self.plots["sub_flow"], self._fmt_x_for(sf.get("days")),
                      lambda v: f"{v / 1e8:+.2f}亿"))
        self._crosshairs.append(
            Crosshair(self.plots["chips"],
                      lambda v: f"{v:.3f}", lambda v: f"{v:.2f}"))
        self._crosshairs.append(
            Crosshair(self.plots["holders"], self._fmt_x_for(ho.get("days")),
                      lambda v: f"{v:,.0f}"))
        self._crosshairs.append(
            Crosshair(self.plots["sd"], self._fmt_x_for(sd.get("days")),
                      lambda v: f"{v:,.0f}"))

    def _set_panel_title(self, key, title):
        if title:
            self.plots[key].setTitle(title, color=theme.C_TEXT, bold=True,
                                     size=f"{self._fs(1)}pt")

    def _set_chips_shape(self, shape_txt, color):
        plot = self.plots["chips"]
        t = plot.titleLabel.text
        # 第二行着色: 重设 titleLabel 的富文本
        plot.titleLabel.setText(
            f"<span style='color:{theme.C_TEXT};font-weight:bold;font-size:{self._fs(1)}pt;'>"
            f"{t}</span><br/>"
            f"<span style='color:{color};font-size:{self._fs(1)}pt;'>"
            f"{shape_txt}</span>")

    def _date_plot(self, key, days):
        if key in self._date_axes:
            self._date_axes[key].set_days([_parse_day(d) for d in days])
        plot = self.plots[key]
        n = len(days)
        self._full_x[key] = (0.0, float(max(n - 1, 1)))
        plot.setXRange(0.0, float(max(n - 1, 1)), padding=0)
        return plot, n

    # ── 各面板绘制 (配色/线型与 plot_market 一致) ──
    def _draw_main_flow(self, mf):
        plot = self.plots["main_flow"]
        if not mf:
            self._empty_panel("main_flow")
            return
        days = mf["days"]
        x = np.arange(len(days))
        vals = np.asarray(mf["vals"], dtype=float)
        plot, n = self._date_plot("main_flow", days)
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen("#9ca3af", 0.8)))
        brushes = [pg.mkBrush(_UP if v >= 0 else _DN) for v in vals]
        plot.addItem(pg.BarGraphItem(x=x, height=vals, width=0.72,
                                     brushes=brushes, pen=None))
        legend = plot.addLegend(offset=(12, 4))
        if mf.get("ma5"):
            l5 = plot.plot(x, np.asarray(mf["ma5"], dtype=float),
                           pen=_pen("#f08c00", 1.6), connect="finite")
            legend.addItem(l5, "5日均线")
        if mf.get("ma20"):
            l20 = plot.plot(x, np.asarray(mf["ma20"], dtype=float),
                            pen=_pen("#1971c2", 1.6), connect="finite")
            legend.addItem(l20, "20日均线")
        cum = np.asarray(mf["cum"], dtype=float)
        plot.addItem(pg.FillBetweenItem(
            pg.PlotCurveItem(x, cum, connect="finite"),
            pg.PlotCurveItem(x, np.zeros(len(x))),
            brush=_brush_alpha("#8a94a6", 0.08)))
        lc = plot.plot(x, cum, pen=_pen(theme.C_MUTED, 1.8,
                                        Qt.PenStyle.DashDotLine),
                       connect="finite")
        legend.addItem(lc, "累计净流入")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yrange(vals, cum), padding=0)

    def _draw_sub_flow(self, sf):
        plot = self.plots["sub_flow"]
        if not sf:
            self._empty_panel("sub_flow")
            return
        days = sf["days"]
        n = len(days)
        plot, n = self._date_plot("sub_flow", days)
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen("#9ca3af", 0.8)))
        legend = plot.addLegend(offset=(12, 4))
        if "super" in sf:
            x = np.arange(n)
            wd2 = 0.62
            specs = (("super", "#e03131", "超大单"), ("large", "#f08c00", "大单"),
                     ("mid", "#1971c2", "中单"), ("small", "#94a3b8", "小单"))
            ymax = 0.0
            for key, color, lab in specs:
                arr = np.asarray(sf[key], dtype=float)
                offsets = {"super": -1.5 * wd2 / 4, "large": -0.5 * wd2 / 4,
                           "mid": 0.5 * wd2 / 4, "small": 1.5 * wd2 / 4}
                bars = pg.BarGraphItem(x=x + offsets[key], height=arr,
                                       width=wd2 / 4, pen=None,
                                       brush=_brush_alpha(color, 0.9))
                plot.addItem(bars)
                legend.addItem(bars, lab)
                if len(arr):
                    ymax = max(ymax, float(np.nanmax(np.abs(arr))))
            plot.setYRange(-ymax * 1.15, ymax * 1.15, padding=0)
        else:
            x = np.arange(len(sf["vals"]))
            vals = np.asarray(sf["vals"], dtype=float)
            brushes = [pg.mkBrush(_UP if v >= 0 else _DN) for v in vals]
            plot.addItem(pg.BarGraphItem(x=x, height=vals, width=0.72,
                                         brushes=brushes, pen=None))
            plot.setYRange(*self._yrange0(vals), padding=0)
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

    def _draw_chips(self, chips):
        plot = self.plots["chips"]
        if not chips:
            self._empty_panel("chips")
            return
        prices = np.asarray(chips["prices"], dtype=float)
        weights = np.asarray(chips["weights"], dtype=float)
        cur = float(chips["cur"])
        poc = float(chips["poc"])
        h = (prices[1] - prices[0]) * 0.85 if len(prices) > 1 else 1.0
        brushes = [pg.mkBrush(_UP if p >= cur else _DN) for p in prices]
        plot.addItem(pg.BarGraphItem(x0=np.zeros(len(prices)), x1=weights,
                                     y=prices - h / 2, height=h,
                                     brushes=brushes, pen=None))
        plot.addItem(pg.InfiniteLine(pos=poc, angle=0,
                                     pen=_pen("#d97706", 1.4,
                                              Qt.PenStyle.DotLine)))
        plot.addItem(pg.InfiniteLine(pos=cur, angle=0,
                                     pen=_pen("#1f2937", 1.4,
                                              Qt.PenStyle.DotLine)))
        xmax = float(np.nanmax(weights))
        if not np.isfinite(xmax) or xmax <= 0:
            xmax = 1.0
        y0, y1 = float(prices[0]), float(prices[-1])
        pad_y = (y1 - y0) * 0.03
        self._text(plot, xmax * 0.02, y1 - pad_y, f"现价 {cur:.2f}",
                   "#1f2937", anchor=(0, 1), delta=-2, bold=True)
        self._text(plot, xmax * 1.13, y0 + pad_y, f"POC {poc:.2f}",
                   "#d97706", anchor=(1, 0), delta=-2, bold=True)
        plot.setXRange(0.0, xmax * 1.15, padding=0)
        plot.setYRange(y0, y1, padding=0)
        plot.enableAutoRange(x=False, y=False)
        self._full_x["chips"] = (0.0, xmax * 1.15)

    def _draw_holders(self, holders):
        plot = self.plots["holders"]
        if not holders:
            self._empty_panel("holders")
            return
        days = holders["days"]
        plot, n = self._date_plot("holders", days)
        x = np.arange(len(days))
        nums = np.asarray(holders["nums"], dtype=float)
        ratios = holders["ratios"]
        cols = [pg.mkBrush(_UP if r > 0 else _DN) for r in ratios]
        plot.addItem(pg.BarGraphItem(x=x, height=nums, width=0.6,
                                     brushes=cols, pen=None))
        plot.plot(x, nums, pen=_pen(theme.C_MUTED, 1.5), symbol="d", symbolSize=4,
                  symbolPen=pg.mkPen(theme.C_MUTED), symbolBrush=pg.mkBrush(theme.C_MUTED))
        for i, (xi, yi, lab) in enumerate(zip(x, nums, holders["labels"])):
            self._text(plot, float(xi), float(yi), lab, theme.C_MUTED,
                       anchor=(0.5, 0), delta=-3, bold=True)
        hi = float(np.nanmax(nums)) if len(nums) else 1.0
        plot.setYRange(0.0, max(hi * 1.25, 1e-9), padding=0)

    def _draw_sd(self, sd):
        plot = self.plots["sd"]
        if not sd:
            self._empty_panel("sd")
            return
        days = sd["days"]
        plot, n = self._date_plot("sd", days)
        x = np.arange(len(days))
        dem = np.asarray(sd["demand"], dtype=float)
        sup = np.asarray(sd["supply"], dtype=float)
        wd2 = 0.36
        legend = plot.addLegend(offset=(12, 4))
        bd = pg.BarGraphItem(x=x - wd2 / 2, height=dem, width=wd2, pen=None,
                             brush=pg.mkBrush(_UP))
        bs = pg.BarGraphItem(x=x + wd2 / 2, height=sup, width=wd2, pen=None,
                             brush=pg.mkBrush(_DN))
        plot.addItem(bd)
        plot.addItem(bs)
        legend.addItem(bd, "需求")
        legend.addItem(bs, "供给")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        hi = max(float(np.nanmax(dem)), float(np.nanmax(sup))) if len(dem) else 1.0
        plot.setYRange(0.0, max(hi * 1.15, 1e-9), padding=0)

    # ── 辅助 ──
    def _empty_panel(self, key):
        plot = self.plots[key]
        plot.clear()
        plot.setTitle("", color=theme.C_MUTED, size=f"{self._fs(1)}pt")
        self._full_x[key] = (0.0, 1.0)

    def _yrange(self, *series):
        parts = []
        for s in series:
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
        pad = (hi - lo) * 0.06
        return lo - pad, hi + pad

    def _yrange0(self, vals):
        hi = float(np.nanmax(np.abs(np.asarray(vals, dtype=float))))
        if not np.isfinite(hi) or hi <= 0:
            return 0.0, 1.0
        return -hi * 1.15, hi * 1.15

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

    # ── 视图 / 交互 (各面板独立) ──
    def zoom_x_about(self, key, cx, factor):
        full = self._full_x.get(key)
        plot = self.plots.get(key)
        if full is None or plot is None:
            return
        x0, x1 = plot.getViewBox().viewRange()[0]
        span = x1 - x0
        full_span = full[1] - full[0]
        new_span = min(max(span * factor, 3.0), full_span)
        if new_span >= full_span - 0.5:
            self.apply_view(key, *full)
            return
        t = min(max((cx - x0) / span, 0.0), 1.0) if span > 0 else 0.5
        nx0 = cx - new_span * t
        nx1 = nx0 + new_span
        if nx0 < full[0]:
            nx0, nx1 = full[0], full[0] + new_span
        if nx1 > full[1]:
            nx1, nx0 = full[1], full[1] - new_span
        self.apply_view(key, nx0, nx1)

    def apply_view(self, key, x0, x1):
        if self._empty:
            return
        plot = self.plots.get(key)
        full = self._full_x.get(key)
        if plot is None or full is None:
            return
        x0 = max(float(x0), full[0])
        x1 = min(float(x1), full[1])
        if x1 - x0 < 1e-6:
            return
        plot.getViewBox().setXRange(x0, x1, padding=0)

    def reset_plot(self, key):
        full = self._full_x.get(key)
        if full is not None:
            self.apply_view(key, *full)

    def reset_view(self):
        for key in self._full_x:
            self.reset_plot(key)

    def _pan_x(self, key, frac):
        plot = self.plots.get(key)
        if plot is None or self._empty:
            return
        x0, x1 = plot.getViewBox().viewRange()[0]
        self.apply_view(key, x0 + (x1 - x0) * frac, x1 + (x1 - x0) * frac)

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            frac = -0.1 if k == Qt.Key.Key_Left else 0.1
            for key in ("main_flow", "sub_flow", "holders", "sd"):
                self._pan_x(key, frac)
            ev.accept()
            return
        if k == Qt.Key.Key_Home:
            self.reset_view()
            ev.accept()
            return
        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            key = self._active_key
            plot = self.plots.get(key)
            if plot is not None and not self._empty:
                x0, x1 = plot.getViewBox().viewRange()[0]
                self.zoom_x_about(key, (x0 + x1) / 2, 0.7)
            ev.accept()
            return
        if k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            key = self._active_key
            plot = self.plots.get(key)
            if plot is not None and not self._empty:
                x0, x1 = plot.getViewBox().viewRange()[0]
                self.zoom_x_about(key, (x0 + x1) / 2, 1.4)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
