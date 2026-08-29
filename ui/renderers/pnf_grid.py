"""P&F 格子/圈叉渲染器 — 负责绘制方格坐标纸与 X/O 记号。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.Qt.QtCore import Qt

from .. import theme


def _pen(color, width=1.0, style=None, alpha=1.0):
    c = pg.mkColor(color)
    if alpha is not None:
        c.setAlphaF(float(alpha))
    kw = {"color": c, "width": width}
    if style is not None:
        kw["style"] = style
    return pg.mkPen(**kw)


def _brush_alpha(color, alpha=1.0):
    c = pg.mkColor(color)
    if alpha is not None:
        c.setAlphaF(float(alpha))
    return pg.mkBrush(c)


def _fmt_cn_vol(v):
    """成交量缩写 (万/亿), 供列信息卡与 HUD。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


class PnfGridRenderer:
    """点数图圈叉图: 浅色方格坐标纸 + X列(红)× / O列(绿)○, 记号填满格子。

    X 与 O 列的数据坐标 (列号×格高) 各向异性, 直接按数据坐标画记号会把
    ×/○ 拉成歪斜的"毛线"状。故记号尺寸先换算到像素空间 (保证屏幕上为正圆 /
    正叉, 随缩放保持格型), 再映射回数据坐标绘制; 方格线则在可见视口内按列/格绘制。
    """

    def __init__(self, cols, box):
        self._cols = cols
        self._box = float(box)

    def create_item(self):
        """创建可加入 PlotItem 的 GraphicsObject。"""
        return _PnfGridItem(self._cols, self._box)


class _PnfGridItem(pg.GraphicsObject):
    """内部 GraphicsObject 实现。"""

    def __init__(self, cols, box):
        super().__init__()
        self._cols = cols
        self._box = float(box)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def paint(self, p, *args):
        box = self._box
        p.setRenderHint(p.RenderHint.Antialiasing)
        n = len(self._cols)
        vb = self.getViewBox()
        if vb is None:
            return
        xr = vb.viewRange()[0]
        yr = vb.viewRange()[1]
        vx0, vx1 = float(xr[0]), float(xr[1])
        vy0, vy1 = float(yr[0]), float(yr[1])
        t = p.transform()
        sx = abs(t.m11())
        sy = abs(t.m22())
        if sx <= 0 or sy <= 0:
            return
        j0 = max(0, int(vx0 + 0.5))
        j1 = min(n - 1, int(vx1 - 0.5))

        # ── 方格坐标纸: 每列一条竖线、每格一条横线 (可见视口内) ──
        gc = pg.mkColor(theme.C_GRID)
        gc.setAlphaF(0.8)
        pen_g = QtGui.QPen(gc, 0)
        pen_g.setWidthF(0.6)
        pen_g.setCosmetic(True)
        p.setPen(pen_g)
        r0 = int(vy0 / box) - 1
        r1 = int(vy1 / box) + 1
        y = (r0 - 0.5) * box
        while y <= (r1 + 0.5) * box:
            p.drawLine(QtCore.QPointF(vx0, y), QtCore.QPointF(vx1, y))
            y += box
        for j in range(j0, j1 + 1):
            x = j - 0.5
            p.drawLine(QtCore.QPointF(x, vy0), QtCore.QPointF(x, vy1))

        # ── 记号: 每个小格子一个 × / ○, 填满整个格子 ──
        cw = sx * 1.0
        ch = sy * box
        s = min(cw, ch)
        if s < 1.0:
            return
        rx = (s / 2) / sx
        ry = (s / 2) / sy
        w_px = max(1.0, s * 0.15)
        for j, c in enumerate(self._cols):
            if j < j0 or j > j1:
                continue
            pen = QtGui.QPen(
                pg.mkColor(theme.C_UP if c["type"] == "X" else theme.C_DOWN), 0)
            pen.setWidthF(w_px)
            pen.setCosmetic(True)
            p.setPen(pen)
            if c["type"] == "X":
                for row in c["rows"]:
                    if row < r0 or row > r1:
                        continue
                    y = row * box
                    p.drawLine(QtCore.QPointF(j - rx, y - ry),
                               QtCore.QPointF(j + rx, y + ry))
                    p.drawLine(QtCore.QPointF(j - rx, y + ry),
                               QtCore.QPointF(j + rx, y - ry))
            else:
                for row in c["rows"]:
                    if row < r0 or row > r1:
                        continue
                    y = row * box
                    p.drawEllipse(QtCore.QRectF(j - rx, y - ry,
                                                2 * rx, 2 * ry))
        p.setPen(QtGui.QPen(Qt.PenStyle.NoPen))

    def boundingRect(self):
        rows = [r for c in self._cols for r in c["rows"]]
        if not rows:
            return QtCore.QRectF(0, 0, 1, 1)
        pad = 50 * self._box
        x0 = -0.5
        x1 = len(self._cols) - 0.5
        y0 = min(rows) * self._box - pad
        y1 = max(rows) * self._box + pad
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class _DragTextItem(pg.TextItem):
    """可鼠标拖拽的文字标注。

    在文字上按下左键拖动可移动标注位置 (不触发下方 ViewBox 平移/双击复位);
    拖到图表上任意位置以避开图格。`_vp` 记录父坐标系下的归一化位置,
    供视口固定标注在窗口缩放/尺寸变化后保持拖拽后的位置。
    """

    def __init__(self, text="", color=(200, 200, 200), html=None,
                 anchor=(0, 0), border=None, fill=None, angle=0, **kwargs):
        # 接收并忽略 bold, delta 等额外参数 (兼容旧调用方式, 字体由调用方后续 setFont 设置)
        # pg.TextItem 需要按位置参数顺序传递: text, color, html, anchor, border, fill, angle
        super().__init__(text, color, html, anchor, border, fill, angle)
        self._vp = (0.5, 0.5)
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def hoverEvent(self, ev):
        if not ev.isExit():
            ev.acceptDrags(Qt.MouseButton.LeftButton)

    def mouseDragEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        if ev.isStart():
            self._drag_offset = self.pos() - self.mapToParent(ev.buttonDownPos())
        ev.accept()
        if self._drag_offset is not None:
            self.prepareGeometryChange()
            self.setPos(self._drag_offset + self.mapToParent(ev.pos()))
            self._update_vp()
        if ev.isFinish():
            self._drag_offset = None

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()

    def _update_vp(self):
        parent = self.parentItem()
        if parent is None or not hasattr(parent, "rect"):
            return
        r = parent.rect()
        if r.width() > 0 and r.height() > 0:
            self._vp = (self.pos().x() / r.width(),
                        self.pos().y() / r.height())

    def set_vp(self, vx, vy):
        """按父坐标系归一化位置摆放 (视口固定标注用)。"""
        self._vp = (float(vx), float(vy))
        parent = self.parentItem()
        if parent is not None:
            r = parent.rect()
            self.setPos(r.width() * self._vp[0], r.height() * self._vp[1])


class _LatestBtnItem(_DragTextItem):
    """右下角 "回到最新列" 悬浮按钮: 点击跳回最近 N 列默认视野。

    平移/缩放离开最新行情区后显示 (见 _update_latest_btn), 点击后隐藏。
    """

    def __init__(self, host):
        super().__init__("▶ 回到最新列", color=theme.C_TEXT, anchor=(1, 1),
                         border=_pen(theme.C_ACCENT, 1.0),
                         fill=pg.mkBrush(theme.C_PANEL))
        self._host = host
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hide()

    def hoverEvent(self, ev):
        pass  # 不接受拖拽授权, 保持纯按钮语义

    def mouseDragEvent(self, ev):
        ev.ignore()

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._host.jump_to_latest()
            ev.accept()
