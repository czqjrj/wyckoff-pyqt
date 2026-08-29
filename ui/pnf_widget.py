"""pyqtgraph P&F 点数图控件 (替换原 matplotlib 点数图)。

数据由 AnalysisThread 在 worker 线程通过 build_pnf_data() 收集 (点数图计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

交互:
  - 滚轮        以光标为锚点 X/Y 等比缩放 (保持格子正方形), 缩放后列边界对齐像素
  - 左键拖拽    平移; Shift+左键拖拽 框选放大 (以框选 X 跨度驱动 Y)
  - 双击        复位到全幅
  - 键盘        + / - 缩放, ←/→ 十字光标逐列步进 (视口边缘自动跟随;
                Shift+←/→ 快速平移), ↑/↓ 平移, PgUp/PgDn 大步平移,
                Home/r 复位, End 回到最新列, Backspace/f 视图历史,
                [ / ] 格值缩放 (发 box_scale_requested 信号由主窗口重算),
                Alt+←/→ 十字光标逐列步进 (同 ←/→, 兼容旧习惯),
                1–9 取视图书签 / Shift+数字 存书签, ? 快捷键帮助
  - 十字光标    吸附最近列, 顶部读数显示该列 X/O·格数·量·日期区间;
                竖线延伸到底部量柱面板, 横线延伸到右侧箱体量面板
"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import FONT_CANDIDATES
from wyckoff.pnf import pnf_box_label, pnf_cap, pnf_hist_title

from . import theme
from .base_plot import ViewHistoryMixin
from .constants import PNF_DEFAULT_COLS, PNF_PRICE_AXIS_W, PNF_RIGHT_MARGIN
from .crosshair import Crosshair
from .renderers import (
    PnfBandsRenderer,
    PnfGridRenderer,
    PnfHistoryRenderer,
    PnfTargetsRenderer,
    PnfVolumeRenderer,
)
from .renderers.pnf_grid import _brush_alpha, _DragTextItem, _fmt_cn_vol, _LatestBtnItem, _pen

# A股配色: 红涨绿跌 (X=上涨列, O=下跌列), 运行时从 theme 动态取色


class _PnfViewBox(pg.ViewBox):
    """点数图 ViewBox: 滚轮 X/Y 等比缩放, 双击复位, Shift+拖拽框选放大。"""

    def __init__(self, host, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._host = host
        self._rb_item = None
        self._rb_down = None

    def wheelEvent(self, ev, axis=None):
        # pyqtgraph≥0.13 坐标轴滚轮以 axis=0/1 委托到 ViewBox; 只缩放对应轴
        if axis is not None:
            return super().wheelEvent(ev, axis=axis)
        pos = self.mapSceneToView(ev.scenePos())
        self._host.zoom_about(pos.x(), pos.y(),
                              0.8 if ev.delta() > 0 else 1.25)
        ev.accept()

    def mouseDragEvent(self, ev):
        # Shift+左键拖拽 → 框选放大 (以框的 X 跨度为准, Y 由 aspect 锁联动)
        if (ev.button() == Qt.MouseButton.LeftButton
                and (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                and self._host._n > 0):
            ev.accept()
            pos = ev.pos()
            if ev.isStart():
                self._rb_down = QtCore.QPointF(ev.buttonDownPos())
                pen = _pen(theme.C_ACCENT, 1.0)
                brush = _brush_alpha(theme.C_ACCENT, 0.12)
                self._rb_item = QtWidgets.QGraphicsRectItem()
                self._rb_item.setPen(pen)
                self._rb_item.setBrush(brush)
                self._rb_item.setZValue(1e6)
                # 挂到 childGroup (数据坐标系), 随视图变换自动映射
                self._rb_item.setParentItem(self.childGroup)
            if self._rb_item is None or self._rb_down is None:
                return
            x0, x1 = sorted((self._rb_down.x(), pos.x()))
            y0, y1 = sorted((self._rb_down.y(), pos.y()))
            self._rb_item.setVisible(True)
            self._rb_item.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
            if ev.isFinish():
                try:
                    self._rb_item.scene().removeItem(self._rb_item)
                except (RuntimeError, TypeError):
                    pass
                self._rb_item = None
                self._rb_down = None
                self._host.box_zoom(x0, x1)
            return
        super().mouseDragEvent(ev)

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

    def wheelEvent(self, ev, axis=None):
        if axis is not None:
            return super().wheelEvent(ev, axis=axis)
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









class PnfWidget(ViewHistoryMixin, pg.GraphicsLayoutWidget):
    """pyqtgraph P&F 点数图: X 列红方块 / O 列绿方块 + TR 区间/目标位标注。

    set_data(**pnf_data) 接收 build_pnf_data() 的返回 (可附 code=标的代码,
    同一标的重复刷新时保留用户当前视野), 其余交互内置。
    """

    # [ / ] 键请求格值缩放 (mult<1 缩小格值), 由主窗口重算后回填 set_data
    box_scale_requested = QtCore.pyqtSignal(float)

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._cols = []
        self._box = 1.0
        self._n = 0
        self._full_x = (0.0, 1.0)
        self._full_y = (0.0, 1.0)
        self._hist_init()
        self._pinned = []
        self.plot = None
        self._vb = None
        self._vol_plot = None
        self._vap_plot = None
        self._volumes = None
        self._col_heights = []
        self._sync_lock = False
        self._crosshairs = []
        self._pending_view = False  # 待显示时重定位到最新列 (见 showEvent)
        # ── 交互增强状态 ──
        self._code = None           # 当前标的代码 (刷新保留视野用)
        self._keep_x = None         # 刷新后待恢复的 x 范围
        self._bookmarks = {}        # 视图书签 {1-9: (x0,x1,y0,y1)}
        self._ch = None             # 主十字光标引用 (键盘步进用)
        self._ln_vol = None         # 量柱面板竖线 (与主图十字联动)
        self._ln_vap = None         # VAP 面板横线 (与主图十字联动)
        self._snap_lock = False     # 像素对齐防重入
        self._layer_items = {}      # 图层名 → item 列表 (可见性开关)
        self._layer_visible = dict.fromkeys(
            ("targets", "bands", "poc", "history", "volume", "vap"), True)
        self._hud = None            # 右上角视野范围指示
        self._latest_btn = None     # 右下角 "回到最新列" 按钮
        self._help_item = None      # ? 快捷键帮助浮层

        self.setBackground(pg.mkColor(theme.C_PANEL))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_plot()
        self.plot.setTitle("输入 A 股代码 (如 600104 / sh600104 / 000001), "
                           "点击\"开始分析\"加载点数图。")
        if self._vol_plot is not None:
            self._vol_plot.setVisible(False)
        if self._vap_plot is not None:
            self._vap_plot.setVisible(False)
        self._build_overlays()

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
        self.plot.getAxis("left").setWidth(PNF_PRICE_AXIS_W)

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
        vol_axis.setWidth(PNF_PRICE_AXIS_W)
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
        self._update_hud()
        self._update_latest_btn()

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

    # ── 图层开关 ──
    LAYER_LABELS = {
        "targets": "目标线/计数",
        "bands": "TR 底色带",
        "poc": "POC/价值区",
        "history": "历史段标注",
        "volume": "量柱面板",
        "vap": "箱体量面板",
    }

    def layer_state(self):
        """当前图层可见性 {图层名: bool}, 供右键菜单勾选。"""
        return dict(self._layer_visible)

    def set_layer_visible(self, name, flag):
        """开关图层 (目标线/TR带/POC/历史段/量柱/VAP 面板)。"""
        if name not in self._layer_visible:
            return
        self._layer_visible[name] = bool(flag)
        self._apply_layer(name)
        if name in ("volume", "vap") and self._ch is not None:
            # 十字延伸线随面板显隐即时刷新
            jx = int(round(self._ch._x)) if self._ch._x is not None else 0
            self._on_cross_move(jx)

    def _apply_layer(self, name):
        vis = bool(self._layer_visible.get(name, True))
        if name == "volume":
            if self._vol_plot is not None:
                has = bool(self._volumes
                           and self._volumes.get("col_max", 0) > 0)
                self._vol_plot.setVisible(vis and has)
        elif name == "vap":
            if self._vap_plot is not None:
                has = bool(self._volumes
                           and self._volumes.get("row_max", 0) > 0)
                self._vap_plot.setVisible(vis and has)
        else:
            for it in self._layer_items.get(name, []):
                try:
                    it.setVisible(vis)
                except (RuntimeError, TypeError):
                    pass

    # ── 导出 ──
    def export_csv(self, path):
        """导出列数据 CSV: 列号/X-O/格数/价格区间/K线索引/日期区间/成交量。"""
        import csv
        vols = ((self._volumes or {}).get("col_vols")) or []
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["列号", "类型", "格数", "低", "高", "起始K线",
                        "结束K线", "起始日期", "结束日期", "成交量"])
            for j, c in enumerate(self._cols):
                rows = c.get("rows") or []
                w.writerow([j, c["type"], len(rows),
                            c.get("lo", ""), c.get("hi", ""),
                            c.get("i0", ""), c.get("i1", ""),
                            c.get("date0", ""), c.get("date1", ""),
                            vols[j] if j < len(vols) else ""])

    # ── 数据入口 ──
    def set_data(self, cols=None, box=None, title="", targets=None,
                 history=None, volumes=None, box_mode="pct", atr_factor=0.5,
                 code=None, **extra):
        # 刷新保留视野: 同一标的且此前有数据 → 记住当前 x 视野, 建图后恢复,
        # 避免周期刷新把正在细看的区间弹回默认视野。
        keep = (code is not None and code == self._code and self._n > 0
                and self._vb is not None)
        prev_x = tuple(self._vb.viewRange()[0]) if keep else None
        self._code = code
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
            # 常驻覆盖层 (HUD/回到最新按钮/帮助) 不随数据重建销毁
            if not getattr(ti, "_ephemeral", True):
                continue
            if ti.scene() is not None:
                ti.scene().removeItem(ti)
        self._pinned = [pt for pt in self._pinned
                        if not getattr(pt[0], "_ephemeral", True)]
        self._hist_clear()
        self._keep_x = None
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
            self._update_hud()
            self._update_latest_btn()
            return
        self._cols = cols
        self._box = float(box)
        self._n = len(cols)
        self._cur_targets = targets or {}
        if keep and prev_x is not None:
            fx0, fx1 = self._full_x
            kx0 = max(float(prev_x[0]), fx0)
            kx1 = min(float(prev_x[1]), fx1)
            if kx1 - kx0 >= 2:
                self._keep_x = (kx0, kx1)
        self._build(cols, self._box, title or "", targets or {},
                    history or [], self._volumes)
        # 经典圈叉图: 格子必须为正方形 — 锁定纵横比 (1 列宽 = box 格高)
        self._vb.setAspectLocked(True, ratio=self._box)
        # set_data 常发生在隐藏页签 (控件尺寸未定), aspect 锁下此时算出的
        # 初始视野可能失真 → 置位标记, 首次真正显示时在 showEvent 重定位
        self._pending_view = True
        self._apply_initial_view()
        self._attach_crosshairs()
        for name in self._layer_visible:
            self._apply_layer(name)
        self._update_hud()
        self._update_latest_btn()

    # ── 十字光标 ──
    def _detach_crosshairs(self):
        for ch in self._crosshairs:
            ch.detach()
        self._crosshairs = []
        for ln, plot in ((self._ln_vol, self._vol_plot),
                         (self._ln_vap, self._vap_plot)):
            if ln is None or plot is None:
                continue
            try:
                plot.removeItem(ln)
            except (ValueError, RuntimeError, TypeError):
                pass
        self._ln_vol = None
        self._ln_vap = None
        self._ch = None

    def _fmt_col_x(self, sx):
        """十字光标顶部读数: 列号吸附 + 该列详情 (X/O · 格数 · 量 · 日期区间)。"""
        j = int(round(sx))
        if j < 0 or j >= self._n:
            return f"列 {sx:.0f}"
        c = self._cols[j]
        s = f"列{j} {'X' if c['type'] == 'X' else 'O'}·{len(c['rows'])}格"
        vols = (self._volumes or {}).get("col_vols")
        if vols and 0 <= j < len(vols) and vols[j] > 0:
            s += f"·量{_fmt_cn_vol(vols[j])}"
        d0, d1 = c.get("date0") or "", c.get("date1") or ""
        if d0:
            if d1 and d1 != d0:
                tail = d1[5:] if (len(d0) == len(d1) and d0[:5] == d1[:5]) \
                    else d1
                s += f" {d0}~{tail}"
            else:
                s += f" {d0}"
        return s

    def _on_cross_move(self, jx):
        """十字移动回调: 同步竖线到底部量柱面板、横线到右侧 VAP 面板。"""
        ch = self._ch
        if ch is None:
            return
        if self._ln_vol is not None:
            vis = (bool(ch.vline.isVisible()) and bool(self._volumes)
                   and self._layer_visible.get("volume", True))
            self._ln_vol.setVisible(vis)
            if vis:
                self._ln_vol.setPos(float(jx))
        if self._ln_vap is not None:
            vis = (bool(ch.hline.isVisible()) and ch._y is not None
                   and bool(self._volumes)
                   and self._layer_visible.get("vap", True))
            self._ln_vap.setVisible(vis)
            if vis and ch._y is not None:
                self._ln_vap.setPos(float(ch._y))

    def _attach_crosshairs(self):
        self._detach_crosshairs()
        if self._n <= 0:
            return
        # snap=True: X 吸附最近列中心; fmt_x 输出该列详情信息卡
        self._ch = Crosshair(self.plot, self._fmt_col_x,
                             lambda v: f"{v:.2f}",
                             font_size=self._fs(0), snap=True,
                             on_move=self._on_cross_move)
        self._crosshairs.append(self._ch)
        pen = _pen(theme.C_MUTED, 1.0, Qt.PenStyle.DashLine, 0.8)
        if self._vol_plot is not None:
            self._ln_vol = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            self._ln_vol.setZValue(50)
            self._ln_vol.setVisible(False)
            self._vol_plot.addItem(self._ln_vol, ignoreBounds=True)
        if self._vap_plot is not None:
            self._ln_vap = pg.InfiniteLine(angle=0, movable=False, pen=pen)
            self._ln_vap.setZValue(50)
            self._ln_vap.setVisible(False)
            self._vap_plot.addItem(self._ln_vap, ignoreBounds=True)

    # ── 绘制 ──
    def _build(self, cols, box, title, targets, history, volumes=None):
        plot = self.plot
        n = len(cols)
        self._layer_items = {}

        def collect(layer, fn):
            """执行 fn 期间新加入 plot 的 item 归入图层 layer (可见性开关用)。"""
            before = {id(it) for it in plot.items}
            fn()
            lst = self._layer_items.setdefault(layer, [])
            vis = self._layer_visible.get(layer, True)
            for it in plot.items:
                if id(it) not in before:
                    lst.append(it)
                    try:
                        it.setVisible(vis)
                    except (RuntimeError, TypeError):
                        pass

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
        self._full_x = (-0.6, n + PNF_RIGHT_MARGIN)
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
            renderer = PnfBandsRenderer(bands, self._full_y)
            collect("bands", lambda: plot.addItem(renderer.create_item()))

        # 格子 (不属于任何可关图层, 始终显示)
        grid_renderer = PnfGridRenderer(cols, box)
        plot.addItem(grid_renderer.create_item())

        # 历史段标注
        if history:
            hist_renderer = PnfHistoryRenderer(history, cols, box, self._full_y, collect)
            hist_renderer.draw(plot)

        # 当前 TR / 目标
        if targets:
            targets_renderer = PnfTargetsRenderer(targets, cols, box, self._full_y, collect)
            targets_renderer.draw(plot)

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

        volumes["row_max"]
        vap_renderer = PnfVolumeRenderer(cols, box, volumes)
        vap_renderer.render_vap(self._vap_plot)

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
                return theme.C_ZONE_DIST, theme.C_DOWN
            return theme.C_ZONE_ACC, theme.C_UP
        if direction == "down":
            if _pos < 1 / 3:
                return theme.C_ZONE_ACC, theme.C_UP
            return theme.C_ZONE_DIST, theme.C_DOWN
        return theme.C_ZONE_NEUT, theme.C_MUTED

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
                pen=_pen(theme.C_MUTED, 0.8, Qt.PenStyle.DotLine, 0.6)))
        _fill, _border = self._chip()
        # 段标签: 仅短标 (段号+区间), 详细语义在顶部信息条, 减少压格
        self._text(plot, (c0 + c1) / 2, top + (yhi - bottom) * 0.012,
                   f"段{int(h.get('seq', 0))} {bottom:.2f}~{top:.2f}",
                   theme.C_UP if zone == "吸筹" else theme.C_DOWN,
                   anchor=(0.5, 0), bold=True, delta=0,
                   fill=_fill, border=_border)
        tx = c1 + 0.3
        if zone == "吸筹" and h.get("up_target") is not None:
            hit = bool(h.get("up_hit"))
            col = theme.C_DOWN if hit else theme.C_MUTED
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
            col = theme.C_UP if hit else theme.C_MUTED
            style = None if hit else Qt.PenStyle.DotLine
            plot.addItem(pg.InfiniteLine(
                pos=float(h["down_target"]), angle=0,
                pen=_pen(col, 1.0, style, 0.95 if hit else 0.7)))
            self._text(plot, tx, float(h["down_target"]),
                       f"{'已到' if hit else '未到'} 下跌目标 "
                       f"{h['down_target']:.2f}", col,
                       anchor=(0, 0.5), delta=-1, bold=bool(hit),
                       fill=_fill, border=_border)

    def _draw_targets(self, plot, targets, n, collect=None):
        if collect is None:
            def collect(layer, fn):
                fn()
        # POC/价值区 与 TR/目标线分属两个可开关图层
        collect("poc", lambda: self._draw_poc(plot, targets))
        collect("targets", lambda: self._draw_target_levels(plot, targets, n))

    def _draw_poc(self, plot, targets):
        """POC 价值中枢 + 价值区色带 (图层 "poc")。"""
        poc = targets.get("poc")
        vah = targets.get("vah")
        val_ = targets.get("val")
        if not (poc and vah and val_ and val_ < vah):
            return
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
        box = self._box
        tr_top = float(targets.get("tr_top", 0))
        tr_bottom = float(targets.get("tr_bottom", 0))
        c1 = float(targets.get("tr_end_col",
                               max(0, len(self._cols) - 1))) + 0.2
        # POC 文字标签 (只在 POC 不贴 TR 边界时画, 避免与 TR 线重叠)
        if abs(float(poc) - tr_top) > box * 1.5 and abs(float(poc) - tr_bottom) > box * 1.5:
            tr_pos = targets.get("tr_position%", 50)
            # TR 位<50%: POC 偏上方写; TR 位>50%: 偏下方写
            poc_y = float(poc) + (box * 1.2 if tr_pos < 50 else -box * 1.2)
            poc_anchor = (0, 0) if tr_pos < 50 else (0, 1)
            self._text(plot, c1, poc_y,
                       f"POC {float(poc):.2f} (价值中枢)", theme.C_AMBER,
                       anchor=poc_anchor, bold=True, delta=0,
                       fill=_fill, border=_border)

    def _draw_target_levels(self, plot, targets, n):
        tr_top = float(targets["tr_top"])
        tr_bottom = float(targets["tr_bottom"])
        direction = targets.get("direction", "range")
        c0 = float(targets.get("tr_start_col", 0))
        c1 = float(targets.get("tr_end_col", n))
        cend = n + 0.5
        box = self._box
        _, t_edge = self._zone_colors(targets)

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
                                          pen=_pen(theme.C_MUTED, 1.2)))
            for (yp, ang) in ((tr_top, -90), (tr_bottom, 90)):
                plot.addItem(pg.ArrowItem(
                    pos=(xc, yp), angle=ang, tailLen=0, headLen=8, headWidth=8,
                    pen=_pen(theme.C_MUTED), brush=pg.mkBrush(theme.C_MUTED)))

        # ── 三档目标绘制: 保守(粗线/高概率色)/中/激进(细线/淡) ──
        # 保守档: 从区间极值投影 (最易到达, 粗实线)
        # 中档: 从 POC 投影 (次易到达, 虚线)
        # 激进档: 从 count_line 投影 (最难到达, 细点线)
        float(targets.get("cause", 0))
        int(targets.get("columns", 0))
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
                col = theme.C_DOWN
            elif prob is not None and prob >= 0.5:
                col = theme.C_DOWN
            else:
                col = theme.C_DOWN
            # 根据概率调整透明度
            alpha_mod = 1.0 if prob is not None and prob >= 0.7 else (0.7 if prob is not None and prob >= 0.5 else 0.5)
            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a * alpha_mod)))
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
                    pen=_pen(theme.C_DOWN, 1.0, Qt.PenStyle.DotLine, a)))
                sp = targets.get("上方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                self._text(plot, label_x, float(near) - label_dy, lbl, theme.C_DOWN,
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
                col = theme.C_UP
            elif prob is not None and prob >= 0.5:
                col = theme.C_UP
            else:
                col = theme.C_UP
            alpha_mod = 1.0 if prob is not None and prob >= 0.7 else (0.7 if prob is not None and prob >= 0.5 else 0.5)
            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a * alpha_mod)))
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
                    pen=_pen(theme.C_UP, 1.0, Qt.PenStyle.DotLine, a)))
                sp = targets.get("下方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                self._text(plot, label_x, float(near) + label_dy, lbl, theme.C_UP,
                           anchor=(0, 0.5), delta=0, alpha=a,
                           fill=_fill, border=_border)

    # ── 视图 / 交互 ──
    def showEvent(self, ev):
        """首次真正显示时重定位到最新行情列。

        set_data 多在页签隐藏 (尺寸未定) 时调用, aspect 锁下初始视野按错误
        尺寸计算, 打开页签后会停在历史区间而非最新列。"""
        super().showEvent(ev)
        self._schedule_pending_view()

    def resizeEvent(self, ev):
        """布局/拖动分隔条改变尺寸时补应用待定初始视野 (aspect 锁在尺寸
        变化时会按旧跨度重新推导, 必须在最终尺寸上重设)。"""
        super().resizeEvent(ev)
        self._schedule_pending_view()

    def _schedule_pending_view(self):
        if not getattr(self, "_pending_view", False):
            return
        # 延迟到本轮布局完成后再尝试 (等宽高生效)
        QtCore.QTimer.singleShot(0, self._try_pending_view)

    def _try_pending_view(self):
        if not self._pending_view or not self._n:
            return
        if self._vb.width() > 1 and self._vb.height() > 1:
            self._apply_initial_view()
        # 几何仍未就绪: 保留标记, 等 resizeEvent 再试

    def _apply_initial_view(self):
        """定位到最新行情列 (最近 PNF_DEFAULT_COLS 列), 可见格子纵向居中。

        刷新保留视野时 (_keep_x 非空) 只恢复原 x 跨度, y 由 aspect 锁联动。
        aspect 锁下同时指定 x/y 或平移 targetRect 都会因跨度不一致被反向
        扩张 (实测: 重锁/translateBy 均会把 x 撑到全幅外)。因此先解锁,
        按当前控件几何显式算出与 x 跨度严格一致的 y 跨度
        (方格约束: y_span = x_span · box · H/W, 与 pyqtgraph 内部推导一致),
        一次性设好两轴后再上锁 —— 跨度已一致, 重锁不再缩放。控件尚未布局
        (宽高为 0) 时只设 x 并保留 pending 标记, 待 showEvent 重定位。"""
        self._pending_view = False
        n = self._n
        if not n:
            return
        fx0, fx1 = self._full_x
        keep = self._keep_x
        if keep is not None:
            self._keep_x = None
            x0, x1 = keep
        else:
            x0 = max(fx0, n - PNF_DEFAULT_COLS)
            x1 = fx1
        # 不可见 (页签未打开/最小化): viewbox 几何是陈旧值, 算出的 y 跨度必错,
        # 一律挂起, 待 showEvent 后按真实几何应用
        if not self.isVisible():
            self._vb.setAspectLocked(False)
            self._vb.setXRange(x0, x1, padding=0)
            self._pending_view = True
            return
        w_px = float(self._vb.width() or 0)
        h_px = float(self._vb.height() or 0)
        if w_px <= 1 or h_px <= 1:
            # 可见但尚未布局: 同样挂起等 resizeEvent
            self._pending_view = True
            return
        self._vb.setAspectLocked(False)
        if keep is not None:
            # 恢复原视野: x 已定, y 由 aspect 锁按比例联动即可
            self._vb.setXRange(x0, x1, padding=0)
            self._vb.setAspectLocked(True, ratio=self._box)
            x0v, x1v = self._vb.viewRange()[0]
            y0v, y1v = self._vb.viewRange()[1]
            self._push_view(x0v, x1v, y0v, y1v)
            self._snap_x_pixels()
            return
        i0 = max(0, int(x0))
        rows = [r for c in self._cols[i0:] for r in c["rows"]]
        fy0, fy1 = self._full_y
        span_x = max(1e-6, fx1 - x0)
        span_y = span_x * self._box * h_px / w_px
        if rows:
            mid = (min(rows) + max(rows)) / 2.0 * self._box
        else:
            mid = (fy0 + fy1) / 2.0
        cy = min(max(mid, fy0 + span_y / 2), max(fy0 + span_y / 2, fy1 - span_y / 2))
        self._vb.setXRange(x0, fx1, padding=0)
        self._vb.setYRange(cy - span_y / 2, cy + span_y / 2, padding=0)
        self._vb.setAspectLocked(True, ratio=self._box)
        x0v, x1v = self._vb.viewRange()[0]
        y0v, y1v = self._vb.viewRange()[1]
        self._push_view(x0v, x1v, y0v, y1v)

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

    def _pin(self, text, color, fy, bold=False, size=10, anchor=(0.5, 0.5),
             fx=0.5, ephemeral=True):
        """视口固定文字 (不随平移缩放移动; 可拖拽, 拖后位置按视口比例保留)。

        fx/fy: 0~1 视口横向/纵向锚点比例; anchor: 文字自身的对齐锚点。
        ephemeral=True 随 set_data 重建销毁; False 为常驻覆盖层
        (HUD / 回到最新按钮 / 帮助浮层)。"""
        ti = _DragTextItem(text, color=color, anchor=anchor)
        f = self._font(bold=bold)
        f.setPointSize(size)
        ti.setFont(f)
        ti._ephemeral = bool(ephemeral)
        ti._pin_anchor = tuple(anchor)
        ti._pin_fx = float(fx)
        ti._pin_fy = float(fy)
        ti.setParentItem(self._vb)
        ti.set_vp(float(fx), float(fy))
        self._pinned.append((ti, float(fy)))
        self._reposition_pinned()
        return ti

    def _reposition_pinned(self):
        if self._vb is None:
            return
        rect = self._vb.rect()
        for ti, fy in self._pinned:
            vx, vy = getattr(ti, "_vp",
                             (getattr(ti, "_pin_fx", 0.5), fy))
            ti.setPos(rect.width() * vx, rect.height() * vy)

    # ── 常驻覆盖层 (HUD / 回到最新按钮 / 帮助) ──
    def _build_overlays(self):
        if self._vb is None:
            return
        self._hud = self._pin("", theme.C_MUTED, 0.02, size=self._fs(-1),
                              anchor=(1, 0), fx=0.997, ephemeral=False)
        self._hud.hide()
        self._latest_btn = _LatestBtnItem(self)
        self._latest_btn._ephemeral = False
        self._latest_btn._pin_fx = 0.997
        self._latest_btn._pin_fy = 0.985
        f = self._font(bold=True)
        f.setPointSize(self._fs(0))
        self._latest_btn.setFont(f)
        self._latest_btn.setParentItem(self._vb)
        self._latest_btn.set_vp(0.997, 0.985)
        self._pinned.append((self._latest_btn, 0.985))

    def _update_hud(self):
        """右上角视野指示: 当前可见列范围 / 总列数 / 格值。"""
        if self._hud is None:
            return
        if not self._n or self._vb is None:
            self._hud.hide()
            return
        x0, x1 = self._vb.viewRange()[0]
        a = max(0, int(round(x0)))
        b = min(self._n - 1, int(round(x1)))
        self._hud.setText(f"列 {a}–{b} / {self._n} · 格值{self._box:.2f}")
        self._hud.show()

    def _update_latest_btn(self):
        """离开最新行情区 (右缘看不到最后一列) 时显示回到最新按钮。"""
        btn = self._latest_btn
        if btn is None:
            return
        if not self._n or self._vb is None:
            btn.hide()
            return
        _, x1 = self._vb.viewRange()[0]
        btn.setVisible(bool(x1 < self._n - 0.5))

    _HELP_LINES = (
        "滚轮 缩放   拖拽 平移   Shift+拖拽 框选放大   双击 复位全幅",
        "+/− 缩放   ←→ 十字逐列步进   ↑↓ 平移",
        "Shift+←→ 快速平移   PgUp/PgDn 大步平移   End 回到最新列",
        "Home/R 全幅   Backspace/F 视图历史   [ ] 格值缩放",
        "数字键 取书签   Shift+数字 存书签   ? 关闭本帮助",
    )

    def toggle_help(self):
        """? 键切换快捷键速查浮层 (视口固定; Esc 或再按 ? 关闭)。"""
        if self._help_item is not None:
            self._help_item.setVisible(not self._help_item.isVisible())
            return
        ti = _DragTextItem("\n".join(self._HELP_LINES), color=theme.C_TEXT,
                           anchor=(0.5, 0.5), border=_pen(theme.C_BORDER, 1.0),
                           fill=pg.mkBrush(theme.C_PANEL))
        f = self._font()
        f.setPointSize(self._fs(0))
        ti.setFont(f)
        ti._ephemeral = False
        ti._pin_anchor = (0.5, 0.5)
        ti._pin_fx = 0.5
        ti._pin_fy = 0.40
        ti.setParentItem(self._vb)
        ti.set_vp(0.5, 0.40)
        self._pinned.append((ti, 0.40))
        self._help_item = ti

    def _push_view(self, x0, x1, y0, y1):
        self._hist_push_key((float(x0), float(x1), float(y0), float(y1)))

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
        self._snap_x_pixels()

    def _step_crosshair(self, delta):
        """←/→ 逐列步进十字光标 (对齐K线图行为): 夹紧到实际列范围 +
        视口边缘跟随。返回 False 表示无十字光标可动, 调用方回退平移。"""
        ch = self._ch
        if ch is None or not self._n:
            return False
        nx = ch.move_by(delta)
        hi = self._n - 1
        if not (0 <= nx <= hi):
            nx = min(max(nx, 0), hi)
            ch.set_x(nx)
        self._ensure_x_visible(nx)
        return True

    def _ensure_x_visible(self, x, margin=1.5):
        """视口跟随: 光标越过可视范围边缘时按最小位移滚动 (K线图同款)。"""
        if self._n == 0 or self._vb is None:
            return
        x0, x1 = self._vb.viewRange()[0]
        y0, y1 = self._vb.viewRange()[1]
        span = x1 - x0
        if span <= 0:
            return
        m = min(float(margin), span * 0.25)
        fx0, fx1 = self._full_x
        if x < x0 + m:
            nx0 = max(fx0, x - m)
        elif x > x1 - m:
            nx1 = min(fx1, x + m)
            nx0 = nx1 - span
        else:
            return
        self.apply_view(nx0, nx0 + span, y0, y1)
        self._snap_x_pixels()

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
        self._snap_x_pixels()

    def nav_hist(self, step):
        if not self._hist:
            return
        pos = self._hist_pos + step
        if 0 <= pos < len(self._hist):
            self._hist_pos = pos
            x0, x1, y0, y1 = self._hist[pos]
            self._vb.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)

    def jump_to_latest(self):
        """回到最新行情列默认视野 (最近 N 列, End 键 / 右下角按钮)。"""
        if not self._n:
            return
        self._keep_x = None
        self._apply_initial_view()

    def box_zoom(self, x0, x1):
        """Shift+框选放大: 以框的 X 跨度为准, Y 由 aspect 锁联动。"""
        if self._n == 0:
            return
        if x0 > x1:
            x0, x1 = x1, x0
        fx0, fx1 = self._full_x
        x0, x1 = max(float(x0), fx0), min(float(x1), fx1)
        if x1 - x0 < 2:
            return
        self._vb.setRange(xRange=(x0, x1), padding=0)
        self._push_view(*self._vb.viewRange()[0], *self._vb.viewRange()[1])
        self._snap_x_pixels()

    def pan_y_by(self, frac):
        """纵向平移当前视野的 frac 比例 (↑/↓ 键; 负值向上)。"""
        if self._n == 0 or self._vb is None:
            return
        y0, y1 = self._vb.viewRange()[1]
        fy0, fy1 = self._full_y
        span = y1 - y0
        if span <= 0 or fy1 - fy0 <= 0:
            return
        d = span * float(frac)
        ny0 = min(max(y0 + d, fy0), max(fy0, fy1 - span))
        if abs(ny0 - y0) < 1e-12:
            return
        self._vb.setYRange(ny0, ny0 + span, padding=0)

    def save_bookmark(self, idx):
        """存视图书签 (Shift+数字键 1-9)。"""
        if self._n == 0 or self._vb is None:
            return
        xr, yr = self._vb.viewRange()
        self._bookmarks[int(idx)] = (float(xr[0]), float(xr[1]),
                                     float(yr[0]), float(yr[1]))

    def recall_bookmark(self, idx):
        """取视图书签 (数字键 1-9); 未存的号忽略。"""
        key = self._bookmarks.get(int(idx))
        if key is None or self._vb is None:
            return
        self._vb.setRange(xRange=key[:2], yRange=key[2:], padding=0)

    def _snap_x_pixels(self):
        """把列边界对齐到整数设备像素 (消除缩放/平移后的半格模糊)。

        aspect 锁下在 range-changed 回调里改范围会互相触发, 故延迟一拍执行,
        以 _snap_lock 防重入。"""
        if self._snap_lock or not self._n:
            return
        self._snap_lock = True
        QtCore.QTimer.singleShot(0, self._do_snap_x)

    def _do_snap_x(self):
        self._snap_lock = False
        vb = self._vb
        if vb is None or not self._n or not self.isVisible():
            return
        w = float(vb.width() or 0)
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0
        if w <= 1 or span <= 0:
            return
        sx_px = w / span
        want = (x0 + 0.5) * sx_px      # 第一条列边界线所在的像素位置
        delta_px = want - round(want)
        if abs(delta_px) < 0.2:        # 亚像素偏差不纠偏, 防抖动
            return
        delta = delta_px / sx_px
        vb.setRange(xRange=(x0 - delta, x1 - delta), padding=0)

    def keyPressEvent(self, ev):
        if self._n == 0:
            return super().keyPressEvent(ev)
        key = ev.key()
        mods = ev.modifiers()
        if key in (Qt.Key.Key_Question, Qt.Key.Key_Slash):
            self.toggle_help()
            ev.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._help_item is not None and self._help_item.isVisible():
                self._help_item.hide()
                ev.accept()
                return
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            idx = int(key) - int(Qt.Key.Key_1) + 1
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.save_bookmark(idx)
            else:
                self.recall_bookmark(idx)
            ev.accept()
            return
        if (mods & Qt.KeyboardModifier.AltModifier
                and key in (Qt.Key.Key_Left, Qt.Key.Key_Right)):
            if self._ch is not None:
                self._ch.move_by(1 if key == Qt.Key.Key_Right else -1)
            ev.accept()
            return
        x0, x1 = self._vb.viewRange()[0]
        y0, y1 = self._vb.viewRange()[1]
        ch = self._ch
        ax = ch._x if (ch is not None and ch._x is not None) else (x0 + x1) / 2
        ay = ch._y if (ch is not None and ch._y is not None) else (y0 + y1) / 2
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_about(ax, ay, 0.8)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_about(ax, ay, 1.25)
        elif key == Qt.Key.Key_Left:
            # 与K线图一致: ←/→ 逐列步进十字光标, Shift+←/→ 快速平移
            if (mods & Qt.KeyboardModifier.ShiftModifier) \
                    or not self._step_crosshair(-1):
                self.pan_by(-0.2)
        elif key == Qt.Key.Key_Right:
            if (mods & Qt.KeyboardModifier.ShiftModifier) \
                    or not self._step_crosshair(1):
                self.pan_by(0.2)
        elif key == Qt.Key.Key_Up:
            self.pan_y_by(-0.2)
        elif key == Qt.Key.Key_Down:
            self.pan_y_by(0.2)
        elif key == Qt.Key.Key_PageUp:
            self.pan_by(-0.6)
        elif key == Qt.Key.Key_PageDown:
            self.pan_by(0.6)
        elif key == Qt.Key.Key_End:
            self.jump_to_latest()
        elif key == Qt.Key.Key_BracketLeft:
            self.box_scale_requested.emit(0.8)
            ev.accept()
            return
        elif key == Qt.Key.Key_BracketRight:
            self.box_scale_requested.emit(1.25)
            ev.accept()
            return
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_R):
            self.reset_view()
        elif key == Qt.Key.Key_Backspace:
            self.nav_hist(-1)
        elif key in (Qt.Key.Key_F,):
            self.nav_hist(1)
        else:
            super().keyPressEvent(ev)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
