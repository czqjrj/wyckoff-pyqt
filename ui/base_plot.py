"""通用 pyqtgraph 图表基类 — 所有图表共用。

交互约定 (不改 pyqtgraph 库源码, 全部在控件层重写实现):
  - 滚轮      以光标位置为锚点平滑缩放 (120ms 缓动), 缩放后光标下的数据点
              保持在光标下方; 悬停哪个面板就缩放哪个面板 (未注册量程的
              面板回退到主面板); Ctrl+滚轮 在开启 y_zoom 的面板缩放价格轴
  - 键盘      上箭头 / + 放大, 下箭头 / - 缩小 (以十字光标位置为锚点);
              左右箭头逐根步进十字光标 (视口边缘自动跟随滚动),
              Shift+左右箭头按 20% 跨度快速平移;
              Home/R 复位全幅; Backspace/F 视图历史前进后退
  - 左键拖拽  平移 (保留 ViewBox 默认行为), 范围由 ViewBox.setLimits 约束
  - 双击      复位到全幅
  - 十字光标  通过 attach_crosshair 挂载 ui.crosshair.Crosshair

多面板图表 (K线三栏 / 指标八宫格) 用 register_plot 注册各面板并参与 X 联动;
单图场景直接用子类 SimplePlot 替换 pg.PlotWidget。
"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore
from pyqtgraph.Qt.QtCore import Qt

from . import theme
from .crosshair import Crosshair


def _event_pos(ev):
    """Qt5/Qt6 兼容: 取事件的视口坐标 QPointF。"""
    p = ev.position() if hasattr(ev, "position") else ev.pos()
    return QtCore.QPointF(p)


class HoverHighlightMixin:
    """面板悬停高亮: 鼠标所在面板显示强调色虚线描边 (提示滚轮/键盘作用面板)。

    子类在自建面板布局完成后 (即 _plots 已注册) 调用 self._hh_start() 一次;
    _build_plots 重建时悬停面板会自动隐藏 (panel 已从 scene 移除, _hit_plot
    返回 None), 无需额外清理。
    """

    def _hh_start(self):
        from PyQt6.QtWidgets import QGraphicsRectItem
        pen = pg.mkPen(theme.C_ACCENT, width=1.6)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._hh_rect = QGraphicsRectItem()
        self._hh_rect.setPen(pen)
        self._hh_rect.setZValue(40)      # 低于十字光标 (50), 高于数据项
        self._hh_rect.setVisible(False)
        self._hh_cur = None
        scene = self.scene()
        if scene is not None:
            scene.addItem(self._hh_rect)
            try:
                scene.sigMouseMoved.connect(self._hh_on_move)
            except (TypeError, RuntimeError):
                pass

    def _hh_on_move(self, scene_pos):
        rect = getattr(self, "_hh_rect", None)
        if rect is None:
            return
        if not self._has_data or not self._plots:
            self._hh_hide()
            return
        if not isinstance(scene_pos, QtCore.QPointF):
            scene_pos = QtCore.QPointF(float(scene_pos[0]), float(scene_pos[1]))
        hit = self._hit_plot(scene_pos)
        if hit is None or hit not in self._x_limits:
            self._hh_hide()
            return
        if hit is not self._hh_cur:
            self._hh_cur = hit
            vb = hit.getViewBox()
            if vb is None:
                self._hh_hide()
                return
            rect.setRect(vb.sceneBoundingRect())
            rect.setVisible(True)

    def _hh_hide(self):
        self._hh_cur = None
        rect = getattr(self, "_hh_rect", None)
        if rect is not None:
            rect.setVisible(False)


class ViewHistoryMixin:
    """视图历史栈 (Backspace/F 前进后退): 元组键去重 + 截断前推。

    BasePlotWidget 存 (x0,x1); PnfWidget 因 aspect 锁存 (x0,x1,y0,y1)。
    键的维度由各图表自定, 入栈/清空机制共用 —— 此前两处逐字重复。
    (注: PnfWidget 不整体并入 BasePlotWidget 是有意为之 —— 点数图需要
    XY 等比锁的方格语义, 与基类 X 中心的多面板联动模型不兼容; 仅共享
    无分歧的机制层。)
    """

    def _hist_init(self):
        self._hist = []
        self._hist_pos = -1

    def _hist_clear(self):
        self._hist = []
        self._hist_pos = -1

    def _hist_push_key(self, key) -> bool:
        """入栈 (与栈顶相同则跳过)。返回是否实际新增。"""
        if self._hist and self._hist[self._hist_pos] == key:
            return False
        self._hist = self._hist[:self._hist_pos + 1]
        self._hist.append(key)
        self._hist_pos = len(self._hist) - 1
        return True


class BasePlotWidget(ViewHistoryMixin, pg.GraphicsLayoutWidget):
    """多面板 pyqtgraph 图表基类。

    子类通过 register_plot 注册面板, set_full_x 声明 X 全幅范围;
    交互 (滚轮/键盘/拖拽边界/双击复位/视图历史) 由基类统一提供,
    子类只需覆盖 on_x_range_changed 做联动后的附加处理 (如价格 Y 自适应)。
    """

    # 可调参数 (类属性, 子类可按图表特性覆盖)
    ZOOM_IN = 0.8          # 放大系数 (<1): 滚轮向上 / 键盘放大
    ZOOM_OUT = 1.25        # 缩小系数 (>1): 滚轮向下 / 键盘缩小
    ANIM_MS = 120          # 滚轮缩放动画时长 (ms), 0 关闭动画
    MIN_SPAN_X = 15.0      # X 最小可见跨度 (数据单位)
    SPAN_EPS = 0.5         # 跨度与全幅之差小于该值时视为全幅
    MIN_APPLY_SPAN = 2.0   # apply_view 允许的最小跨度
    PAN_STEP = 0.2         # Shift+左右箭头快速平移量 (占当前跨度比例)

    def __init__(self, parent=None, background=None):
        super().__init__(parent)
        self.setBackground(pg.mkColor(background or theme.C_PANEL))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._plots = []       # 注册的 PlotItem (按序)
        self._synced = set()   # 参与 X 联动的 PlotItem
        self._x_limits = {}    # PlotItem -> (lo, hi) 全幅
        self._yzoom = {}       # PlotItem -> bool 是否允许缩放 Y
        self._crosshairs = []
        self._hist_init()
        self._has_data = False
        self._primary_plot = None
        self._sync_lock = False
        self._anims = []       # 进行中的缩放动画 [(anim, plot, target, push)]

    # ── 装配 ──
    def register_plot(self, plot, full_x=None, sync=True, y_zoom=False,
                      primary=False):
        """注册面板: 纳入滚轮命中/键盘作用范围; sync 决定是否跟随 X 联动;
        primary 指定键盘缩放/平移/历史的作用主面板 (默认首个注册者)。"""
        if plot in self._plots:
            return
        self._plots.append(plot)
        if primary or self._primary_plot is None:
            self._primary_plot = plot
        if sync:
            self._synced.add(plot)
        self._yzoom[plot] = bool(y_zoom)
        vb = plot.getViewBox()
        try:
            vb.sigXRangeChanged.disconnect(self._on_vb_x_changed)
        except (TypeError, RuntimeError):
            pass
        vb.sigXRangeChanged.connect(self._on_vb_x_changed)
        if full_x is not None:
            self.set_full_x(plot, full_x)

    def set_full_x(self, plot, span):
        """声明面板的 X 全幅范围, 并用 ViewBox.setLimits 约束拖拽/缩放边界。"""
        lo, hi = float(span[0]), float(span[1])
        if not hi > lo:
            hi = lo + 1.0
        self._x_limits[plot] = (lo, hi)
        plot.getViewBox().setLimits(xMin=lo, xMax=hi,
                                    minXRange=min(self.MIN_SPAN_X, hi - lo))

    def clear_plots(self):
        """重建面板前调用: 断开信号、清空注册/十字光标/视图历史。"""
        self._finish_anims()
        self.detach_crosshairs()
        for p in self._plots:
            try:
                p.getViewBox().sigXRangeChanged.disconnect(
                    self._on_vb_x_changed)
            except (TypeError, RuntimeError):
                pass
        self._plots = []
        self._synced = set()
        self._x_limits = {}
        self._yzoom = {}
        self._hist_clear()
        self._has_data = False
        self._primary_plot = None

    # ── 面板选择 ──
    def _primary(self):
        if self._primary_plot is not None:
            return self._primary_plot
        return self._plots[0] if self._plots else None

    def _hit_plot(self, scene_pos):
        """返回 scene_pos 落入的已注册面板; 无命中返回 None。"""
        for p in self._plots:
            if p.getViewBox().sceneBoundingRect().contains(scene_pos):
                return p
        return None

    def _target(self, scene_pos):
        """滚轮作用面板: 命中且已声明量程的面板优先, 否则主面板。"""
        hit = self._hit_plot(scene_pos)
        if hit is not None and hit in self._x_limits:
            return hit
        return self._primary()

    # ── 核心几何 ──
    def zoom_about(self, ax, factor, plot=None, ay=None, animate=False):
        """以数据坐标 ax 为锚点缩放 X (factor<1 放大)。

        锚点在旧视野中的相对位置 t 在新视野中保持不变, 因此缩放后
        锚点处的数据点仍停留在同一屏幕像素下。ay 不为 None 且面板
        开启 y_zoom 时同步缩放 Y。
        """
        plot = plot or self._primary()
        if plot is None or not self._has_data:
            return
        vb = plot.getViewBox()
        lim = self._x_limits.get(plot)
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0
        if span <= 0:
            return
        if lim:
            lo, hi = lim
            full_span = hi - lo
            new_span = min(max(span * factor, self.MIN_SPAN_X), full_span)
            if new_span >= full_span - self.SPAN_EPS:
                self.apply_view(lo, hi, plot=plot, animate=animate)
                return
            t = min(max((ax - x0) / span, 0.0), 1.0)
            nx0 = ax - new_span * t
            nx1 = nx0 + new_span
            if nx0 < lo:
                nx0, nx1 = lo, lo + new_span
            if nx1 > hi:
                nx1, nx0 = hi, hi - new_span
            self.apply_view(nx0, nx1, plot=plot, animate=animate)
        else:
            new_span = max(span * factor, self.MIN_SPAN_X)
            t = min(max((ax - x0) / span, 0.0), 1.0)
            nx0 = ax - new_span * t
            self.apply_view(nx0, nx0 + new_span, plot=plot, animate=animate)
        if ay is not None and self._yzoom.get(plot):
            y0, y1 = vb.viewRange()[1]
            yspan = y1 - y0
            if yspan > 0:
                new_ys = max(yspan * factor, 1e-12)
                ty = min(max((ay - y0) / yspan, 0.0), 1.0)
                ny0 = ay - new_ys * ty
                vb.setYRange(ny0, ny0 + new_ys, padding=0)

    def zoom_y_about(self, ay, factor, plot=None):
        """Ctrl+滚轮: 以数据坐标 ay 为锚点只缩放 Y (不改变 X 区间)。"""
        plot = plot or self._primary()
        if plot is None or not self._has_data:
            return
        vb = plot.getViewBox()
        y0, y1 = vb.viewRange()[1]
        yspan = y1 - y0
        if yspan <= 0:
            return
        new_ys = max(yspan * factor, 1e-12)
        t = min(max((ay - y0) / yspan, 0.0), 1.0)
        ny0 = ay - new_ys * t
        vb.setYRange(ny0, ny0 + new_ys, padding=0)

    def zoom_x_about(self, cx, factor):
        """兼容旧接口: 以数据坐标 cx 为锚点只缩放 X。"""
        self.zoom_about(cx, factor)

    def zoom_center(self, factor):
        """键盘缩放: 以十字光标位置为锚点; 无光标时回退到视图中心。"""
        plot = self._primary()
        if plot is None or not self._has_data:
            return
        vb = plot.getViewBox()
        xr, yr = vb.viewRange()
        ch = self._primary_crosshair()
        ax = ch._x if (ch is not None and ch._x is not None) else \
            (xr[0] + xr[1]) / 2.0
        ay = None
        if self._yzoom.get(plot):
            ay = ch._y if (ch is not None and ch._y is not None) else \
                (yr[0] + yr[1]) / 2.0
        self.zoom_about(ax, factor, plot=plot, ay=ay)

    def apply_view(self, x0, x1, push=True, plot=None, animate=False):
        """设置主面板 (或指定面板) 的 X 可见区间, 夹紧到全幅并记历史。

        animate=True 时经 120ms 缓动动画过渡到目标区间 (滚轮缩放用),
        历史只在动画结束时记录一次。"""
        plot = plot or self._primary()
        if plot is None:
            return None
        lim = self._x_limits.get(plot)
        if lim:
            x0 = max(float(x0), lim[0])
            x1 = min(float(x1), lim[1])
            if x1 - x0 < self.MIN_APPLY_SPAN:
                return None
        elif x1 - x0 <= 0:
            return None
        x0, x1 = float(x0), float(x1)
        if animate and self.ANIM_MS > 0:
            self._animate_view(plot, x0, x1, push=push)
        else:
            plot.getViewBox().setXRange(x0, x1, padding=0)
            if push:
                self._push_view(x0, x1)
        return (x0, x1)

    # ── 缩放动画 ──
    def _animate_view(self, plot, x0, x1, push=True):
        """从当前区间缓动过渡到 (x0, x1); 新动画到来时旧动画被顶替。"""
        vb = plot.getViewBox()
        start = vb.viewRange()[0]
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(int(self.ANIM_MS))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

        def _tick(t, start=tuple(start), target=(x0, x1)):
            cx0 = start[0] + (target[0] - start[0]) * t
            cx1 = start[1] + (target[1] - start[1]) * t
            vb.setXRange(cx0, cx1, padding=0)

        anim.valueChanged.connect(_tick)
        entry = [anim, plot, (float(x0), float(x1)), bool(push)]
        # 同面板旧动画作废 (新动画已从其中途状态出发), 停掉防止叠加驱动
        for e in list(self._anims):
            if e[1] is plot:
                try:
                    e[0].finished.disconnect()
                except (TypeError, RuntimeError):
                    pass
                e[0].stop()
                self._anims.remove(e)
        self._anims.append(entry)

        def _done():
            try:
                self._anims.remove(entry)
            except ValueError:
                pass

        anim.finished.connect(_done)
        anim.start()

    def _finish_anims(self):
        """立即完成所有进行中的缩放动画 (供测试同步断言 / 清理)。"""
        for anim, plot, target, push in list(self._anims):
            try:
                anim.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            anim.stop()
            try:
                plot.getViewBox().setXRange(target[0], target[1], padding=0)
            except RuntimeError:
                continue
            if push:
                self._push_view(*target)
        self._anims = []

    def reset_view(self):
        """全部已声明量程的面板复位到各自全幅 (历史只记主面板一次)。"""
        if not self._has_data:
            return
        first = True
        for p in self._plots:
            lim = self._x_limits.get(p)
            if not lim:
                continue
            self.apply_view(lim[0], lim[1], push=first, plot=p)
            first = False

    def pan_by(self, frac, plot=None):
        """按当前跨度的 frac 比例平移 (frac>0 向右), 夹紧到全幅。"""
        plot = plot or self._primary()
        if plot is None or not self._has_data:
            return
        vb = plot.getViewBox()
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0
        if span <= 0:
            return
        lim = self._x_limits.get(plot)
        if lim:
            lo, hi = lim
            full_span = hi - lo
            if span >= full_span - self.SPAN_EPS:
                self.apply_view(lo, hi, plot=plot)
                return
            nx0 = min(max(x0 + span * frac, lo), hi - span)
        else:
            nx0 = x0 + span * frac
        self.apply_view(nx0, nx0 + span, plot=plot)

    # ── 视图历史 ──
    def _push_view(self, x0, x1):
        self._hist_push_key((float(x0), float(x1)))

    def nav_hist(self, step):
        """Backspace(-1) 回退 / F(+1) 前进视图历史。"""
        if not self._hist or self._primary() is None:
            return
        pos = self._hist_pos + step
        if 0 <= pos < len(self._hist):
            self._hist_pos = pos
            x0, x1 = self._hist[pos]
            self._primary().getViewBox().setXRange(x0, x1, padding=0)

    # ── X 联动 ──
    def _on_vb_x_changed(self, vb, xrange):
        """任一已注册面板 X 变化 → 同步其余联动面板, 再触发子类钩子。

        setXRange 会同步再触发 sigXRangeChanged, 用 _sync_lock 防重入。
        """
        if self._sync_lock or not self._plots:
            return
        self._sync_lock = True
        try:
            x0, x1 = float(xrange[0]), float(xrange[1])
            for p in self._synced:
                pvb = p.getViewBox()
                if pvb is not vb:
                    pvb.setXRange(x0, x1, padding=0)
            self.on_x_range_changed(x0, x1, vb)
        finally:
            self._sync_lock = False

    def on_x_range_changed(self, x0, x1, source_vb=None):
        """联动后钩子: 子类覆盖 (如价格面板随可见区间重算 Y)。"""

    # ── 十字光标 ──
    def attach_crosshair(self, plot, fmt_x, fmt_y, font_size=8, snap=False,
                         on_move=None):
        """挂载十字光标到指定面板, 返回实例供调用方保存。

        snap=True 时 X 吸附整数柱位; on_move(idx) 在光标位置变化后回调
        (供 K线图 OHLC 信息条刷新)。"""
        ch = Crosshair(plot, fmt_x, fmt_y, font_size=font_size, snap=snap,
                       on_move=on_move)
        self._crosshairs.append(ch)
        return ch

    def detach_crosshairs(self):
        for ch in self._crosshairs:
            ch.detach()
        self._crosshairs = []

    def apply_theme(self):
        """主题切换时刷新十字光标颜色。"""
        for ch in self._crosshairs:
            try:
                ch.apply_theme()
            except Exception:
                pass

    def _primary_crosshair(self):
        """主面板对应的十字光标 (方向键步进的驱动者)。"""
        prim = self._primary()
        for ch in self._crosshairs:
            if ch.plot is prim:
                return ch
        return self._crosshairs[0] if self._crosshairs else None

    def step_crosshair(self, delta):
        """左右方向键: 十字光标移动 delta 个数据单位 (K线图=1根), 视口跟随。

        返回 True 表示光标已消费该按键; 无光标时返回 False (回退平移)。"""
        ch = self._primary_crosshair()
        if not self._has_data or ch is None:
            return False
        nx = ch.move_by(delta)
        plot = self._primary()
        lim = self._x_limits.get(plot) if plot else None
        if lim and not (lim[0] <= nx <= lim[1]):
            nx = min(max(nx, lim[0]), lim[1])
            for c in self._crosshairs:
                c.set_x(nx)
        self.ensure_x_visible(nx)
        return True

    def ensure_x_visible(self, x, margin=2.0):
        """视口跟随: 保证数据坐标 x 可见, 越过边缘时按最小位移滚动。"""
        plot = self._primary()
        if plot is None or not self._has_data:
            return
        vb = plot.getViewBox()
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0
        if span <= 0:
            return
        m = min(float(margin), span * 0.25)
        lo, hi = self._x_limits.get(plot) or (x0, x1)
        if x < x0 + m:
            nx0 = max(lo, x - m)
        elif x > x1 - m:
            nx1 = min(hi, x + m)
            nx0 = nx1 - span
        else:
            return
        nx1 = nx0 + span
        if hi - lo > span:
            if nx0 < lo:
                nx0, nx1 = lo, lo + span
            if nx1 > hi:
                nx1, nx0 = hi, hi - span
        self.apply_view(nx0, nx1)

    def clear_measure(self):
        """Escape 清除测量尺; 子类可覆盖。返回 True 表示已处理。"""
        return False

    # ── 事件重写 ──
    def wheelEvent(self, ev):
        """滚轮: 光标锚点平滑缩放; Ctrl+滚轮 在 y_zoom 面板缩放价格轴。

        事件在控件层消费, 不再下发 ViewBox 内置缩放。"""
        if self._has_data and self._plots:
            scene_pos = self.mapToScene(_event_pos(ev).toPoint())
            target = self._target(scene_pos)
            if target is not None:
                anchor = target.getViewBox().mapSceneToView(scene_pos)
                dy = ev.angleDelta().y() if hasattr(ev, "angleDelta") \
                    else ev.delta()
                factor = self.ZOOM_IN if dy > 0 else self.ZOOM_OUT
                ctrl = bool(ev.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
                if ctrl and self._yzoom.get(target):
                    self.zoom_y_about(anchor.y(), factor, plot=target)
                else:
                    self.zoom_about(anchor.x(), factor, plot=target,
                                    animate=True)
                ev.accept()
                return
        super().wheelEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        """双击面板空白处复位到全幅。"""
        sp = self.mapToScene(_event_pos(ev).toPoint())
        if self._has_data and self._hit_plot(sp) is not None:
            self.reset_view()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def keyPressEvent(self, ev):
        """键盘: 上/+ 放大, 下/- 缩小 (十字光标锚点); 左右逐根步进十字光标
        (Shift+左右按 20% 跨度快速平移); PageUp/PageDown 大步平移;
        Home 复位; Backspace/F 视图历史; Esc 清除测量尺。其余按键交还父类。"""
        if not self._has_data:
            return super().keyPressEvent(ev)
        key = ev.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_center(self.ZOOM_IN)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Minus,
                     Qt.Key.Key_Underscore):
            self.zoom_center(self.ZOOM_OUT)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            d = -1 if key == Qt.Key.Key_Left else 1
            shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift or not self.step_crosshair(d):
                self.pan_by(d * self.PAN_STEP)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            d = -1 if key == Qt.Key.Key_PageUp else 1
            self.pan_by(d * self.PAN_STEP * 5)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_R):
            self.reset_view()
        elif key == Qt.Key.Key_Backspace:
            self.nav_hist(-1)
        elif key in (Qt.Key.Key_F, Qt.Key.Key_End):
            self.nav_hist(1)
        elif key == Qt.Key.Key_Escape:
            self.clear_measure()
        else:
            return super().keyPressEvent(ev)
        ev.accept()


class SimplePlot(BasePlotWidget):
    """单面板图表基类 — pg.PlotWidget 的直接替换品。

    额外获得: 光标锚点滚轮缩放 (双轴)、中心锚点键盘缩放 (上/+/放大,
    下/-缩小)、左右平移、Home 复位、双击复位、视图历史。
    数据渲染 API 与 pg.PlotWidget 同名委托 (plot/addItem/clear/getAxis/…)。
    """

    def __init__(self, parent=None, title=None, background=None):
        super().__init__(parent, background=background)
        self.ci.setContentsMargins(0, 0, 0, 0)
        self.ci.setSpacing(0)
        self.plot_item = pg.PlotItem(title=title or None)
        self.ci.addItem(self.plot_item, 0, 0)
        # 单面板无联动; 开启 Y 缩放使滚轮/键盘对两轴生效 (与 PlotWidget 一致)
        self.register_plot(self.plot_item, sync=False, y_zoom=True)
        self._has_data = True
        # GraphicsLayoutWidget.__init__ 已把 ci 的 addItem/clear 绑为实例属性,
        # 会遮蔽下方类级委托; 与 pg.PlotWidget 一致, 重绑到 plot_item。
        self.addItem = self.plot_item.addItem      # noqa: A1 实例属性覆盖
        self.clear = self.plot_item.clear          # noqa: A1 实例属性覆盖

    # ── pg.PlotWidget 兼容委托 ──
    def getViewBox(self):
        return self.plot_item.getViewBox()

    def getAxis(self, axis):
        return self.plot_item.getAxis(axis)

    def plot(self, *args, **kwargs):
        return self.plot_item.plot(*args, **kwargs)

    def removeItem(self, *args, **kwargs):
        return self.plot_item.removeItem(*args, **kwargs)

    def setTitle(self, *args, **kwargs):
        return self.plot_item.setTitle(*args, **kwargs)

    def setLabel(self, *args, **kwargs):
        return self.plot_item.setLabel(*args, **kwargs)

    def showGrid(self, *args, **kwargs):
        return self.plot_item.showGrid(*args, **kwargs)

    def setAspectLocked(self, *args, **kwargs):
        return self.plot_item.setAspectLocked(*args, **kwargs)

    def addLegend(self, *args, **kwargs):
        return self.plot_item.addLegend(*args, **kwargs)

    def setBackground(self, color):  # noqa: N802 (Qt 命名)
        super().setBackground(color)
        plot_item = getattr(self, "plot_item", None)
        if plot_item is None:
            return  # GraphicsView.__init__ 期间 plot_item 尚未创建
        try:
            c = pg.mkColor(color)
        except (ValueError, TypeError):
            return  # 'default'/'auto' 等 pyqtgraph 特殊值不适用于 ViewBox
        plot_item.getViewBox().setBackgroundColor(c)
