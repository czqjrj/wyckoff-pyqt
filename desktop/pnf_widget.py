# -*- coding: utf-8 -*-
"""pyqtgraph P&F 点数图控件 (替换原 matplotlib 点数图)。

数据由 AnalysisThread 在 worker 线程通过 build_pnf_data() 收集 (点数图计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

交互 (与原 matplotlib 版行为一致):
  - 滚轮    以光标为锚点 X/Y 等比缩放 (保持格子近似正方形)
  - 左键拖拽 平移
  - 双击    复位到全幅
  - 键盘    + / - 缩放, 左/右箭头平移, Home/r 复位, Backspace/f 视图历史
"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import FONT_CANDIDATES
from wyckoff.pnf import pnf_box_label, pnf_cap, pnf_hist_title

from . import theme
from .crosshair import Crosshair

# A股配色: 红涨绿跌 (X=上涨列, O=下跌列), 运行时从 theme 动态取色

# 默认视野显示的列数 (点数图通常列数较多, 满幅每列仅 2~4px, 格子与标注不可辨,
# 与主流看盘软件一致默认聚焦最近 N 列; Home/双击可看全幅)。
_DEFAULT_COLS = 70

# 右缘留白 (列数): 放置 "上涨/下跌目标位" 标注
_RIGHT_MARGIN = 9.0

# 左侧价格轴固定宽度 (px): 主图与底部量柱面板共用, 保证两视图 X 像素映射一致,
# 使量柱与点数图列严格对齐。
_PRICE_AXIS_W = 64


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


class PnfGridItem(pg.GraphicsObject):
    """点数图圈叉图: 浅色方格坐标纸 + X列(红)× / O列(绿)○, 记号填满格子。

    X 与 O 列的数据坐标 (列号×格高) 各向异性, 直接按数据坐标画记号会把
    ×/○ 拉成歪斜的"毛线"状。故记号尺寸先换算到像素空间 (保证屏幕上为正圆 /
    正叉, 随缩放保持格型), 再映射回数据坐标绘制; 方格线则在可见视口内按列/格绘制。
    """

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
        # 注意: 画笔宽度为逻辑单位会被视图变换放大, 必须用 cosmetic (恒为设备像素)
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
        # 格子像素尺寸: 列宽 1.0 数据单位 × 格高 box; 以较短边为方格边长 s,
        # × 画在 s 方格的角到角, ○ 内切于 s 方格, 屏幕上是正叉/正圆并占满格子。
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


class PnfBandsItem(pg.GraphicsObject):
    """点数图区间底色带 (历史/当前 TR): 垂直色带铺满价格区间, 画在格子之下。"""

    def __init__(self, bands, y_range):
        super().__init__()
        self._bands = bands        # [(x0, x1, color, alpha)]
        self._yr = y_range
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def paint(self, p, *args):
        y = 1e6
        for x0, x1, color, alpha in self._bands:
            c = pg.mkColor(color)
            c.setAlphaF(float(alpha))
            p.fillRect(QtCore.QRectF(x0, -y, x1 - x0, 2 * y),
                       QtGui.QBrush(c))

    def boundingRect(self):
        if not self._bands:
            return QtCore.QRectF(0, 0, 1, 1)
        y0, y1 = self._yr
        x0 = min(b[0] for b in self._bands)
        x1 = max(b[1] for b in self._bands)
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class _PnfViewBox(pg.ViewBox):
    """点数图 ViewBox: 滚轮 X/Y 等比缩放, 双击复位。"""

    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def wheelEvent(self, ev):
        pos = self.mapSceneToView(ev.scenePos())
        self._host.zoom_about(pos.x(), pos.y(),
                              0.8 if ev.delta() > 0 else 1.25)
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._host.reset_view()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)


class _VapViewBox(pg.ViewBox):
    """箱体量 (Volume-at-Price) 面板 ViewBox: 滚轮/双击委托主图缩放复位。

    本面板 X=成交量、Y=价格 (与主图 Y 联动), 不响应平移, 避免与主图脱钩。
    """

    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host

    def wheelEvent(self, ev):
        vb = self._host._vb
        if vb is None:
            return
        # 以鼠标位置为锚点缩放 (而非中心点), 与主图行为一致
        pos = vb.mapSceneToView(ev.scenePos())
        self._host.zoom_about(pos.x(), pos.y(),
                              0.8 if ev.delta() > 0 else 1.25)
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._host.reset_view()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)


class _VapBarsItem(pg.GraphicsObject):
    """箱体量 Volume-at-Price: 每个价格行一根横向量条 (右侧面板)。

    X 轴 = 成交量 (绝对量), Y 轴与主图价格联动; 条长 ∝ 该价格箱体的累计量,
    颜色 (主题 accent 蓝) 透明度随占比加深。
    """

    def __init__(self, row_vols, box, row_max):
        super().__init__()
        self._row_vols = dict(row_vols)
        self._box = float(box)
        self._row_max = float(row_max)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def paint(self, p, *args):
        box = self._box
        row_max = self._row_max
        if row_max <= 0 or not self._row_vols:
            return
        p.setRenderHint(p.RenderHint.Antialiasing)
        base = pg.mkColor(theme.C_ACCENT)
        p.setPen(Qt.PenStyle.NoPen)
        for row, v in self._row_vols.items():
            if v <= 0:
                continue
            frac = v / row_max
            c = pg.mkColor(base)
            c.setAlphaF(0.15 + 0.85 * frac)
            p.setBrush(QtGui.QBrush(c))
            p.drawRect(QtCore.QRectF(0.0, row * box - box * 0.45,
                                     max(v, 1e-9), box * 0.9))
        p.setPen(QtGui.QPen(Qt.PenStyle.NoPen))

    def boundingRect(self):
        box = self._box
        if not self._row_vols:
            return QtCore.QRectF(0, 0, 1, 1)
        rows = list(self._row_vols)
        y0 = min(rows) * box - box
        y1 = max(rows) * box + box
        return QtCore.QRectF(0, y0, max(self._row_max, 1.0), y1 - y0)


class _DragTextItem(pg.TextItem):
    """可鼠标拖拽的文字标注。

    在文字上按下左键拖动可移动标注位置 (不触发下方 ViewBox 平移/双击复位);
    拖到图表上任意位置以避开图格。`_vp` 记录父坐标系下的归一化位置,
    供视口固定标注在窗口缩放/尺寸变化后保持拖拽后的位置。
    """

    def __init__(self, text="", color=(200, 200, 200), html=None,
                 anchor=(0, 0), border=None, fill=None, angle=0):
        super().__init__(text, color=color, html=html, anchor=anchor,
                         border=border, fill=fill, angle=angle)
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



class PnfWidget(pg.GraphicsLayoutWidget):
    """pyqtgraph P&F 点数图: X 列红方块 / O 列绿方块 + TR 区间/目标位标注。

    set_data(**pnf_data) 接收 build_pnf_data() 的返回, 其余交互内置。
    """

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._cols = []
        self._box = 1.0
        self._n = 0
        self._full_x = (0.0, 1.0)
        self._full_y = (0.0, 1.0)
        self._hist = []
        self._hist_pos = -1
        self._pinned = []
        self.plot = None
        self._vb = None
        self._vol_plot = None
        self._vap_plot = None
        self._volumes = None
        self._col_heights = []
        self._sync_lock = False
        self._crosshairs = []

        self.setBackground(pg.mkColor(theme.C_PANEL))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_plot()
        self.plot.setTitle("输入 A 股代码 (如 600104 / sh600104 / 000001), "
                           "点击\"开始分析\"加载点数图。")
        if self._vol_plot is not None:
            self._vol_plot.setVisible(False)
        if self._vap_plot is not None:
            self._vap_plot.setVisible(False)

    # ── 布局 ──
    def _build_plot(self):
        self.ci.clear()
        self.ci.setContentsMargins(8, 8, 8, 8)
        self.ci.setSpacing(4)

        vb = _PnfViewBox(self, enableMenu=True)
        vb.setBackgroundColor(pg.mkColor(theme.C_PANEL))
        vb.setMouseEnabled(x=True, y=True)
        self._vb = vb
        self.plot = pg.PlotItem(viewBox=vb)
        self.ci.addItem(self.plot, 0, 0)
        self.plot.hideButtons()
        # 不用坐标轴默认网格 — 圈叉图自带每列/每格方格坐标纸 (PnfGridItem)
        self.plot.showGrid(x=False, y=False)
        vb.sigResized.connect(self._reposition_pinned)
        for ax_name in ("left", "bottom"):
            axis = self.plot.getAxis(ax_name)
            axis.setPen(_pen(theme.C["axis"], 1))
            axis.setTextPen(_pen(theme.C_TEXT, 1))
        self.plot.setLabel("bottom", "列序号")
        self.plot.setLabel("left", "价格")
        # 列序号统一放底部量柱面板的最下方; 主图不显示重复刻度, 让两面板连成一体
        self.plot.hideAxis("bottom")
        # 价格轴固定宽度, 与量柱面板左轴等宽 → 两视图 X 像素映射一致 (列对齐)
        self.plot.getAxis("left").setWidth(_PRICE_AXIS_W)

        # 箱体量 Volume-at-Price 右侧面板: X=量, Y=价格 (与主图 Y 联动)
        vap_vb = _VapViewBox(self)
        vap_vb.setBackgroundColor(pg.mkColor(theme.C_PANEL))
        vap_vb.setMouseEnabled(x=False, y=False)
        vap_vb.enableAutoRange(pg.ViewBox.XAxis, False)
        vap_vb.enableAutoRange(pg.ViewBox.YAxis, False)
        self._vap_plot = pg.PlotItem(viewBox=vap_vb)
        self._vap_plot.hideButtons()
        self._vap_plot.hideAxis("left")
        self._vap_plot.hideAxis("bottom")
        self.ci.addItem(self._vap_plot, 0, 1)

        # 列级成交量底部面板: X=列序号 (与主图 X 联动), Y=量
        vol_vb = _PnfViewBox(self, enableMenu=True)
        vol_vb.setBackgroundColor(pg.mkColor(theme.C_PANEL))
        vol_vb.setMouseEnabled(x=True, y=False)
        self._vol_plot = pg.PlotItem(viewBox=vol_vb)
        self._vol_plot.hideButtons()
        # 左轴仅用于占位 (与主图价格轴等宽, 使两面板列严格对齐), 不显示刻度
        vol_axis = self._vol_plot.getAxis("left")
        vol_axis.setStyle(showValues=False, tickLength=0)
        vol_axis.setWidth(_PRICE_AXIS_W)
        for ax_name in ("bottom",):
            axis = self._vol_plot.getAxis(ax_name)
            axis.setPen(_pen(theme.C["axis"], 1))
            axis.setTextPen(_pen(theme.C_TEXT, 1))
        self._vol_plot.setLabel("bottom", "列序号")
        self._vol_plot.showGrid(x=False, y=False)
        # 只占主图所在列 (不跨列), 保证与主图同宽同左缘 → 像素级对齐
        self.ci.addItem(self._vol_plot, 1, 0)

        gl = self.ci.layout
        gl.setRowStretchFactor(0, 6)
        gl.setRowStretchFactor(1, 1)
        gl.setColumnStretchFactor(0, 48)
        gl.setColumnStretchFactor(1, 3)

        # 主图 X 与列量面板 X 双向同步 (滚轮/拖拽/键盘缩放均保持对齐)
        for pvb in (self._vb, vol_vb):
            pvb.sigXRangeChanged.connect(self._on_x_range_changed)
        # 主图 Y (aspect 锁驱动) → 箱体量面板 Y 单向同步
        self._vb.sigYRangeChanged.connect(self._on_y_range_changed)

    def _on_x_range_changed(self, vb, xrange):
        if self._n == 0 or self._sync_lock:
            return
        self._sync_lock = True
        try:
            x0, x1 = float(xrange[0]), float(xrange[1])
            for pvb in (self._vb, self._vol_plot.getViewBox()):
                if pvb is not vb:
                    pvb.setXRange(x0, x1, padding=0)
            self._fit_vol_y(x0, x1)
        finally:
            self._sync_lock = False

    def _fit_vol_y(self, x0, x1):
        """列级成交量面板 Y 归一化到当前视野内可见列的最大量 (避免全局最大值在视野
        外时量柱被压成 1-2px 细线; 视野变化时自动跟随)。"""
        if not self._col_heights or self._vol_plot is None:
            return
        vis = [h for c, h in enumerate(self._col_heights)
               if h > 0 and c + 0.45 >= x0 and c - 0.45 <= x1]
        if not vis:
            return
        top = max(max(vis) * 1.12, 0.02)
        self._vol_plot.getViewBox().setYRange(0.0, top, padding=0)

    def _on_y_range_changed(self, vb, yrange):
        if self._n == 0 or self._vap_plot is None:
            return
        y0, y1 = float(yrange[0]), float(yrange[1])
        vap_vb = self._vap_plot.getViewBox()
        if vap_vb is not vb:
            vap_vb.setYRange(y0, y1, padding=0)

    # ── 主题 ──
    def apply_theme(self):
        """主题切换时刷新控件/视口/坐标轴配色。"""
        self.setBackground(pg.mkColor(theme.C_PANEL))
        for plot in (self.plot, self._vol_plot, self._vap_plot):
            if plot is None:
                continue
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                if axis is not None:
                    axis.setPen(_pen(theme.C["axis"], 1))
                    axis.setTextPen(_pen(theme.C_TEXT, 1))

    # ── 数据入口 ──
    def set_data(self, cols=None, box=None, title="", targets=None,
                 history=None, volumes=None, box_mode="pct", atr_factor=0.5,
                 **extra):
        self._box_mode = box_mode or "pct"
        self._atr_factor = atr_factor
        self._detach_crosshairs()
        self.plot.clear()
        self._volumes = volumes or None
        self._col_heights = []
        if self._vol_plot is not None:
            self._vol_plot.clear()
        if self._vap_plot is not None:
            self._vap_plot.clear()
        for ti, _fy in self._pinned:
            if ti.scene() is not None:
                ti.scene().removeItem(ti)
        self._pinned = []
        self._hist = []
        self._hist_pos = -1
        if not cols:
            self._n = 0
            self._full_x = (0.0, 1.0)
            self._full_y = (0.0, 1.0)
            if self._vb is not None:
                self._vb.setAspectLocked(False)
            self.plot.setTitle(title or "暂无点数图数据")
            if self._vol_plot is not None:
                self._vol_plot.setVisible(False)
            if self._vap_plot is not None:
                self._vap_plot.setVisible(False)
            return
        self._cols = cols
        self._box = float(box)
        self._n = len(cols)
        self._cur_targets = targets or {}
        self._build(cols, self._box, title or "", targets or {},
                    history or [], self._volumes)
        # 经典圈叉图: 格子必须为正方形 — 锁定纵横比 (1 列宽 = box 格高)
        self._vb.setAspectLocked(True, ratio=self._box)
        self._apply_initial_view()
        self._attach_crosshairs()

    # ── 十字光标 ──
    def _detach_crosshairs(self):
        for ch in self._crosshairs:
            ch.detach()
        self._crosshairs = []

    def _attach_crosshairs(self):
        self._detach_crosshairs()
        if self._n <= 0:
            return
        self._crosshairs.append(
            Crosshair(self.plot,
                      lambda v: f"列 {v:.0f}",
                      lambda v: f"{v:.2f}",
                      font_size=self._fs(0)))

    # ── 绘制 ──
    def _build(self, cols, box, title, targets, history, volumes=None):
        plot = self.plot
        n = len(cols)

        # Y 范围: 格子 + TR/目标线
        rows_all = [r for c in cols for r in c["rows"]]
        ymin = min(rows_all) * box if rows_all else 0.0
        ymax = max(rows_all) * box if rows_all else box
        extra = []
        for src in (targets,):
            for k in ("tr_top", "tr_bottom", "横向计数上方目标",
                      "横向计数下方目标", "近端上方目标", "近端下方目标"):
                v = src.get(k)
                if isinstance(v, (int, float)):
                    extra.append(float(v))
        for h in history:
            for k in ("tr_top", "tr_bottom", "up_target", "down_target"):
                v = h.get(k)
                if isinstance(v, (int, float)):
                    extra.append(float(v))
        if extra:
            ymin = min(ymin, min(extra))
            ymax = max(ymax, max(extra))
        pad = (ymax - ymin) * 0.10 if ymax > ymin else max(box, 1.0)
        y0, y1 = ymin - pad, ymax + pad
        self._full_x = (-0.6, n + _RIGHT_MARGIN)
        self._full_y = (y0, y1)

        # 底色带 (历史段 + 当前 TR), 画在最底
        bands = []
        for h in history:
            c0 = float(h.get("tr_start_col", 0))
            c1 = float(h.get("tr_end_col", n))
            fill = theme.C_ZONE_ACC if h.get("zone") == "吸筹" else theme.C_ZONE_DIST
            bands.append((c0 - 0.4, c1 + 0.4, fill, 0.55))
        if targets:
            c0 = float(targets.get("tr_start_col", 0))
            c1 = float(targets.get("tr_end_col", n))
            bands.append((c0 - 0.4, c1 + 0.4, self._zone_colors(targets)[0],
                          0.6))
        if bands:
            plot.addItem(PnfBandsItem(bands, self._full_y))

        # 格子
        plot.addItem(PnfGridItem(cols, box))

        # 历史段标注
        for h in history:
            self._draw_history(plot, h, n)

        # 当前 TR / 目标
        if targets:
            self._draw_targets(plot, targets, n)

        # 标题
        t_title = f"[P&F] 格值={self._box:.2f} ({pnf_box_label(self._box_mode, self._atr_factor)})  反转=3格"
        if targets:
            t_title += "  |  威科夫计数目标已标注"
        plot.setTitle(f"{title}\n{t_title}", color=theme.C_TEXT,
                      size=f"{self._fs(2)}pt")

        # 底部解读行 (固定视口)
        cap, cap_color = pnf_cap(targets, cols)
        self._pin(cap, cap_color, 0.98, bold=True, size=self._fs(1))
        ht = pnf_hist_title(history)
        if ht:
            self._pin(ht.strip(), theme.C_MUTED, 0.92, bold=True, size=self._fs(1))

        # 顶部信息条 (固定视口, 不压格子): 当前区间/POC/三档目标概率/历史段摘要
        if targets:
            zone_label, zone_note = self._zone_meta(targets)
            tr_top = float(targets.get("tr_top", 0))
            tr_bottom = float(targets.get("tr_bottom", 0))
            poc = targets.get("poc")
            _cn = int(targets.get("columns", 0))
            _ca = float(targets.get("cause", 0))
            cause_ratio = _ca / (tr_top - tr_bottom) if (tr_top - tr_bottom) > 0 else 0
            tr_pos = targets.get("tr_position%")
            # 方向优先概率/空间: 向上→取保守档上方概率, 向下→保守档下方概率
            if targets.get("direction") == "up":
                p = targets.get("上方概率_保守")
                s = targets.get("上方空间_保守%")
                t = targets.get("横向计数上方目标_保守")
                if p is not None and s is not None and t is not None:
                    sign = "+" if s > 0 else ""
                    ps = f"保守目标 {t:.2f}{sign}{s:.1f}% 概率{int(p*100)}%"
                else:
                    ps = "目标待确认"
            elif targets.get("direction") == "down":
                p = targets.get("下方概率_保守")
                s = targets.get("下方空间_保守%")
                t = targets.get("横向计数下方目标_保守")
                if p is not None and s is not None and t is not None:
                    sign = "+" if s > 0 else ""
                    ps = f"保守目标 {t:.2f}{sign}{s:.1f}% 概率{int(p*100)}%"
                else:
                    ps = "目标待确认"
            else:
                up_p = targets.get("上方概率_保守")
                up_s = targets.get("上方空间_保守%")
                up_t = targets.get("横向计数上方目标_保守")
                dn_p = targets.get("下方概率_保守")
                dn_s = targets.get("下方空间_保守%")
                dn_t = targets.get("横向计数下方目标_保守")
                parts = []
                if up_t is not None and up_s is not None and up_p is not None:
                    sign = "+" if up_s > 0 else ""
                    parts.append(f"上{up_t:.2f}{sign}{up_s:.1f}%{int(up_p*100)}%")
                if dn_t is not None and dn_s is not None and dn_p is not None:
                    sign = "+" if dn_s > 0 else ""
                    parts.append(f"下{dn_t:.2f}{sign}{dn_s:.1f}%{int(dn_p*100)}%")
                ps = " / ".join(parts) if parts else "目标待确认"
            poc_s = f" · POC {float(poc):.2f}" if poc else ""
            pos_s = f" · TR位{tr_pos:.0f}%" if tr_pos is not None else ""
            ratio_s = f" · 因/TR={cause_ratio:.2f}" if cause_ratio > 0 else ""
            line = (f"{zone_label} {tr_bottom:.2f}~{tr_top:.2f}{pos_s}{poc_s}"
                    f"  |  威科夫横向计数: {_cn}列×格×反转 因{_ca:.2f}{ratio_s}"
                    f"  |  {ps}"
                    f"  |  {zone_note}")
            _, t_edge = self._zone_colors(targets)
            self._pin(line, t_edge, 0.018, bold=True, size=self._fs(1))
        if history:
            parts = []
            for h in history[-4:]:
                hit = bool(h.get("up_hit") or h.get("down_hit"))
                mark = "✓" if hit else "✗"
                parts.append(f"段{int(h.get('seq', 0))} {h.get('zone', '')}"
                             f"{h.get('tr_bottom', 0):.2f}~{h.get('tr_top', 0):.2f}{mark}")
            self._pin("历史: " + "  ".join(parts), theme.C_MUTED,
                      0.045, bold=True, size=self._fs(0))

        # 成交量 (列级柱 + 箱体量 Volume-at-Price)
        self._render_volume(cols, box, volumes)

    def _render_volume(self, cols, box, volumes):
        """列级成交量柱 (底部面板, 每列一根) + 箱体量 (右侧面板)。"""
        has_vol = bool(volumes and volumes.get("col_max", 0) > 0
                       and volumes.get("row_max", 0) > 0)
        if self._vol_plot is not None:
            self._vol_plot.setVisible(has_vol)
        if self._vap_plot is not None:
            self._vap_plot.setVisible(has_vol)
        if not has_vol or self._vol_plot is None or self._vap_plot is None:
            return
        n = len(cols)
        col_vols = volumes["col_vols"]
        col_max = volumes["col_max"]
        heights = [(v / col_max if col_max > 0 else 0.0) for v in col_vols]
        brushes = []
        for c in cols:
            base = pg.mkColor(theme.C_UP if c["type"] == "X" else theme.C_DOWN)
            base.setAlphaF(0.85)
            brushes.append(QtGui.QBrush(base))
        self._vol_plot.addItem(pg.BarGraphItem(
            x=list(range(n)), height=heights, width=0.88, brushes=brushes))
        self._col_heights = heights
        x0, x1 = self._vol_plot.getViewBox().viewRange()[0]
        self._fit_vol_y(x0, x1)

        row_max = volumes["row_max"]
        self._vap_plot.addItem(_VapBarsItem(volumes["row_vols"], box, row_max))
        self._vap_plot.getViewBox().setXRange(0, row_max, padding=0)

    def _zone_meta(self, targets):
        """区间语义标签: (zone_label, zone_note)。位置为主、突破方向为辅。"""
        direction = targets.get("direction", "range")
        tr_top = float(targets.get("tr_top", 0))
        tr_bottom = float(targets.get("tr_bottom", 0))
        mid = (tr_top + tr_bottom) / 2
        tr_c0 = int(targets.get("tr_start_col", 0))
        loc = self._cols[max(0, tr_c0 - 30):]
        if loc:
            _lo = min(c["lo"] for c in loc)
            _hi = max(c["hi"] for c in loc)
            _pos = (mid - _lo) / (_hi - _lo) if _hi > _lo else 0.5
        else:
            _pos = 0.5
        if direction == "up":
            if _pos > 2 / 3:
                return "派发区间", "高位上冲 → 警惕UTAD, 当前TR实为派发区间"
            return "吸筹区间", "低位向上突破 → 当前TR为吸筹区间"
        if direction == "down":
            if _pos < 1 / 3:
                return "吸筹区间", "低位下破 → 警惕Spring, 当前TR实为吸筹区间"
            return "派发区间", "高位向下破位 → 当前TR为派发区间"
        return "TR区间(整理中)", "仍在区间内盘整 → 等待突破确认方向"

    def _zone_colors(self, targets):
        direction = targets.get("direction", "range")
        tr_c0 = int(targets.get("tr_start_col", 0))
        tr_top = float(targets.get("tr_top", 0))
        tr_bottom = float(targets.get("tr_bottom", 0))
        mid = (tr_top + tr_bottom) / 2
        loc = self._cols[max(0, tr_c0 - 30):]
        if loc:
            _lo = min(c["lo"] for c in loc)
            _hi = max(c["hi"] for c in loc)
            _pos = (mid - _lo) / (_hi - _lo) if _hi > _lo else 0.5
        else:
            _pos = 0.5
        if direction == "up":
            if _pos > 2 / 3:
                return theme.C_ZONE_DIST, "#047857"
            return theme.C_ZONE_ACC, "#be123c"
        if direction == "down":
            if _pos < 1 / 3:
                return theme.C_ZONE_ACC, "#be123c"
            return theme.C_ZONE_DIST, "#047857"
        return theme.C_ZONE_NEUT, "#495057"

    def _draw_history(self, plot, h, n):
        top = float(h.get("tr_top", 0))
        bottom = float(h.get("tr_bottom", 0))
        c0 = float(h.get("tr_start_col", 0))
        c1 = float(h.get("tr_end_col", n))
        zone = h.get("zone", "")
        yhi = self._full_y[1]
        for yv in (top, bottom):
            plot.addItem(pg.InfiniteLine(
                pos=yv, angle=0,
                pen=_pen("#94a3b8", 0.8, Qt.PenStyle.DotLine, 0.6)))
        _fill, _border = self._chip()
        # 段标签: 仅短标 (段号+区间), 详细语义在顶部信息条, 减少压格
        self._text(plot, (c0 + c1) / 2, top + (yhi - bottom) * 0.012,
                   f"段{int(h.get('seq', 0))} {bottom:.2f}~{top:.2f}",
                   "#be123c" if zone == "吸筹" else "#047857",
                   anchor=(0.5, 0), bold=True, delta=0,
                   fill=_fill, border=_border)
        tx = c1 + 0.3
        if zone == "吸筹" and h.get("up_target") is not None:
            hit = bool(h.get("up_hit"))
            col = "#16a34a" if hit else "#94a3b8"
            style = None if hit else Qt.PenStyle.DotLine
            plot.addItem(pg.InfiniteLine(
                pos=float(h["up_target"]), angle=0,
                pen=_pen(col, 1.0, style, 0.95 if hit else 0.7)))
            self._text(plot, tx, float(h["up_target"]),
                       f"{'已到' if hit else '未到'} 上涨目标 "
                       f"{h['up_target']:.2f}", col,
                       anchor=(0, 0.5), delta=-1, bold=bool(hit),
                       fill=_fill, border=_border)
        if zone == "派发" and h.get("down_target") is not None:
            hit = bool(h.get("down_hit"))
            col = "#dc2626" if hit else "#94a3b8"
            style = None if hit else Qt.PenStyle.DotLine
            plot.addItem(pg.InfiniteLine(
                pos=float(h["down_target"]), angle=0,
                pen=_pen(col, 1.0, style, 0.95 if hit else 0.7)))
            self._text(plot, tx, float(h["down_target"]),
                       f"{'已到' if hit else '未到'} 下跌目标 "
                       f"{h['down_target']:.2f}", col,
                       anchor=(0, 0.5), delta=-1, bold=bool(hit),
                       fill=_fill, border=_border)

    def _draw_targets(self, plot, targets, n):
        tr_top = float(targets["tr_top"])
        tr_bottom = float(targets["tr_bottom"])
        direction = targets.get("direction", "range")
        c0 = float(targets.get("tr_start_col", 0))
        c1 = float(targets.get("tr_end_col", n))
        cend = n + 0.5
        box = self._box
        _, t_edge = self._zone_colors(targets)

        # ── POC / 价值区 ──
        poc = targets.get("poc")
        vah = targets.get("vah")
        val_ = targets.get("val")
        if poc and vah and val_ and val_ < vah:
            # 价值区色带 (POC ± 35% TR 宽): 浅琥珀, 半透明
            from pyqtgraph import LinearRegionItem
            vr = LinearRegionItem(
                [val_, vah], orientation='horizontal',
                brush=_brush_alpha(theme.C_AMBER, 0.12),
                pen=_pen(theme.C_AMBER, 0.8, Qt.PenStyle.NoPen))
            plot.addItem(vr)
            # POC 虚线 (控制点, 更强锚点)
            plot.addItem(pg.InfiniteLine(
                pos=float(poc), angle=0,
                pen=_pen(theme.C_AMBER, 1.2, Qt.PenStyle.DashLine, 0.9)))
            _fill, _border = self._chip()
            # POC 文字标签 (只在 POC 不贴 TR 边界时画, 避免与 TR 线重叠)
            if abs(float(poc) - tr_top) > box * 1.5 and abs(float(poc) - tr_bottom) > box * 1.5:
                tr_pos = targets.get("tr_position%", 50)
                # TR 位<50%: POC 偏上方写; TR 位>50%: 偏下方写
                poc_y = float(poc) + (box * 1.2 if tr_pos < 50 else -box * 1.2)
                poc_anchor = (0, 0) if tr_pos < 50 else (0, 1)
                self._text(plot, c1 + 0.2, poc_y,
                           f"POC {float(poc):.2f} (价值中枢)", theme.C_AMBER,
                           anchor=poc_anchor, bold=True, delta=0,
                           fill=_fill, border=_border)

        # TR 上下沿 (虚线)
        plot.addItem(pg.InfiniteLine(
            pos=tr_top, angle=0,
            pen=_pen(t_edge, 1.2, Qt.PenStyle.DashLine)))
        plot.addItem(pg.InfiniteLine(
            pos=tr_bottom, angle=0,
            pen=_pen(t_edge, 1.2, Qt.PenStyle.DashLine)))

        # 计数起/止 双箭头 (区间语义/计数文字放在视口顶部信息条, 不压格子)
        _fill, _border = self._chip()
        for xc in (c0, c1):
            plot.addItem(pg.PlotCurveItem([xc, xc], [tr_bottom, tr_top],
                                          pen=_pen("#495057", 1.2)))
            for (yp, ang) in ((tr_top, -90), (tr_bottom, 90)):
                plot.addItem(pg.ArrowItem(
                    pos=(xc, yp), angle=ang, tailLen=0, headLen=8, headWidth=8,
                    pen=_pen("#495057"), brush=pg.mkBrush("#495057")))

        # ── 三档目标绘制: 保守(粗线/高概率色)/中/激进(细线/淡) ──
        # 保守档: 从区间极值投影 (最易到达, 粗实线)
        # 中档: 从 POC 投影 (次易到达, 虚线)
        # 激进档: 从 count_line 投影 (最难到达, 细点线)
        cause = float(targets.get("cause", 0))
        cols_count = int(targets.get("columns", 0))
        cause_s = f"+因{cause:.2f}({cols_count}列×格×反转)"
        cause_s_dn = f"-因{cause:.2f}({cols_count}列×格×反转)"
        active_up = direction in ("up", "range")
        active_dn = direction in ("down", "range")

        # ── 上涨方向三档 ──
        up_tiers = [
            # (目标key, 概率key, 空间key, 线宽, 样式, 透明度, 标签前缀)
            ("横向计数上方目标_保守", "上方概率_保守", "上方空间_保守%", 1.6, None, 0.95 if active_up else 0.4, "保"),
            ("横向计数上方目标_中",    "上方概率_中",    "上方空间_中%",    1.1, Qt.PenStyle.DashLine, 0.80 if active_up else 0.35, "中"),
            ("横向计数上方目标",        "上方概率_激进",  "上方空间_激进%",  0.9, Qt.PenStyle.DotLine,  0.65 if active_up else 0.3,  "激"),
        ]
        label_x = cend + 0.4
        label_dy = box * 1.6  # 三档标签垂直间距
        for idx, (tk, pk, sk, lw, style, a, label) in enumerate(up_tiers):
            if tk not in targets:
                continue
            t = float(targets[tk])
            prob = targets.get(pk)
            sp = targets.get(sk)
            # 颜色: 按概率分级 (高→深绿, 低→浅绿)
            if prob is not None and prob >= 0.7:
                col = "#166534"
            elif prob is not None and prob >= 0.5:
                col = "#2f9e44"
            else:
                col = "#8ce99a"
            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a)))
            # 只在最外档 (激进) 画箭头和因箭头
            if tk == "横向计数上方目标":
                plot.addItem(pg.PlotCurveItem(
                    [cend, cend], [tr_top, t], pen=_pen(col, 1.4, None, a)))
                plot.addItem(pg.ArrowItem(
                    pos=(cend, t), angle=-90, tailLen=0, headLen=9, headWidth=9,
                    pen=_pen(col, 1.0, None, a), brush=pg.mkBrush(col)))
            # 标签: 保/中/激 + 价格 + 空间% + 概率%
            p_pct = f"{int(prob*100)}%" if prob is not None else ""
            sign = "+" if (sp or 0) > 0 else ""
            sp_s = f"{sign}{sp:.1f}%" if sp is not None else ""
            lbl = f"{label}{t:.2f}{sp_s}{p_pct}"
            # 三档标签按概率高低上下错开, 避免重叠
            y_off = (idx - 1) * label_dy  # 保守-1, 中0, 激+1
            if direction == "up":
                y_off = idx * label_dy  # 向上时: 保守最下(在箭头旁), 中/激在上方
            self._text(plot, label_x, t + y_off, lbl, col, anchor=(0, 0.5),
                       bold=(idx == 0), delta=0, alpha=a, fill=_fill, border=_border)

        # 近端参考目标 (可到达口径)
        near = targets.get("近端上方目标")
        if isinstance(near, (int, float)):
            far_up = targets.get("横向计数上方目标")
            if far_up is None or abs(float(near) - float(far_up)) > box:
                a = 0.85 if active_up else 0.4
                plot.addItem(pg.InfiniteLine(
                    pos=float(near), angle=0,
                    pen=_pen("#82c91e", 1.0, Qt.PenStyle.DotLine, a)))
                sp = targets.get("上方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                self._text(plot, label_x, float(near) - label_dy, lbl, "#82c91e",
                           anchor=(0, 0.5), delta=0, alpha=a,
                           fill=_fill, border=_border)

        # ── 下跌方向三档 ──
        dn_tiers = [
            ("横向计数下方目标_保守", "下方概率_保守", "下方空间_保守%", 1.6, None, 0.95 if active_dn else 0.4, "保"),
            ("横向计数下方目标_中",    "下方概率_中",    "下方空间_中%",    1.1, Qt.PenStyle.DashLine, 0.80 if active_dn else 0.35, "中"),
            ("横向计数下方目标",        "下方概率_激进",  "下方空间_激进%",  0.9, Qt.PenStyle.DotLine,  0.65 if active_dn else 0.3,  "激"),
        ]
        for idx, (tk, pk, sk, lw, style, a, label) in enumerate(dn_tiers):
            if tk not in targets:
                continue
            t = float(targets[tk])
            prob = targets.get(pk)
            sp = targets.get(sk)
            if prob is not None and prob >= 0.7:
                col = "#8a1a1a"
            elif prob is not None and prob >= 0.5:
                col = "#e03131"
            else:
                col = "#ffa8a8"
            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a)))
            if tk == "横向计数下方目标":
                plot.addItem(pg.PlotCurveItem(
                    [cend, cend], [tr_bottom, t], pen=_pen(col, 1.4, None, a)))
                plot.addItem(pg.ArrowItem(
                    pos=(cend, t), angle=90, tailLen=0, headLen=9, headWidth=9,
                    pen=_pen(col, 1.0, None, a), brush=pg.mkBrush(col)))
            p_pct = f"{int(prob*100)}%" if prob is not None else ""
            sign = "+" if (sp or 0) > 0 else ""
            sp_s = f"{sign}{sp:.1f}%" if sp is not None else ""
            lbl = f"{label}{t:.2f}{sp_s}{p_pct}"
            # 向下时: 保守档最高(在箭头旁), 中/激叠在下方
            y_off = (1 - idx) * label_dy if direction == "down" else (idx - 1) * label_dy
            self._text(plot, label_x, t + y_off, lbl, col, anchor=(0, 0.5),
                       bold=(idx == 0), delta=0, alpha=a, fill=_fill, border=_border)

        # 下跌近端参考
        near = targets.get("近端下方目标")
        if isinstance(near, (int, float)):
            far_dn = targets.get("横向计数下方目标")
            if far_dn is None or abs(float(near) - float(far_dn)) > box:
                a = 0.85 if active_dn else 0.4
                plot.addItem(pg.InfiniteLine(
                    pos=float(near), angle=0,
                    pen=_pen("#f08c00", 1.0, Qt.PenStyle.DotLine, a)))
                sp = targets.get("下方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                self._text(plot, label_x, float(near) + label_dy, lbl, "#f08c00",
                           anchor=(0, 0.5), delta=0, alpha=a,
                           fill=_fill, border=_border)

    # ── 视图 / 交互 ──
    def _apply_initial_view(self):
        n = self._n
        fx0, fx1 = self._full_x
        x0 = max(fx0, n - _DEFAULT_COLS)
        # 只设 x 范围, y 由 aspect 锁按比例联动 (方格为正方形)
        self._vb.setRange(xRange=(x0, fx1), padding=0)
        y0, y1 = self._vb.viewRange()[1]
        # 把 y 窗口中心对齐到可见列的格子中位, 保持同一跨度 (不破坏正方形)
        i0 = max(0, int(x0))
        rows = [r for c in self._cols[i0:] for r in c["rows"]]
        if rows:
            mid = (min(rows) + max(rows)) / 2.0 * self._box
            span = y1 - y0
            fy0, fy1 = self._full_y
            y0c = mid - span / 2
            y1c = mid + span / 2
            if y0c < fy0:
                y0c, y1c = fy0, fy0 + span
            if y1c > fy1:
                y1c, y0c = fy1, fy1 - span
            self._vb.setRange(xRange=(x0, fx1), yRange=(y0c, y1c), padding=0)
        x0, x1 = self._vb.viewRange()[0]
        y0, y1 = self._vb.viewRange()[1]
        self._push_view(x0, x1, y0, y1)

    def _default_y(self, x0):
        """默认视野 (最近 N 列) 的 Y 范围: 可见列格子 + 当前 TR/目标线。"""
        i0 = max(0, int(x0))
        rows = [r for c in self._cols[i0:] for r in c["rows"]]
        if not rows:
            return self._full_y
        ymin = min(rows) * self._box
        ymax = max(rows) * self._box
        targets = self._cur_targets if getattr(self, "_cur_targets", None) else None
        if targets:
            for k in ("tr_top", "tr_bottom", "横向计数上方目标",
                      "横向计数下方目标", "近端上方目标", "近端下方目标"):
                v = targets.get(k)
                if isinstance(v, (int, float)):
                    ymin = min(ymin, float(v))
                    ymax = max(ymax, float(v))
        pad = (ymax - ymin) * 0.10 if ymax > ymin else max(self._box, 1.0)
        return max(self._full_y[0], ymin - pad), min(self._full_y[1], ymax + pad)

    def _fs(self, delta=0):
        return max(6, self._font_size + delta)

    def _font(self, delta=0, bold=False):
        f = QtGui.QFont()
        f.setFamily(FONT_CANDIDATES[0])
        f.setPointSize(self._fs(delta))
        f.setBold(bold)
        return f

    def _text(self, plot, x, y, text, color, anchor=(0.5, 0.5),
              delta=0, bold=False, fill=None, border=None, alpha=1.0):
        kwargs = {}
        if fill is not None:
            kwargs["fill"] = fill
        if border is not None:
            kwargs["border"] = border
        ti = _DragTextItem(text, color=color, anchor=anchor, **kwargs)
        ti.setFont(self._font(delta, bold))
        ti.setPos(float(x), float(y))
        if alpha is not None and alpha < 1.0:
            ti.setOpacity(float(alpha))
        plot.addItem(ti)
        return ti

    def _chip(self):
        """标注底色块 (panel 底色 + 边框), 压在图格上也能看清文字。"""
        return (pg.mkBrush(theme.C_PANEL), _pen(theme.C_BORDER, 1.0))

    def _pin(self, text, color, fy, bold=False, size=10):
        """底部解读行: 固定在视口 (fy 为 0~1 纵向位置), 不随平移缩放移动;
        可鼠标拖拽移动, 拖拽后位置按视口比例保留。"""
        ti = _DragTextItem(text, color=color, anchor=(0.5, 0.5))
        f = self._font(bold=bold)
        f.setPointSize(size)
        ti.setFont(f)
        ti.setParentItem(self._vb)
        ti.set_vp(0.5, fy)
        self._pinned.append((ti, fy))
        self._reposition_pinned()
        return ti

    def _reposition_pinned(self):
        if self._vb is None:
            return
        rect = self._vb.rect()
        for ti, fy in self._pinned:
            vx, vy = getattr(ti, "_vp", (0.5, fy))
            ti.setPos(rect.width() * vx, rect.height() * vy)

    def _push_view(self, x0, x1, y0, y1):
        key = (float(x0), float(x1), float(y0), float(y1))
        if self._hist and self._hist[self._hist_pos] == key:
            return
        self._hist = self._hist[:self._hist_pos + 1]
        self._hist.append(key)
        self._hist_pos = len(self._hist) - 1

    def apply_view(self, x0, x1, y0, y1, push=True):
        if self._n == 0:
            return None
        fx0, fx1 = self._full_x
        fy0, fy1 = self._full_y
        x0 = max(float(x0), fx0)
        x1 = min(float(x1), fx1)
        y0 = max(float(y0), fy0)
        y1 = min(float(y1), fy1)
        if x1 - x0 < 2 or y1 - y0 <= 0:
            return None
        # 单次 setRange: aspect 锁统一约束两轴, 保持方格正方形
        self._vb.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        if push:
            self._push_view(x0, x1, y0, y1)
        return (x0, x1, y0, y1)

    def reset_view(self):
        """复位到全幅: 只设 x 范围, y 由 aspect 锁按比例联动扩展。
        目标线 (尤其威科夫三档目标中的较远档) 可能把 full_y 拉得较高, 若同时
        设定 y 范围会导致 aspect 锁反向扩张 x, 使 x 无法精确复位到全幅。"""
        if self._n:
            fx0, fx1 = self._full_x
            self._vb.setRange(xRange=(fx0, fx1), padding=0)
            self._push_view(*self._vb.viewRange()[0], *self._vb.viewRange()[1])

    def zoom_about(self, cx, cy, factor):
        if self._n == 0:
            return
        x0, x1 = self._vb.viewRange()[0]
        fx0, fx1 = self._full_x
        full_xspan = fx1 - fx0
        if full_xspan <= 0:
            return
        xspan = x1 - x0
        nxspan = min(max(xspan * factor, 3.0), full_xspan)
        # aspect 锁下两轴等比例缩放; 已达全幅则复位
        if nxspan >= full_xspan - 0.5:
            self.reset_view()
            return
        t = min(max((cx - x0) / xspan, 0.0), 1.0) if xspan > 0 else 0.5
        nx0 = cx - nxspan * t
        nx1 = nx0 + nxspan
        if nx0 < fx0:
            nx0, nx1 = fx0, fx0 + nxspan
        if nx1 > fx1:
            nx1, nx0 = fx1, fx1 - nxspan
        # 只设置 x 范围, y 由 aspect 锁按比例联动 (方格始终为正方形)
        self._vb.setRange(xRange=(nx0, nx1), padding=0)
        self._push_view(*self._vb.viewRange()[0], *self._vb.viewRange()[1])

    def pan_by(self, frac):
        if self._n == 0:
            return
        x0, x1 = self._vb.viewRange()[0]
        y0, y1 = self._vb.viewRange()[1]
        fx0, fx1 = self._full_x
        full_span = fx1 - fx0
        span = x1 - x0
        if full_span <= 0 or span <= 0:
            return
        if span >= full_span - 0.5:
            self.apply_view(fx0, fx1, y0, y1)
            return
        nx0 = min(max(x0 + span * frac, fx0), fx1 - span)
        self.apply_view(nx0, nx0 + span, y0, y1)

    def nav_hist(self, step):
        if not self._hist:
            return
        pos = self._hist_pos + step
        if 0 <= pos < len(self._hist):
            self._hist_pos = pos
            x0, x1, y0, y1 = self._hist[pos]
            self._vb.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)

    def keyPressEvent(self, ev):
        if self._n == 0:
            return super().keyPressEvent(ev)
        key = ev.key()
        x0, x1 = self._vb.viewRange()[0]
        y0, y1 = self._vb.viewRange()[1]
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_about((x0 + x1) / 2, (y0 + y1) / 2, 0.8)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_about((x0 + x1) / 2, (y0 + y1) / 2, 1.25)
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
