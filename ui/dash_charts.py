"""仪表盘图表控件 (pyqtgraph) — 让大盘状态一图看懂。

数据由主线程 set_data() 注入渲染, 控件不触网 (与 dashboard_widget 同模式)。
三个图表:
  - SseKlineChart   上证指数 K线 + MA20/50 + 成交量
  - IndexCompareChart  多指数归一化对比 (同基期 100)
  - SectorFlowChart 板块资金流横向条形图
"""
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from pyqtgraph.Qt import QtGui

from . import theme
from .kline_widget import CandlestickItem


def _pen(color, width=1.0):
    """主题感知画笔 (alpha 4位色)."""
    pen = pg.mkPen(color)
    pen.setCosmetic(True)
    pen.setWidthF(width)
    return pen


def _mk_font(size, bold=False):
    f = QtGui.QFont()
    f.setFamily(theme.mono_font_family())
    f.setPointSize(size)
    f.setBold(bold)
    return f


class _ChartCard(QWidget):
    """图表卡片容器: 透明背景 (由外层 Card 提供面板), 顶部为绿色强调条+标题条。

    标题条可选: title 为空时只渲染图表, 便于嵌入带 PanelHeader 的卡片。
    """

    def __init__(self, title="", height=0, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.title_lab = None
        self.sub_lab = None
        if title:
            top = QHBoxLayout()
            top.setSpacing(8)
            strip = QFrame()
            strip.setFixedSize(4, 14)
            strip.setObjectName("chartAccent")
            strip.setStyleSheet("background:%s;border-radius:2px;" % theme.C_ACCENT)
            top.addWidget(strip)
            self.title_lab = QLabel(title)
            self.title_lab.setFont(_mk_font(11, bold=True))
            self.title_lab.setStyleSheet("color:%s;background:transparent;" % theme.C_TEXT)
            top.addWidget(self.title_lab)
            self.sub_lab = QLabel("")
            self.sub_lab.setFont(_mk_font(9))
            self.sub_lab.setStyleSheet("color:%s;background:transparent;" % theme.C_MUTED)
            top.addWidget(self.sub_lab)
            top.addStretch(1)
            v.addLayout(top)
        self.plot_host = QVBoxLayout()
        self.plot_host.setContentsMargins(0, 0, 0, 0)
        self.plot_host.setSpacing(0)
        v.addLayout(self.plot_host, 1)
        if height:
            self.setMinimumHeight(height)

    def _repaint_theme(self):
        if self.title_lab is None:
            return
        if self.sub_lab is not None:
            self.sub_lab.setStyleSheet(
                "color:%s;background:transparent;" % theme.C_MUTED)
        self.title_lab.setStyleSheet("color:%s;background:transparent;" % theme.C_TEXT)


def _no_data(plot, text="暂无数据"):
    ti = pg.TextItem(text, color=theme.C_MUTED, anchor=(0.5, 0.5))
    ti.setFont(_mk_font(12))
    plot.addItem(ti, ignoreBounds=True)


# ── 上证指数 K线 ──


class SseKlineChart(_ChartCard):
    """上证指数 K线 + MA20/MA50 + 成交量 (上下两窗联动)."""

    def __init__(self, parent=None):
        super().__init__("上证指数走势 · K线", height=300, parent=parent)
        self.gw = pg.GraphicsLayoutWidget()
        self.gw.setBackground(theme.C_PANEL)
        self.gw.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self.plot_host.addWidget(self.gw)

        self.price = self.gw.addPlot(row=0, col=0)
        self.price.hideAxis("left")
        self.price.setMouseEnabled(x=True, y=True)
        self.vol = self.gw.addPlot(row=1, col=0)
        self.vol.hideAxis("left")
        self.vol.setXLink(self.price)
        self.gw.ci.layout.setRowStretchFactor(0, 3)
        self.gw.ci.layout.setRowStretchFactor(1, 1)

        for p in (self.price, self.vol):
            p.showGrid(x=False, y=True, alpha=0.25)
            p.getAxis("bottom").setStyle(tickFont=_mk_font(7))
            p.getViewBox().setDefaultPadding(0.02)
            p.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))

        self._candle = None
        self._ma20 = None
        self._ma50 = None
        self._vol_item = None

    def set_data(self, chart):
        """chart: build_sse_chart() 返回 dict 或 None。"""
        self.clear()
        if not chart:
            _no_data(self.price)
            return
        x = np.asarray(chart["x"], dtype=float)
        self._candle = CandlestickItem(
            x, chart["open"], chart["close"], chart["low"], chart["high"],
            up_color=theme.C_UP, down_color=theme.C_DOWN)
        self.price.addItem(self._candle)

        ma20 = np.asarray(chart["ma20"], dtype=float)
        ma50 = np.asarray(chart["ma50"], dtype=float)
        self._ma20 = self.price.plot(x, ma20, pen=_pen("#f08c00", 1.2),
                                     connect="finite")
        self._ma50 = self.price.plot(x, ma50, pen=_pen("#1971c2", 1.2),
                                     connect="finite")
        legend = self.price.addLegend(offset=(8, 6), labelTextColor=theme.C_TEXT,
                                      brush=pg.mkBrush(theme.C_PANEL))
        legend.addItem(self._ma20, "MA20")
        legend.addItem(self._ma50, "MA50")

        up = np.asarray(chart["close"]) >= np.asarray(chart["open"])
        vol = np.asarray(chart["volume"], dtype=float) / 1e8  # 亿手
        brushes = [pg.mkBrush(theme.C_UP if u else theme.C_DOWN) for u in up]
        self._vol_item = pg.BarGraphItem(x=x, height=vol, width=0.6,
                                         brushes=brushes, pen=None)
        self.vol.addItem(self._vol_item)
        self.vol.getAxis("left").setTicks([])

        env = chart.get("env", "")
        tone = chart.get("tone", "")
        last = chart.get("last_close", 0)
        subtxt = f"最新 {last:,.2f}"
        if env:
            ec = theme.TONE_COLOR.get(tone, theme.C_MUTED)
            subtxt += f" · 大盘 {env}"
            self.sub_lab.setStyleSheet(
                "color:%s;background:transparent;" % ec)
        else:
            self.sub_lab.setStyleSheet("color:%s;background:transparent;" % theme.C_MUTED)
        self.sub_lab.setText(subtxt)
        self.price.setXRange(x[0], x[-1], padding=0.02)
        # 底部日期刻度 (采样 ~6 个)
        days = chart.get("days") or []
        if len(days) == len(x) and len(x) > 1:
            step = max(1, (len(x) - 1) // 6)
            ticks = [(float(i), days[i][5:]) for i in range(0, len(x), step)]
            if ticks[-1][0] != float(len(x) - 1):
                ticks.append((float(len(x) - 1), days[-1][5:]))
            for p in (self.price, self.vol):
                p.getAxis("bottom").setTicks([ticks])

    def clear(self):
        self.gw.setBackground(theme.C_PANEL)
        for p in (self.price, self.vol):
            p.clear()
            p.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
        self._repaint_theme()
        self._candle = self._ma20 = self._ma50 = self._vol_item = None


# ── 多指数对比 ──


class IndexCompareChart(_ChartCard):
    """多指数同基期归一化走势对比。"""

    def __init__(self, parent=None):
        super().__init__("多指数对比 · 基期100", height=260, parent=parent)
        self.plot = pg.PlotWidget(background=theme.C_PANEL)
        self.plot.hideAxis("left")
        self.plot.showGrid(x=False, y=True, alpha=0.25)
        self.plot.getAxis("bottom").setStyle(tickFont=_mk_font(7))
        self.plot_host.addWidget(self.plot)
        self._curves = []

    def set_data(self, data):
        self.clear()
        if not data or not data.get("series"):
            _no_data(self.plot)
            return
        palette = [theme.C_UP, theme.C_DOWN, theme.C_AMBER, theme.C_ACCENT,
                   theme.C_MUTED, "#0ea5e9"]
        n = max(len(s["data"]) for s in data["series"])
        for i, s in enumerate(data["series"]):
            color = palette[i % len(palette)]
            x = np.arange(len(s["data"]))
            c = self.plot.plot(x, s["data"], pen=_pen(color, 1.4),
                               connect="finite")
            self._curves.append(c)
        # 图例
        legend = self.plot.addLegend(offset=(6, 4), labelTextColor=theme.C_TEXT,
                                     brush=pg.mkBrush(theme.C_PANEL))
        for i, s in enumerate(data["series"]):
            legend.addItem(self._curves[i], s.get("name", ""))
        # 底部日期刻度
        days = data.get("days") or []
        if len(days) == n and n > 1:
            step = max(1, (n - 1) // 6)
            ticks = [(float(i), days[i][5:]) for i in range(0, n, step)]
            if ticks[-1][0] != float(n - 1):
                ticks.append((float(n - 1), days[-1][5:]))
            self.plot.getAxis("bottom").setTicks([ticks])

    def clear(self):
        self.plot.setBackground(theme.C_PANEL)
        self.plot.clear()
        self._repaint_theme()
        self._curves = []


# ── 板块资金条形图 ──


class SectorFlowChart(_ChartCard):
    """板块资金流横向条形图 (正流红/负流绿)."""

    def __init__(self, parent=None):
        super().__init__("板块资金流 · 5日累计", height=260, parent=parent)
        self.plot = pg.PlotWidget(background=theme.C_PANEL)
        self.plot.hideAxis("left")
        self.plot.showGrid(x=True, y=False, alpha=0.25)
        self.plot.getAxis("bottom").setStyle(tickFont=_mk_font(7))
        self.plot_host.addWidget(self.plot)

    def set_data(self, rows):
        self.clear()
        if not rows:
            _no_data(self.plot)
            return
        names = [r["name"][:5] + ("…" if len(r["name"]) > 5 else "")
                 for r in rows]
        flows = [r["flow"] for r in rows]
        x = np.arange(len(rows))
        top = max((abs(f) for f in flows), default=1.0) or 1.0
        colors = [theme.C_UP if f > 0 else theme.C_DOWN
                  if f < 0 else theme.C_MUTED for f in flows]
        bars = pg.BarGraphItem(x=x, height=np.asarray(flows, dtype=float),
                               width=0.6, brushes=[pg.mkBrush(c) for c in colors],
                               pen=None)
        self.plot.addItem(bars)
        for i, n in enumerate(names):
            color = colors[i]
            ti = pg.TextItem(n, color=color, anchor=(0.5, 1))
            ti.setFont(_mk_font(7))
            ti.setPos(i, -top * 0.06)
            self.plot.addItem(ti)
        self.plot.getAxis("bottom").setTicks([[(i, n) for i, n in enumerate(names)]])
        self.plot.getAxis("left").setTicks([])

    def clear(self):
        self.plot.setBackground(theme.C_PANEL)
        self.plot.clear()
        self._repaint_theme()