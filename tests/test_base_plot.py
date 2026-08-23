"""BasePlotWidget 通用图表基类测试。

覆盖需求点:
  1. 滚轮以光标为锚点缩放 — 缩放后光标下的数据点保持在光标下方;
  2. 键盘 上/+/放大、下/-缩小 (视图中心锚点), 左右箭头平移;
  3. 拖拽平移边界限制 (ViewBox.setLimits)、X 联动、双击复位、视图历史;
  4. SimplePlot 作为 pg.PlotWidget 替换品的兼容 API 与双轴锚点缩放。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

FULL_X = (0.0, 299.0)


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _Chart:
    """三面板测试图: p1/p2 日期联动, p3 独立 (无量程)。"""

    def __init__(self):
        import pyqtgraph as pg

        from desktop.base_plot import BasePlotWidget

        class Chart(BasePlotWidget):
            pass

        self.w = Chart()
        self.w.p1 = pg.PlotItem()
        self.w.p2 = pg.PlotItem()
        self.w.p3 = pg.PlotItem()
        for i, p in enumerate((self.w.p1, self.w.p2, self.w.p3)):
            self.w.ci.addItem(p, i, 0)
            p.plot([0, 100, 200, 299], [1, 2, 1.5, 3])
        self.w.register_plot(self.w.p1, full_x=FULL_X, sync=True,
                             primary=True)
        self.w.register_plot(self.w.p2, full_x=FULL_X, sync=True)
        self.w.register_plot(self.w.p3, sync=False)
        self.w._has_data = True
        self.w.p1.getViewBox().setXRange(*FULL_X, padding=0)


@pytest.fixture(scope="module")
def widget():
    try:
        app = _app()
        chart = _Chart()
    except Exception as e:  # 无 Qt/offscreen 环境
        pytest.skip(f"BasePlotWidget 不可用: {e}")
    w = chart.w
    w.resize(800, 900)
    w.show()
    app.processEvents()
    return app, w


def _key(key):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.Type.KeyPress, key,
                     Qt.KeyboardModifier.NoModifier)


def _wheel(view, pos):
    """构造滚轮向上 (angleDelta.y=120) 的 QWheelEvent, pos 为视口坐标 QPointF。"""
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    pos = QPointF(pos)
    gpos = view.viewport().mapToGlobal(QPoint(int(pos.x()), int(pos.y())))
    return QWheelEvent(pos, QPointF(gpos), QPoint(0, 0), QPoint(0, 120),
                       Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier,
                       Qt.ScrollPhase.NoScrollPhase, False)


def _dblclick(view, pos):
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    pos = QPointF(pos)
    gpos = view.viewport().mapToGlobal(QPoint(int(pos.x()), int(pos.y())))
    return QMouseEvent(QEvent.Type.MouseButtonDblClick,
                       pos, QPointF(gpos), Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


# ── 需求 1: 滚轮光标锚点缩放 ──
def test_zoom_anchor_keeps_data_under_cursor(widget):
    """锚点的相对位置 t 在缩放前后不变 → 数据点保持在光标下方。"""
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    cx = x0 + (x1 - x0) * 0.25
    w.zoom_about(cx, 0.8)
    nx0, nx1 = vb.viewRange()[0]
    t_before = (cx - x0) / (x1 - x0)
    t_after = (cx - nx0) / (nx1 - nx0)
    assert abs(t_before - t_after) < 1e-9, "锚点相对位置应保持不变"
    assert nx1 - nx0 == pytest.approx((x1 - x0) * 0.8)


def test_real_wheel_event_anchor(widget):
    """真实 QWheelEvent 路径: 场景点下的数据坐标在滚轮后保持不变。"""
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    app.processEvents()
    br = vb.sceneBoundingRect()
    sp = br.topLeft() + type(br.topLeft())(br.width() * 0.25,
                                           br.height() * 0.5)
    before = vb.mapSceneToView(sp).x()
    span_before = vb.viewRange()[0][1] - vb.viewRange()[0][0]
    ev = _wheel(w, w.mapFromScene(sp))
    w.wheelEvent(ev)
    app.processEvents()
    after = vb.mapSceneToView(sp).x()
    span_after = vb.viewRange()[0][1] - vb.viewRange()[0][0]
    assert span_after < span_before, "滚轮向上应放大"
    assert abs(after - before) < 0.5, "光标下的数据点应留在光标下"


def test_wheel_over_any_panel_targets_that_panel(widget):
    """悬停 p2 滚轮 → 缩放 p2 且联动到 p1; p3 不受影响。"""
    app, w = widget
    w.reset_view()
    app.processEvents()
    vb2 = w.p2.getViewBox()
    vb3 = w.p3.getViewBox()
    vb3.setXRange(1000, 1100, padding=0)
    br = vb2.sceneBoundingRect()
    sp = br.center()
    ev = _wheel(w, w.mapFromScene(sp))
    w.wheelEvent(ev)
    app.processEvents()
    r1 = w.p1.getViewBox().viewRange()[0]
    r2 = vb2.viewRange()[0]
    assert abs(r1[0] - r2[0]) < 1e-6 and abs(r1[1] - r2[1]) < 1e-6
    r3 = vb3.viewRange()[0]
    assert abs(r3[0] - 1000) < 1e-6, "未联动面板不应跟随"


# ── 需求 2: 键盘缩放 (中心锚点) 与左右箭头平移 ──
def test_keyboard_up_plus_zoom_in_center_anchor(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    app.processEvents()
    full_span = FULL_X[1] - FULL_X[0]
    c_before = sum(vb.viewRange()[0]) / 2
    w.keyPressEvent(_key(Qt.Key.Key_Up))
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 - x0 == pytest.approx(full_span * 0.8)
    assert sum((x0, x1)) / 2 == pytest.approx(c_before, abs=1e-6), \
        "键盘放大应以视图中心为锚点"
    old_span = x1 - x0
    w.keyPressEvent(_key(Qt.Key.Key_Plus))
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 - x0 == pytest.approx(old_span * 0.8)


def test_keyboard_down_minus_zoom_out(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    w.zoom_about(FULL_X[1] / 2, 0.5)
    app.processEvents()
    small = vb.viewRange()[0][1] - vb.viewRange()[0][0]
    w.keyPressEvent(_key(Qt.Key.Key_Down))
    app.processEvents()
    mid = vb.viewRange()[0][1] - vb.viewRange()[0][0]
    assert mid == pytest.approx(small / 0.5 * 0.5 * 1.25) or mid > small
    w.keyPressEvent(_key(Qt.Key.Key_Minus))
    app.processEvents()
    bigger = vb.viewRange()[0][1] - vb.viewRange()[0][0]
    assert bigger > mid


def test_keyboard_down_zoom_never_exceeds_full_range(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    w.zoom_about(FULL_X[1] / 2, 0.4)
    for _ in range(30):
        w.keyPressEvent(_key(Qt.Key.Key_Down))
        app.processEvents()
        x0, x1 = vb.viewRange()[0]
        assert x0 >= FULL_X[0] - 1e-9
        assert x1 <= FULL_X[1] + 1e-9


def test_arrow_keys_pan_and_clamp(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    w.zoom_about(150.0, 0.5)
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    span = x1 - x0
    w.keyPressEvent(_key(Qt.Key.Key_Right))
    app.processEvents()
    nx0, nx1 = vb.viewRange()[0]
    assert nx0 == pytest.approx(x0 + span * 0.2), "右箭头应右移 20% 跨度"
    w.keyPressEvent(_key(Qt.Key.Key_Left))
    app.processEvents()
    bx0, bx1 = vb.viewRange()[0]
    assert bx0 == pytest.approx(nx0 - span * 0.2)
    # 连续左移到底: 左边界不得越过全幅起点
    for _ in range(40):
        w.keyPressEvent(_key(Qt.Key.Key_Left))
    app.processEvents()
    ex0, ex1 = vb.viewRange()[0]
    assert ex0 >= FULL_X[0] - 1e-9, "平移应被夹紧在全幅内"
    assert ex1 - ex0 == pytest.approx(span)


# ── 边界限制 / 复位 / 历史 ──
def test_viewbox_limits_enforced(widget):
    """setLimits 写入后越界 setXRange 被 pyqtgraph 自动夹紧 (拖拽同源约束)。"""
    app, w = widget
    vb = w.p1.getViewBox()
    lim = vb.state["limits"]
    assert lim["xLimits"][0] == FULL_X[0]
    assert lim["xLimits"][1] == FULL_X[1]
    assert lim["xRange"][0] == 15.0
    vb.setXRange(500, 600, padding=0)
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 <= FULL_X[1] + 1e-6 and x0 >= FULL_X[0] - 1e-6


def test_home_resets_and_double_click_resets(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    w.zoom_about(150.0, 0.5)
    app.processEvents()
    w.keyPressEvent(_key(Qt.Key.Key_Home))
    app.processEvents()
    assert tuple(vb.viewRange()[0]) == pytest.approx(FULL_X)
    # 双击复位
    w.zoom_about(150.0, 0.5)
    app.processEvents()
    br = vb.sceneBoundingRect()
    ev = _dblclick(w, w.mapFromScene(br.center()))
    w.mouseDoubleClickEvent(ev)
    app.processEvents()
    assert tuple(vb.viewRange()[0]) == pytest.approx(FULL_X)


def test_view_history_nav(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    app.processEvents()
    w.zoom_about(150.0, 0.8)
    r1 = tuple(vb.viewRange()[0])
    w.zoom_about(150.0, 0.8)
    r2 = tuple(vb.viewRange()[0])
    w.keyPressEvent(_key(Qt.Key.Key_Backspace))
    app.processEvents()
    assert tuple(vb.viewRange()[0]) == pytest.approx(r1)
    w.keyPressEvent(_key(Qt.Key.Key_F))
    app.processEvents()
    assert tuple(vb.viewRange()[0]) == pytest.approx(r2)


def test_min_span_floor(widget):
    from PyQt6.QtCore import Qt
    app, w = widget
    vb = w.p1.getViewBox()
    w.reset_view()
    for _ in range(30):
        w.keyPressEvent(_key(Qt.Key.Key_Up))
    app.processEvents()
    x0, x1 = vb.viewRange()[0]
    assert x1 - x0 >= 15.0 - 1e-6, "跨度不应低于 MIN_SPAN_X"


# ── 十字光标托管 ──
def test_crosshair_lifecycle(widget):
    app, w = widget
    ch = w.attach_crosshair(w.p1, lambda v: f"{v:.0f}",
                            lambda v: f"{v:.2f}")
    assert ch in w._crosshairs
    assert ch.vline.scene() is not None
    w.detach_crosshairs()
    assert w._crosshairs == []
    assert ch.vline.scene() is None


# ── SimplePlot: pg.PlotWidget 替换品 ──
@pytest.fixture(scope="module")
def simple():
    try:
        app = _app()
        from desktop.base_plot import SimplePlot
    except Exception as e:
        pytest.skip(f"SimplePlot 不可用: {e}")
    w = SimplePlot(title="demo")
    w.resize(500, 300)
    w.show()
    app.processEvents()
    return app, w


def test_simple_plot_dropin_api(simple):
    import pyqtgraph as pg
    app, w = simple
    curve = w.plot([0, 1, 2], [1, 3, 2], pen=pg.mkPen("r"))
    assert curve in w.plot_item.items
    w.addItem(pg.TextItem("hi"))
    w.getAxis("left").setRange(0, 10)
    w.getAxis("bottom").setTicks([[(0, "a"), (1, "b")]])
    w.showGrid(x=True, y=True, alpha=0.3)
    w.setLabel("bottom", "x轴")
    w.setBackground("#101418")
    w.setTitle("t")
    w.setAspectLocked(lock=False)
    assert w.getViewBox() is w.plot_item.getViewBox()
    w.clear()
    app.processEvents()
    assert len([i for i in w.plot_item.items
                if isinstance(i, pg.PlotDataItem)]) == 0


def test_simple_plot_xy_anchored_wheel_and_keys(simple):
    from PyQt6.QtCore import Qt
    app, w = simple
    vb = w.getViewBox()
    vb.setRange(xRange=(0, 100), yRange=(0, 50), padding=0)
    app.processEvents()
    xr0, yr0 = vb.viewRange()
    cx, cy = (xr0[0] + xr0[1]) / 2, (yr0[0] + yr0[1]) / 2
    sp = vb.sceneBoundingRect().center()
    ev = _wheel(w, w.mapFromScene(sp))
    w.wheelEvent(ev)
    app.processEvents()
    xr1, yr1 = vb.viewRange()
    assert (xr1[1] - xr1[0]) < (xr0[1] - xr0[0]), "X 应随滚轮放大"
    assert (yr1[1] - yr1[0]) < (yr0[1] - yr0[0]), "Y 应随滚轮放大"
    # 事件坐标取整到像素, 中心保持用宽松容差
    assert (xr1[0] + xr1[1]) / 2 == pytest.approx(cx, abs=1.0)
    assert (yr1[0] + yr1[1]) / 2 == pytest.approx(cy, abs=1.0)
    w.keyPressEvent(_key(Qt.Key.Key_Down))
    app.processEvents()
    xr2, yr2 = vb.viewRange()
    assert (xr2[1] - xr2[0]) > (xr1[1] - xr1[0])
    assert (yr2[1] - yr2[0]) > (yr1[1] - yr1[0])


def test_simpleplot_set_full_x_bounds():
    try:
        app = _app()
        from desktop.base_plot import SimplePlot
    except Exception as e:
        pytest.skip(f"不可用: {e}")
    w = SimplePlot()
    w.set_full_x(w.plot_item, (30, 100))
    w.apply_view(0, 200)
    x0, x1 = w.getViewBox().viewRange()[0]
    assert x0 >= 30 and x1 <= 100
