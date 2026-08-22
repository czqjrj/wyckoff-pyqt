# -*- coding: utf-8 -*-
"""pyqtgraph K线图控件 (替换原 matplotlib K线图)。

数据由 AnalysisThread 在 worker 线程通过 build_kline_data() 收集 (K线相关计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

交互 (由 desktop.base_plot.BasePlotWidget 基类统一提供):
  - 滚轮    以光标为锚点缩放 X 轴 (Y 随可见区间自动重算)
  - 左键拖拽 平移
  - 双击    复位到全幅
  - 键盘    上箭头 / + 放大, 下箭头 / - 缩小 (以视图中心为锚点),
            左/右箭头平移, Home/r 复位, Backspace/f 视图历史
  - 单击    事件/VSA 文本标签 → labelClicked 信号 (由主窗口弹窗解释)
"""
import math

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import (FONT_CANDIDATES, W_RECENT, _PHASE_STYLE)

from . import theme
from .base_plot import BasePlotWidget
from .crosshair import Crosshair

# A股配色: 红涨绿跌 (运行时从 theme 动态取色, 支持主题切换)


def _feedback_verdicts(df, segs, symbol, scale):
    """按 标的+周期+起止时间 关联阶段带的反馈标注, 返回 {(key,a,e): verdict}。

    各带 正确/错误 徽标直接画在 K 线上, 一眼可辨阶段判定靠不靠谱。"""
    if not segs or not symbol:
        return {}
    try:
        from wyckoff.storage import feedback_key, load_feedback, _day_fmt
    except Exception:
        return {}
    fbmap = {}
    try:
        for r in load_feedback():
            if r.get("start_dt") and r.get("end_dt"):
                fbmap[feedback_key(r["symbol"], r.get("scale", 240),
                                   r["start_dt"], r["end_dt"])] = r
    except Exception:
        return {}
    out = {}
    for a, e, key, _label in segs:
        if not (0 <= a < e < len(df)):
            continue
        k = feedback_key(symbol, int(scale), _day_fmt(df["day"].iloc[a]),
                         _day_fmt(df["day"].iloc[e]))
        r = fbmap.get(k)
        if r and r.get("verdict"):
            out[(key, int(a), int(e))] = r.get("verdict"), r.get("source")
    return out

_MA_LINES = [
    ("price_ma5", "#f783ac", "MA5"),
    ("price_ma10", "#12b886", "MA10"),
    ("price_ma20", "#1971c2", "MA20"),
    ("price_ma50", "#f08c00", "MA50"),
    ("price_ma200", "#adb5bd", "MA200"),
]

_VOL_MA_LINES = [
    ("vol_ma5", "#f783ac", "量MA5"),
    ("vol_ma10", "#12b886", "量MA10"),
    ("vol_ma20", "#1971c2", "量MA20"),
]

_VSA_DRAW = {"CHOC", "UPT", "TRU", "TRD", "DEM", "SUP", "ABS",
             "TEST", "ETR", "ETF", "BC", "SV"}

# 默认视野显示的K线根数。K线图实际绘制宽度约 800~1000px (被自选股/分析
# 面板瓜分), 满幅(近3年≈700根)每根不足2px、250根也只有~4px, 实体不可辨,
# 看起来只剩均线与色带。默认聚焦最近 N 根(≈12px/根)让蜡烛清晰可见,
# 与主流看盘软件默认缩放一致 (Home/双击可看全幅)。
_DEFAULT_BARS = 80


def _brush_alpha(color, alpha):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    return pg.mkBrush(c)


def _pen(color, width=1.0, style=None):
    kw = {"width": width}
    if style is not None:
        kw["style"] = style
    return pg.mkPen(color, **kw)


class CandlestickItem(pg.GraphicsObject):
    """蜡烛图: 实时逐根绘制, 影线用 0 宽画笔 (任意缩放下恒为 1px 细线)。

    注意不能用 QPicture 预生成: 录制时的画笔宽度会随回放变换等比缩放,
    默认视野下影线被放大成与实体同宽的粗柱, 蜡烛看起来像柱状图。
    影线用 hairline 画笔 (width=0) 保证任何缩放级别都保持 1px。
    """

    def __init__(self, x, opens, closes, lows, highs, width=0.6,
                 up_color=None, down_color=None):
        super().__init__()
        self._x = np.asarray(x, dtype=float)
        self._o = np.asarray(opens, dtype=float)
        self._c = np.asarray(closes, dtype=float)
        self._l = np.asarray(lows, dtype=float)
        self._h = np.asarray(highs, dtype=float)
        self._width = width
        self._up = pg.mkColor(up_color or theme.C_UP)
        self._dn = pg.mkColor(down_color or theme.C_DOWN)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def paint(self, p, *args):
        half = self._width / 2.0
        wick = QtGui.QPen()
        wick.setWidth(0)
        for xi, o, c, lo, hi in zip(self._x, self._o, self._c,
                                    self._l, self._h):
            if not (np.isfinite(lo) and np.isfinite(hi)):
                continue
            col = self._up if c >= o else self._dn
            wick.setColor(col)
            p.setPen(wick)
            p.drawLine(QtCore.QPointF(xi, lo), QtCore.QPointF(xi, hi))
            p.setPen(QtGui.QPen(Qt.PenStyle.NoPen))
            p.setBrush(QtGui.QBrush(col))
            p.drawRect(QtCore.QRectF(xi - half, min(o, c), self._width,
                                     max(abs(c - o), 0.04)))

    def boundingRect(self):
        if self._x.size == 0:
            return QtCore.QRectF(0, 0, 1, 1)
        x0, x1 = float(self._x.min()) - 1, float(self._x.max()) + 1
        y0, y1 = float(np.nanmin(self._l)), float(np.nanmax(self._h))
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class PhaseBands(pg.GraphicsObject):
    """威科夫阶段底色带: 垂直色带铺满价格区间, 画在蜡烛之下。"""

    def __init__(self, bands, y_range):
        super().__init__()
        self._bands = bands            # [(x0, x1, color, alpha, label)]
        self._yr = y_range
        self._picture = None
        self._generate()

    def _generate(self):
        pict = QtGui.QPicture()
        p = QtGui.QPainter(pict)
        y = 1e6
        for x0, x1, color, alpha, _label in self._bands:
            c = pg.mkColor(color)
            c.setAlphaF(float(alpha))
            p.fillRect(QtCore.QRectF(x0 - 0.5, -y, (x1 - x0) + 1, 2 * y),
                       QtGui.QBrush(c))
        p.end()
        self._picture = pict

    def paint(self, p, *args):
        p.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if not self._bands:
            return QtCore.QRectF(0, 0, 1, 1)
        y0, y1 = self._yr
        x0 = min(b[0] for b in self._bands) - 0.5
        x1 = max(b[1] for b in self._bands) + 0.5
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class _DateAxis(pg.AxisItem):
    """X 轴日期刻度: 按整数柱索引回查 df['day'] 渲染日期标签。"""

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
                if self._minute:
                    out.append(d.strftime("%m-%d %H:%M"))
                else:
                    out.append(d.strftime("%y-%m-%d"))
            else:
                out.append("")
        return out


class _KlineViewBox(pg.ViewBox):
    """价格/量能/累计量 ViewBox: 滚轮缩放由基类控件层接管, 此处只处理
    双击复位与单击标签触发解释。"""

    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            if ev.double():
                self._host.reset_view()
                ev.accept()
                return
            label = self._host.hit_label(ev.scenePos())
            if label:
                self._host.labelClicked.emit(label[0], label[1])
                ev.accept()
                return
        super().mouseClickEvent(ev)


class KlineWidget(BasePlotWidget):
    """pyqtgraph K线图: 价格 / 成交量 / 波段累计量 三栏, X 轴联动。

    继承 BasePlotWidget 获得统一交互 (滚轮/键盘/拖拽边界/双击复位/视图历史)。
    set_data(**kline_data) 接收 build_kline_data() 的返回。
    """

    labelClicked = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._days = []
        self._is_minute = False
        self._n = 0
        self._fb_verdicts = {}
        self._chart_lo = np.array([])
        self._chart_hi = np.array([])
        self._full_x = (0.0, 1.0)
        self._full_y = (0.0, 1.0)
        self._full_vol = (0.0, 1.0)
        self._date_axis = None
        self.price_plot = None
        self.vol_plot = None
        self.cum_plot = None
        self._build_plots()
        self.price_plot.setTitle("输入 A 股代码 (如 600104 / sh600104 / 000001), "
                                 "点击\"开始分析\"加载 K 线图。")

    # ── 布局 ──
    def _build_plots(self):
        self.ci.clear()
        self.ci.setContentsMargins(8, 8, 8, 8)
        self.ci.setSpacing(6)

        price_vb = _KlineViewBox(self, enableMenu=True)
        self.price_plot = pg.PlotItem(viewBox=price_vb)
        vol_vb = _KlineViewBox(self, enableMenu=True)
        self.vol_plot = pg.PlotItem(viewBox=vol_vb)
        self._date_axis = _DateAxis("bottom")
        cum_vb = _KlineViewBox(self, enableMenu=True)
        self.cum_plot = pg.PlotItem(viewBox=cum_vb,
                                    axisItems={"bottom": self._date_axis})

        self.ci.addItem(self.price_plot, 0, 0)
        self.ci.addItem(self.vol_plot, 1, 0)
        self.ci.addItem(self.cum_plot, 2, 0)
        gl = self.ci.layout
        gl.setRowStretchFactor(0, 32)
        gl.setRowStretchFactor(1, 10)
        gl.setRowStretchFactor(2, 7)

        for plot in (self.price_plot, self.vol_plot, self.cum_plot):
            plot.showAxis("bottom", False)
            plot.hideButtons()
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getViewBox().setMouseEnabled(x=True, y=False)
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                axis.setPen(_pen(theme.C["axis"], 1))
                axis.setTextPen(_pen(theme.C_TEXT, 1))
        self.cum_plot.showAxis("bottom", True)

        # 基类注册: 三栏 X 联动, price 为主面板 (滚轮/键盘/历史作用对象)
        for i, plot in enumerate(
                (self.price_plot, self.vol_plot, self.cum_plot)):
            self.register_plot(plot, sync=True, primary=(i == 0))

    # ── 主题 ──
    def apply_theme(self):
        """主题切换时刷新控件/视口/坐标轴配色, 无需重渲染数据。"""
        self.setBackground(pg.mkColor(theme.C_PANEL))
        for plot in (self.price_plot, self.vol_plot, self.cum_plot):
            if plot is None:
                continue
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                if axis is not None:
                    axis.setPen(_pen(theme.C["axis"], 1))
                    axis.setTextPen(_pen(theme.C_TEXT, 1))

    # ── 数据入口 ──
    def set_data(self, df=None, title="", pivots=None, events=None, waves=None,
                 draw_waves=True, locks=None, tr=None, profile=None, phase=None,
                 segs=None, sector=None, vsa_signals=None, wave_cum=None,
                 wave_segs=None, up_mask=None, caption=None, symbol=None,
                 scale=240, **extra):
        # 批量更新: 三栏图表构建期间禁用重绘 (减少中间状态闪烁)
        self.setUpdatesEnabled(False)
        try:
            self.clear_plots()
            self._days = []
            self._n = 0
            if df is None or len(df) == 0:
                self._build_plots()
                self.price_plot.setTitle(title or "暂无 K 线数据")
                return
            n = len(df)
            self._n = n
            self._has_data = True
            self._days = df["day"].tolist()
            self._fb_verdicts = _feedback_verdicts(df, segs or [], symbol, scale)
            try:
                self._is_minute = df["day"].dt.hour.nunique() > 1
            except Exception:
                self._is_minute = False
            self._chart_lo = df["low"].values.astype(float)
            self._chart_hi = df["high"].values.astype(float)
            self._full_x = (0.0, float(n - 1))
            ylo, yhi = float(df["low"].min()), float(df["high"].max())
            pad = (yhi - ylo) * 0.06 if yhi > ylo else 1.0
            self._full_y = (ylo - pad, yhi + pad)
            vmax = float(np.nanmax(df["volume"].values)) / 1e4
            self._full_vol = (0.0, max(vmax * 1.15, 1.0))

            self._build_plots()
            for plot in (self.price_plot, self.vol_plot, self.cum_plot):
                self.set_full_x(plot, self._full_x)
            self._date_axis.set_days(self._days, self._is_minute)
            self._build_price(df, title, pivots or [], events or [],
                              [list(w) for w in (waves or [])],
                              bool(draw_waves), locks or [], tr, profile,
                              segs or [], sector, vsa_signals or [])
            self._build_volume(df, wave_segs or [])
            self._build_cum(df, wave_cum, wave_segs, caption)

            n_view = min(self._n, _DEFAULT_BARS)
            self.apply_view(self._full_x[1] - (n_view - 1), self._full_x[1],
                            push=False)
            self._rescale_price_y(self._full_x[1] - (n_view - 1),
                                  self._full_x[1])
            self._attach_crosshairs()
        finally:
            self.setUpdatesEnabled(True)

    # ── 十字光标 ──
    def _fmt_x(self):
        days = self._days
        minute = self._is_minute

        def fmt(i):
            idx = int(round(i))
            if 0 <= idx < len(days):
                d = days[idx]
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
        self._crosshairs.append(
            Crosshair(self.price_plot, fmt_x, lambda v: f"{v:.2f}"))
        self._crosshairs.append(
            Crosshair(self.vol_plot, fmt_x, lambda v: f"{v:.1f}万手"))
        self._crosshairs.append(
            Crosshair(self.cum_plot, fmt_x, lambda v: f"{v:.0f}"))

    # ── 主图: 蜡烛 + 均线 + 波段 + 事件 + 锁 + VSA ──
    def _build_price(self, df, title, pivots, events, waves, draw_waves,
                     locks, tr, profile, segs, sector, vsa_signals):
        x = np.arange(self._n)
        plot = self.price_plot
        plot.setTitle(title or "", color=theme.C_TEXT, size=f"{self._fs(3)}pt")

        if segs:
            bands = [(s0, s1, _PHASE_STYLE[key][1], _PHASE_STYLE[key][2], label)
                     for s0, s1, key, label in segs if key in _PHASE_STYLE]
            if bands:
                bands_item = PhaseBands(bands, self._full_y)
                bands_item.setZValue(-100)
                plot.addItem(bands_item)
                # 阶段徽标 (短名+全名, 彩色底片) 按序错开, 避免相邻带标签重叠
                top = self._full_y[1]
                band_h = (top - self._full_y[0]) * 0.040
                for i, (s0, s1, key, label) in enumerate(
                        [s for s in segs if s[2] in _PHASE_STYLE]):
                    color, short = _PHASE_STYLE[key][1], _PHASE_STYLE[key][3]
                    xpos = float(min(s0 + 4, max(s0, s1 - 1)))
                    grow = (i % 3)
                    ypos = top - grow * band_h - band_h * 0.45
                    self._text(plot, xpos, ypos,
                               f" {short}·{label}  {i + 1} ",
                               "#ffffff", anchor=(0, 1), delta=1, bold=True,
                               fill=_brush_alpha(color, 0.95))
                    verdict = self._fb_verdicts.get((key, s0, s1)) if hasattr(
                        self, "_fb_verdicts") else None
                    if verdict:
                        v, src = verdict
                        ok = v == "correct"
                        tag = "✓ 正确" if ok else "✗ 错误"
                        if src == "auto":
                            tag += "(自动)"
                        xv = float(min(s1 - 2, max(s1, s0 + 1)))
                        self._text(plot, xv, ypos, f" {tag} ",
                                   "#ffffff", anchor=(1, 1), delta=1, bold=True,
                                   fill=_brush_alpha(
                                       theme.C_UP if ok else theme.C_DOWN, 0.95))

        plot.addItem(CandlestickItem(
            x, df["open"].values, df["close"].values,
            df["low"].values, df["high"].values))

        legend = plot.addLegend(offset=(12, 8))
        for col, color, label in _MA_LINES:
            if col in df.columns:
                curve = plot.plot(x, df[col].values,
                                  pen=_pen(color, 1), connect="finite")
                legend.addItem(curve, label)
        if "boll_up" in df.columns and "boll_dn" in df.columns:
            bu = plot.plot(x, df["boll_up"].values,
                           pen=_pen(theme.C_UP, 0.8, Qt.PenStyle.DashLine),
                           connect="finite")
            bd = plot.plot(x, df["boll_dn"].values,
                           pen=_pen(theme.C_DOWN, 0.8, Qt.PenStyle.DashLine),
                           connect="finite")
            legend.addItem(bu, "BOLL上轨")
            legend.addItem(bd, "BOLL下轨")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

        for p in pivots[-4:]:
            if p["type"] == "low":
                plot.addItem(pg.InfiniteLine(
                    pos=p["price"], angle=0,
                    pen=_pen(theme.C_DOWN, 0.7, Qt.PenStyle.DashLine)))
            else:
                plot.addItem(pg.InfiniteLine(
                    pos=p["price"], angle=0,
                    pen=_pen(theme.C_UP, 0.7, Qt.PenStyle.DashLine)))

        x_end = self._n - 1
        if tr:
            plot.addItem(pg.InfiniteLine(
                pos=tr["top"], angle=0,
                pen=_pen(theme.C_UP, 1.0, Qt.PenStyle.DashDotLine)))
            plot.addItem(pg.InfiniteLine(
                pos=tr["bottom"], angle=0,
                pen=_pen(theme.C_DOWN, 1.0, Qt.PenStyle.DashDotLine)))
            self._text(plot, x_end, tr["top"], f" TR上轨 {tr['top']:.2f}",
                       theme.C_UP, anchor=(1, 0.5), delta=-2)
            self._text(plot, x_end, tr["bottom"], f" TR下轨 {tr['bottom']:.2f}",
                       theme.C_DOWN, anchor=(1, 0.5), delta=-2)
        if profile:
            poc = profile["poc"]
            plot.addItem(pg.InfiniteLine(
                pos=poc, angle=0,
                pen=_pen("#d97706", 1.2, Qt.PenStyle.DotLine)))
            self._text(plot, x_end, poc, f" POC {poc:.2f}",
                       "#d97706", anchor=(1, 0.5), delta=-2)

        if draw_waves and len(waves) >= 2:
            self._draw_waves(plot, waves)
        self._draw_events(plot, events)
        if vsa_signals:
            self._draw_vsa(plot, df, vsa_signals)
        if locks or any(e["type"] in ("UTAD", "BC", "UT", "SOW", "LPSY") for e, _s, _d in events):
            self._draw_locks(plot, events, locks)

        if sector and sector.get("name") and sector.get("main20") is not None:
            s20 = sector["main20"] / 1e8
            sc = theme.C_UP if s20 >= 0 else theme.C_DOWN
            self._text(plot, x_end, self._full_y[1],
                       f"板块 {sector['name']} · 近20日主力 {s20:+.2f}亿",
                       sc, anchor=(1, 1), delta=-1, bold=True,
                       fill=_brush_alpha(theme.C_PANEL, 0.9))

    def _draw_waves(self, plot, waves):
        ymin, ymax = self._full_y
        off = (ymax - ymin) * 0.04
        for i in range(len(waves) - 1):
            w0, w1 = waves[i], waves[i + 1]
            x0, y0 = w0[0], w0[1]
            x1, y1 = w1[0], w1[1]
            up = y1 >= y0
            col = theme.C_UP if up else theme.C_DOWN
            tag = "上升浪" if up else "下跌浪"
            plot.addItem(pg.PlotCurveItem([x0, x1], [y0, y1],
                                          pen=_pen(col, 1.4)))
            ang = math.degrees(math.atan2(-(y1 - y0), x1 - x0)) if x1 != x0 else 0
            plot.addItem(pg.ArrowItem(pos=(x1, y1), angle=ang, tipAngle=25,
                                      tailLen=10, headLen=8, tailWidth=1.4,
                                      headWidth=1.4, pen=_pen(col),
                                      brush=pg.mkBrush(col)))
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            yt = my + (off if up else -off)
            self._text(plot, mx, yt, f"{tag} {y0:.2f}→{y1:.2f}", col,
                       bold=True, delta=-2, fill=_brush_alpha(theme.C_PANEL, 0.7))
        for i, wpt in enumerate(waves):
            wx, wy = wpt[0], wpt[1]
            wlabel = wpt[2] if len(wpt) >= 3 else str(i + 1)
            plot.addItem(pg.PlotDataItem(
                [wx], [wy], symbol="o", symbolSize=5,
                symbolPen=_pen("#9c36b5"), symbolBrush=pg.mkBrush("#9c36b5")))
            self._text(plot, wx + 1.5, wy, str(wlabel), "#9c36b5",
                       anchor=(0, 0.5), delta=-1, bold=True)

    def _draw_events(self, plot, events):
        for e, sign, dy in events:
            col = e["color"]
            label = str(e["type"])
            ix, price = e["idx"], e["price"]
            ty = price + sign * dy
            plot.addItem(pg.PlotCurveItem([ix, ix], [price, ty],
                                          pen=_pen(col, 0.8)))
            plot.addItem(pg.PlotDataItem(
                [ix], [price], symbol="o", symbolSize=4,
                symbolPen=_pen(col), symbolBrush=pg.mkBrush(col)))
            ti = pg.TextItem(label, color=col, anchor=(0.5, 0.5))
            ti.setFont(self._font(-2, bold=True))
            ti.setPos(ix, ty)
            ti.ev_label = label
            ti.ev_conf = e.get("conf")
            plot.addItem(ti)

    def _draw_vsa(self, plot, df, vsa_signals):
        ymin, ymax = self._full_y
        for s in vsa_signals:
            if s["label"] not in _VSA_DRAW:
                continue
            ix = s["idx"]
            if ix >= self._n:
                continue
            col = s["color"]
            lab = s["label"]
            ly = float(df["low"].iloc[ix])
            y_off = (ymax - ymin) * 0.025
            ty = ly - y_off
            plot.addItem(pg.PlotCurveItem([ix, ix], [ly, ty],
                                          pen=_pen(col, 0.4)))
            ti = pg.TextItem(lab, color=col, anchor=(0.5, 0))
            ti.setFont(self._font(-4, bold=True))
            ti.setPos(ix, ty)
            ti.ev_label = lab
            ti.ev_conf = None
            plot.addItem(ti)

    def _draw_locks(self, plot, events, locks):
        sell_types = {"UTAD", "BC", "UT", "SOW", "LPSY"}
        defs = [(lx, ly, str(lno), True) for lx, ly, lno in locks]
        defs += [(e["idx"], e["price"], "", False)
                 for e, _s, _d in events
                 if e["type"] in sell_types and e["idx"] >= self._n - W_RECENT]
        defs.sort(key=lambda d: d[0])
        if not defs:
            return
        ymin, ymax = self._full_y
        mid = (ymin + ymax) / 2
        base_dy = (ymax - ymin) * 0.06
        for lx, ly, label, is_buy in defs:
            sign = 1 if ly >= mid else -1
            ly2 = ly - sign * base_dy * 0.7
            color = theme.C_UP if is_buy else theme.C_DOWN
            dark = theme.C["up_dark"] if is_buy else theme.C["down_dark"]
            if label:
                ti = pg.TextItem(label, color="#ffffff", anchor=(0, 0.5),
                                 border=_pen(dark, 1), fill=pg.mkBrush(color))
                ti.setFont(self._font(-2, bold=True))
                ti.setPos(lx + 0.5, ly2)
                plot.addItem(ti)
            else:
                plot.addItem(pg.PlotDataItem(
                    [lx], [ly2], symbol="d", symbolSize=9,
                    symbolPen=_pen(dark), symbolBrush=pg.mkBrush(color)))
            if label == "3":
                self._text(plot, lx + 3.0, ly2, "✓ 买点", theme.C_UP,
                           anchor=(0, 0.5), delta=-1, bold=True)

    # ── 成交量 ──
    def _build_volume(self, df, wave_segs):
        x = np.arange(self._n)
        plot = self.vol_plot
        vol = df["volume"].values.astype(float) / 1e4
        up = df["close"].values >= df["open"].values
        finite = np.isfinite(vol)
        xs = x[finite]
        heights = vol[finite]
        brushes = [pg.mkBrush(theme.C_UP if u else theme.C_DOWN) for u in up[finite]]
        plot.addItem(pg.BarGraphItem(x=xs, height=heights, width=0.6,
                                     brushes=brushes, pen=None))
        legend = plot.addLegend(offset=(12, 4))
        for col, color, label in _VOL_MA_LINES:
            if col in df.columns:
                curve = plot.plot(x, df[col].values / 1e4,
                                  pen=_pen(color, 0.8), connect="finite")
                legend.addItem(curve, label)
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

        if "vol_ratio_20" in df.columns and self._n:
            _vr = float(df["vol_ratio_20"].iloc[-1])
            _vc = theme.C_UP if _vr >= 1.2 else theme.C_DOWN if _vr <= 0.8 else "#adb5bd"
            ti = pg.TextItem(f"量比 {_vr:.2f}", color=_vc, anchor=(1, 1),
                             fill=_brush_alpha(theme.C_PANEL, 0.85))
            ti.setFont(self._font(-1, bold=True))
            ti.setPos(self._n - 1, self._full_vol[1])
            plot.addItem(ti)

        for a, _b, _d in wave_segs[1:]:
            for p in (self.vol_plot, self.cum_plot):
                p.addItem(pg.InfiniteLine(
                    pos=a, angle=90,
                    pen=_pen("#adb5bd", 0.7, Qt.PenStyle.DotLine)))

        plot.setYRange(*self._full_vol, padding=0)
        plot.setXRange(*self._full_x, padding=0)

    # ── 波段累计量 ──
    def _build_cum(self, df, wave_cum, wave_segs, caption):
        x = np.arange(self._n)
        plot = self.cum_plot
        volume = df["volume"].values.astype(float)
        cum = np.asarray(wave_cum, dtype=float) if wave_cum is not None \
            else np.zeros(self._n)
        if wave_segs:
            cmax = float(np.nanmax(np.abs(cum)))
            scale = cmax if cmax > 0 else 1.0
            cum_norm = cum / scale * 100
            for a, b, direction in wave_segs:
                xs = x[a:b + 1]
                ys = cum_norm[a:b + 1]
                wcol = theme.C_UP if direction > 0 else theme.C_DOWN if direction < 0 else "#8a94a6"
                curve = plot.plot(xs, ys, pen=_pen(wcol, 1.0))
                zero = pg.PlotDataItem(xs, np.zeros_like(xs))
                plot.addItem(pg.FillBetweenItem(curve, zero,
                                                brush=_brush_alpha(wcol, 0.22)))
                tot = float(volume[a:b + 1].sum()) / 1e4
                arrow = "↑" if direction > 0 else "↓" if direction < 0 else ""
                peak = float(np.nanmax(np.abs(ys))) if len(ys) else 0.0
                y_txt = peak + 6 if direction >= 0 else -(peak + 6)
                self._text(plot, (a + b) / 2, y_txt,
                           f"{arrow}{tot:.0f}万手", wcol,
                           delta=-3, bold=True)
            plot.setYRange(-110, 110, padding=0)
        else:
            cmax = float(np.nanmax(np.abs(cum)))
            scale = cmax if cmax > 0 else 1.0
            cum_norm = cum / scale * 100
            curve = plot.plot(x, cum_norm, pen=_pen("#495057", 0.9))
            zero = pg.PlotDataItem(x, np.zeros_like(x))
            plot.addItem(pg.FillBetweenItem(curve, zero,
                                            brush=_brush_alpha("#8a94a6", 0.25)))
            plot.setYRange(0, 100, padding=0)
        plot.setXRange(*self._full_x, padding=0)

        if caption:
            text, color = caption
            if text:
                plot.setLabel("bottom", text=text, color=color,
                              **{"font-size": f"{self._fs(1)}pt"})

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
              delta=0, bold=False, fill=None):
        kwargs = {}
        if fill is not None:
            kwargs["fill"] = fill
        ti = pg.TextItem(text, color=color, anchor=anchor, **kwargs)
        ti.setFont(self._font(delta, bold))
        ti.setPos(float(x), float(y))
        plot.addItem(ti)
        return ti

    def hit_label(self, scene_pos):
        for item in self.scene().items(scene_pos):
            if isinstance(item, pg.TextItem):
                label = getattr(item, "ev_label", None)
                if label:
                    return label, getattr(item, "ev_conf", None)
        return None

    def zoom_x_about(self, cx, factor):
        """兼容旧接口: 以数据坐标 cx 为锚点缩放 X (基类实现)。"""
        self.zoom_about(cx, factor)

    def on_x_range_changed(self, x0, x1, source_vb=None):
        """基类联动钩子: 价格面板 Y 随可见区间自适应。"""
        if self._n:
            self._rescale_price_y(x0, x1)

    def _rescale_price_y(self, x0, x1):
        vb = self.price_plot.getViewBox()
        full_span = self._full_x[1] - self._full_x[0]
        if x1 - x0 >= full_span - 0.5:
            vb.setYRange(self._full_y[0], self._full_y[1], padding=0)
            return
        i0 = max(0, int(math.floor(x0)))
        i1 = min(self._n - 1, int(math.ceil(x1)))
        if i0 > i1 or i1 < 0 or i0 >= self._n:
            vb.setYRange(self._full_y[0], self._full_y[1], padding=0)
            return
        lo = float(np.min(self._chart_lo[i0:i1 + 1]))
        hi = float(np.max(self._chart_hi[i0:i1 + 1]))
        if hi - lo < 1e-9:
            return
        pad = (hi - lo) * 0.04
        y0 = max(self._full_y[0], lo - pad)
        y1 = min(self._full_y[1], hi + pad)
        if y1 - y0 > 1e-9:
            vb.setYRange(y0, y1, padding=0)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
