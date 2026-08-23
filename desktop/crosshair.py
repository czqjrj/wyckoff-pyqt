"""通用十字光标 (Crosshair) — 挂载到任意 pyqtgraph PlotItem。

跟随鼠标显示两条虚线 (横/纵), 并在顶部显示 X 读数 (日期/序号/成交量等)、
右侧显示 Y 读数 (价格/资金/指标值等)。格式化由调用方通过闭包提供,
使各图表可以自由定义坐标含义 (如日期索引 → 日期字符串)。
"""
import pyqtgraph as pg
from pyqtgraph.Qt.QtCore import QPointF, Qt
from pyqtgraph.Qt.QtGui import QFont

from . import theme

_LINE_COLOR = "#6b7280"


def _pen(color, width=1.0, style=Qt.PenStyle.DashLine, alpha=0.8):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    pen = pg.mkPen(c, width=width)
    pen.setStyle(style)
    return pen


def _chip_colors():
    """十字光标读数框配色, 随主题取色。"""
    return theme.C_TEXT, theme.C_PANEL, theme.C_BORDER


class Crosshair:
    """跟随鼠标的十字光标。每条 (plot, fmt_x, fmt_y) 实例化一个。"""

    def __init__(self, plot, fmt_x, fmt_y, font_size=8):
        self._alive = True
        self.plot = plot
        self._fmt_x = fmt_x
        self._fmt_y = fmt_y
        self._scene = plot.scene()

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=_pen(_LINE_COLOR))
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=_pen(_LINE_COLOR))
        self.vline.setZValue(50)
        self.hline.setZValue(50)
        self.vline.setVisible(False)
        self.hline.setVisible(False)
        plot.addItem(self.vline, ignoreBounds=True)
        plot.addItem(self.hline, ignoreBounds=True)

        font = QFont()
        font.setPointSize(int(font_size))
        fg, bg, border = _chip_colors()
        self.txt_x = pg.TextItem("", color=fg, anchor=(0.5, 0),
                                 border=_pen(border, 1, Qt.PenStyle.SolidLine,
                                             alpha=1.0),
                                 fill=pg.mkBrush(bg))
        self.txt_y = pg.TextItem("", color=fg, anchor=(1, 0.5),
                                 border=_pen(border, 1, Qt.PenStyle.SolidLine,
                                             alpha=1.0),
                                 fill=pg.mkBrush(bg))
        for t in (self.txt_x, self.txt_y):
            t.setFont(font)
            t.setZValue(51)
            t.setVisible(False)
        plot.addItem(self.txt_x, ignoreBounds=True)
        plot.addItem(self.txt_y, ignoreBounds=True)

        if self._scene is not None:
            self._scene.sigMouseMoved.connect(self._moved)

    # ── 生命周期 ──
    def detach(self):
        """断开鼠标信号并移除全部 item (widget 重建 PlotItem 前调用)。"""
        self._alive = False
        if self._scene is not None:
            try:
                self._scene.sigMouseMoved.disconnect(self._moved)
            except (TypeError, RuntimeError):
                pass
        for it in (self.vline, self.hline, self.txt_x, self.txt_y):
            if it.scene() is not None:
                self.plot.removeItem(it)

    def set_visible(self, flag):
        for it in (self.vline, self.hline, self.txt_x, self.txt_y):
            it.setVisible(bool(flag))

    # ── 鼠标跟踪 ──
    def _moved(self, scene_pos):
        if not self._alive:
            return
        if not isinstance(scene_pos, QPointF):
            scene_pos = QPointF(float(scene_pos[0]), float(scene_pos[1]))
        vb = self.plot.getViewBox()
        if vb is None:
            return
        if not vb.sceneBoundingRect().contains(scene_pos):
            self.set_visible(False)
            return
        mouse = vb.mapSceneToView(scene_pos)
        vr = vb.viewRect()
        x, y = float(mouse.x()), float(mouse.y())
        self.vline.setPos(x)
        self.hline.setPos(y)

        try:
            tx = self._fmt_x(x)
        except Exception:
            tx = ""
        try:
            ty = self._fmt_y(y)
        except Exception:
            ty = ""
        self.txt_x.setText(tx)
        self.txt_y.setText(ty)

        pad = 4.0
        x_txt = max(vr.left() + pad, min(vr.right() - pad, x))
        y_txt = max(vr.top() + pad, min(vr.bottom() - pad, y))
        self.txt_x.setPos(x_txt, vr.top() + 2)
        self.txt_y.setPos(vr.right() - 2, y_txt)
        self.set_visible(True)
