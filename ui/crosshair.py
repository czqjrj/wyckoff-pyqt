"""通用十字光标 (Crosshair) — 挂载到任意 pyqtgraph PlotItem。

跟随鼠标显示两条虚线 (横/纵), 并在顶部显示 X 读数 (日期/序号/成交量等)、
右侧显示 Y 读数 (价格/资金/指标值等)。格式化由调用方通过闭包提供,
使各图表可以自由定义坐标含义 (如日期索引 → 日期字符串)。

snap=True 时 X 吸附到最近的整数刻度 (K线图柱索引), 竖线始终对齐柱中心;
另提供 set_x / move_by 供键盘方向键逐根步进 (见 base_plot.step_crosshair)。
on_move(idx) 回调在光标位置变化后触发 (供 OHLC 信息条刷新)。
"""
import pyqtgraph as pg
from pyqtgraph.Qt.QtCore import QObject, QPointF, Qt, pyqtSignal
from pyqtgraph.Qt.QtGui import QFont

from . import theme


def _pen(color, width=1.0, style=Qt.PenStyle.DashLine, alpha=0.8):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    pen = pg.mkPen(c, width=width)
    pen.setStyle(style)
    return pen


def _chip_colors():
    """十字光标读数框配色, 随主题取色。"""
    return theme.C_TEXT, theme.C_PANEL, theme.C_BORDER


def _crosshair_line_color():
    """十字光标线颜色, 随主题取色。"""
    return theme.C_MUTED


class Crosshair(QObject):
    """跟随鼠标的十字光标。每条 (plot, fmt_x, fmt_y) 实例化一个。"""

    sigPositionChanged = pyqtSignal(object)

    def __init__(self, plot, fmt_x, fmt_y, font_size=8, snap=False,
                 on_move=None):
        super().__init__()
        self._alive = True
        self.plot = plot
        self._fmt_x = fmt_x
        self._fmt_y = fmt_y
        self._snap = bool(snap)
        self._on_move = on_move
        self._x = None
        self._y = None
        self._scene = plot.scene()

        line_color = _crosshair_line_color()
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=_pen(line_color))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=_pen(line_color))
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

    def apply_theme(self):
        """主题切换时刷新十字光标线条颜色。"""
        line_color = _crosshair_line_color()
        self.vline.setPen(_pen(line_color))
        self.hline.setPen(_pen(line_color))
        fg, bg, border = _chip_colors()
        self.txt_x.setColor(fg)
        self.txt_y.setColor(fg)
        self.txt_x.setBorder(_pen(border, 1, Qt.PenStyle.SolidLine, alpha=1.0))
        self.txt_y.setBorder(_pen(border, 1, Qt.PenStyle.SolidLine, alpha=1.0))
        self.txt_x.setFill(pg.mkBrush(bg))
        self.txt_y.setFill(pg.mkBrush(bg))

    def set_visible(self, flag):
        for it in (self.vline, self.hline, self.txt_x, self.txt_y):
            it.setVisible(bool(flag))

    # ── 内部工具 ──
    def _snapped(self, x):
        """snap 模式下取最近整数柱位, 否则原值。"""
        return float(round(x)) if self._snap else float(x)

    def _view_rect(self):
        vb = self.plot.getViewBox()
        return vb.viewRect() if vb is not None else None

    def _emit_moved(self, idx_x):
        if self._on_move is not None:
            try:
                self._on_move(idx_x)
            except Exception:
                pass

    def _place_chips(self, sx, y):
        vr = self._view_rect()
        if vr is None:
            return
        pad = 4.0
        x_txt = max(vr.left() + pad, min(vr.right() - pad, sx))
        y_txt = max(vr.top() + pad, min(vr.bottom() - pad, y))
        self.txt_x.setPos(x_txt, vr.top() + 2)
        self.txt_y.setPos(vr.right() - 2, y_txt)

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
        vb.viewRect()
        x, y = float(mouse.x()), float(mouse.y())
        self._x, self._y = x, y
        sx = self._snapped(x)
        self.vline.setPos(sx)
        self.hline.setPos(y)
        try:
            tx = self._fmt_x(sx)
        except Exception:
            tx = ""
        try:
            ty = self._fmt_y(y)
        except Exception:
            ty = ""
        self.txt_x.setText(tx)
        self.txt_y.setText(ty)
        self._place_chips(sx, y)
        self.set_visible(True)
        self._emit_moved(sx)
        self.sigPositionChanged.emit(self)

    # ── 键盘步进 (base_plot.step_crosshair 调用) ──
    def set_x(self, x):
        """绝对设置竖线位置 (只动竖线与 X 读数, 不碰横线)。"""
        if not self._alive:
            return
        self._x = float(x)
        sx = self._snapped(self._x)
        self.vline.setPos(sx)
        try:
            tx = self._fmt_x(sx)
        except Exception:
            tx = ""
        self.txt_x.setText(tx)
        vr = self._view_rect()
        y = self._y if self._y is not None else \
            ((vr.top() + vr.bottom()) / 2 if vr is not None else 0.0)
        self._place_chips(sx, y)
        self.vline.setVisible(True)
        self.txt_x.setVisible(True)
        self._emit_moved(sx)
        self.sigPositionChanged.emit(self)

    def move_by(self, delta):
        """相对步进 delta 个数据单位; 返回新的数据坐标。

        首次调用且从未有鼠标位置时, 以视口中心为起点。"""
        vr = self._view_rect()
        if self._x is None:
            base = (vr.left() + vr.right()) / 2 if vr is not None else 0.0
            base = self._snapped(base)
        else:
            base = self._snapped(self._x)
        nx = base + float(delta)
        self.set_x(nx)
        return nx
