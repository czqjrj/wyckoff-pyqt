"""仪表盘图表控件 (pyqtgraph) — 让大盘状态一图看懂。

数据由主线程 set_data() 注入渲染, 控件不触网 (与 dashboard_widget 同模式)。
图表:
  - SseKlineChart   上证指数 K线 + MA20/50 + 成交量
  - IndexCompareChart  多指数归一化对比 (同基期 100)
  - SectorFlowChart 板块资金流横向条形图
  - SectorHeatmap   板块热力图 (方块=成交额, 颜色=涨跌幅)
"""
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget
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
            strip.setStyleSheet(f"background:{theme.C_ACCENT};border-radius:2px;")
            top.addWidget(strip)
            self.title_lab = QLabel(title)
            self.title_lab.setFont(_mk_font(11, bold=True))
            self.title_lab.setStyleSheet(f"color:{theme.C_TEXT};background:transparent;")
            top.addWidget(self.title_lab)
            self.sub_lab = QLabel("")
            self.sub_lab.setFont(_mk_font(9))
            self.sub_lab.setStyleSheet(f"color:{theme.C_MUTED};background:transparent;")
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
                f"color:{theme.C_MUTED};background:transparent;")
        self.title_lab.setStyleSheet(f"color:{theme.C_TEXT};background:transparent;")


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
                f"color:{ec};background:transparent;")
        else:
            self.sub_lab.setStyleSheet(f"color:{theme.C_MUTED};background:transparent;")
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
        self.plot.getAxis("bottom").setStyle(tickFont=_mk_font(11))
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
                               width=0.62, brushes=[pg.mkBrush(c) for c in colors],
                               pen=None)
        self.plot.addItem(bars)
        for i, n in enumerate(names):
            color = colors[i]
            ti = pg.TextItem(n, color=color, anchor=(0.5, 1))
            ti.setFont(_mk_font(11))
            ti.setPos(i, -top * 0.05)
            self.plot.addItem(ti)
        self.plot.getAxis("bottom").setTicks([[(i, n) for i, n in enumerate(names)]])
        self.plot.getAxis("left").setTicks([])

    def clear(self):
        self.plot.setBackground(theme.C_PANEL)
        self.plot.clear()
        self._repaint_theme()


# ── 板块热力图 ──


class _HeatCell:
    __slots__ = ("name", "pct", "amount_yi", "rect", "color")

    def __init__(self, name, pct, amount_yi):
        self.name = name
        self.pct = pct
        self.amount_yi = amount_yi
        self.rect = None
        self.color = theme.C_MUTED


class _HeatGrid(QWidget):
    """自绘热力图网格: 行高∝该行成交额合计, 块宽∝板块成交额占比, 色=涨跌幅。

    红涨绿跌、深浅按幅度走 (theme.C_UP/C_DOWN + alpha), hover 显示明细。
    """

    COLUMNS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._cells: list[_HeatCell] = []
        self._hover: _HeatCell | None = None

    def set_cells(self, cells):
        self._cells = cells
        self._hover = None
        self.update()

    def clear(self):
        self._cells = []
        self._hover = None
        self.update()

    def _cell_color(self, cell):
        pct = cell.pct
        if pct == 0:
            return QColor(theme.C_MUTED)
        base = QColor(theme.C_UP if pct > 0 else theme.C_DOWN)
        # 幅度 → 亮度 (不透明, 保留色相): 满幅=主题原色最亮, 小幅=加深但可辨
        ratio = min(1.0, abs(pct) / 1.5)
        hsv = base.toHsv()
        hue = max(hsv.hue(), 0)
        sat = max(hsv.saturation(), 0)
        value = int((0.78 + 0.22 * ratio) * 255)
        return QColor.fromHsv(hue, sat, value, hsv.alpha())

    def _text_color(self, cell):
        return QColor("#ffffff") if cell.pct != 0 else QColor(theme.C_MUTED)

    def _layout(self, inner_w, inner_h, gap):
        cells = self._cells
        if not cells:
            return
        rows = (len(cells) + self.COLUMNS - 1) // self.COLUMNS
        total_all = sum(c.amount_yi for c in cells) or 0
        usable_w = inner_w - gap * (self.COLUMNS - 1)
        usable_h = inner_h - gap * (rows - 1)
        top = 0.0
        for r in range(rows):
            row_cells = cells[r * self.COLUMNS:(r + 1) * self.COLUMNS]
            row_total = sum(c.amount_yi for c in row_cells)
            if total_all > 0:
                h = usable_h * row_total / total_all
            else:
                h = usable_h / rows
            left = 0.0
            for c in row_cells:
                w = (usable_w * c.amount_yi / row_total
                     if row_total > 0 else usable_w / len(row_cells))
                c.rect = (float(left), float(top),
                          float(max(w - gap, 8)), float(max(h - gap, 8)))
                left += w
            top += h

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        gap = 3
        if not self._cells:
            p.setPen(QColor(theme.C_MUTED))
            p.setFont(_mk_font(10))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无板块数据")
            p.end()
            return
        self._layout(rect.width(), rect.height(), gap)
        name_font = _mk_font(13, bold=True)
        pct_font = _mk_font(11)
        for c in self._cells:
            if not c.rect:
                continue
            x, y, w, h = c.rect
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._cell_color(c))
            p.drawRoundedRect(QRectF(x, y, w, h), 3, 3)
            if w < 34 or h < 24:
                continue
            p.setPen(self._text_color(c))
            inner = QRectF(x + 5, y + 3, w - 10, h - 6)
            if h >= 38:
                p.setFont(name_font)
                fm = QFontMetrics(name_font)
                name = c.name
                if fm.horizontalAdvance(name) > inner.width():
                    name = fm.elidedText(name, Qt.TextElideMode.ElideRight,
                                         int(inner.width()))
                p.drawText(inner, Qt.AlignmentFlag.AlignTop
                           | Qt.AlignmentFlag.AlignHCenter, name)
                p.setFont(pct_font)
                p.drawText(QRectF(x, y + h - 20, w, 18),
                           Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignBottom,
                           f"{c.pct:+.2f}%")
            else:
                p.setFont(pct_font)
                p.drawText(inner, Qt.AlignmentFlag.AlignCenter,
                           f"{c.pct:+.2f}%")
        p.end()

    def _cell_at(self, pos):
        for c in self._cells:
            if c.rect is None:
                continue
            x, y, w, h = c.rect
            if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                return c
        return None

    def mouseMoveEvent(self, ev):
        c = self._cell_at(ev.position())
        if c is not self._hover:
            self._hover = c
            self.update()
        if c:
            QToolTip.showText(
                ev.globalPosition().toPoint(),
                f"{c.name}   {c.pct:+.2f}%\n成交额 {c.amount_yi:,.2f} 亿",
                self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, ev):
        self._hover = None
        QToolTip.hideText()
        self.update()


class SectorHeatmap(_ChartCard):
    """板块热力图 (方块=成交额, 颜色=涨跌幅)。"""

    def __init__(self, parent=None):
        super().__init__("板块热力图 · 量价", height=360, parent=parent)
        self.grid = _HeatGrid()
        self.grid.setMinimumHeight(300)
        self.plot_host.addWidget(self.grid)

    def set_data(self, rows):
        self.clear()
        if not rows:
            return
        cells = [_HeatCell(r["name"], r.get("pct", 0),
                           r.get("amount_yi", 0)) for r in rows]
        self.grid.set_cells(cells)
        total = sum(c.amount_yi for c in cells)
        self.sub_lab.setText(f"Top {len(rows)} · 合计 {total:,.0f} 亿")
        self.sub_lab.setStyleSheet(
            f"color:{theme.C_MUTED};background:transparent;")

    def clear(self):
        self.grid.clear()
        self._repaint_theme()

# ── 指数共振度 ──


class ResonanceChart(_ChartCard):
    """多指数每日涨跌方向一致率曲线 (0-100%)."""

    def __init__(self, parent=None):
        super().__init__("指数共振 · 120日", height=210, parent=parent)
        self.plot = pg.PlotWidget(background=theme.C_PANEL)
        self.plot.hideAxis("left")
        self.plot.showGrid(x=False, y=True, alpha=0.25)
        self.plot.getAxis("bottom").setStyle(tickFont=_mk_font(7))
        self.plot_host.addWidget(self.plot)
        self.plot.setYRange(0, 100)

    def set_data(self, data):
        self.clear()
        if not data or not data.get("agree"):
            _no_data(self.plot)
            return
        agree = data["agree"]
        days = data.get("days") or []
        x = np.arange(len(agree))
        pen = _pen(theme.C_ACCENT, 1.4)
        self.plot.plot(x, agree, pen=pen, fillLevel=50,
                       brush=pg.mkBrush(QColor(theme.css_rgba(theme.C_ACCENT, 40))))
        for lv in (33, 67):
            self.plot.addItem(pg.InfiniteLine(
                pos=lv, angle=0, pen=pg.mkPen(theme.C_MUTED, style=Qt.PenStyle.DashLine)))
        if len(days) == len(x) and len(x) > 1:
            step = max(1, (len(x) - 1) // 6)
            ticks = [(float(i), days[i][5:]) for i in range(0, len(x), step)]
            if ticks[-1][0] != float(len(x) - 1):
                ticks.append((float(len(x) - 1), days[-1][5:]))
            self.plot.getAxis("bottom").setTicks([ticks])
        today = data.get("today_pct")
        tone = data.get("tone", "")
        tone_map = {"共振": theme.C_UP, "分歧": theme.C_DOWN, "分化": theme.C_AMBER}
        if today is not None:
            tc = tone_map.get(tone, theme.C_MUTED)
            self.sub_lab.setText(f"今日一致率 {today:.0f}% · {tone}")
            self.sub_lab.setStyleSheet(
                f"color:{tc};background:transparent;" if tc else
                f"color:{theme.C_MUTED};background:transparent;")

    def clear(self):
        self.plot.setBackground(theme.C_PANEL)
        self.plot.clear()
        self.plot.setYRange(0, 100)
        self._repaint_theme()
