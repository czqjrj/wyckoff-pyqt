"""PnfWidget (pyqtgraph) 离屏冒烟测试: 渲染 + 交互 (滚轮/键盘/复位/历史) + 导出。

GUI 依赖无法在无 Qt 环境的 CI 上运行, 失败时自动跳过而非报错。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest

from wyckoff.pnf import (
    build_pnf,
    build_pnf_data,
    pnf_history_targets,
    pnf_targets,
)


def _df(n=300):
    rng = np.random.default_rng(7)
    closes = 20 + np.cumsum(rng.normal(0, 0.4, n))
    closes = np.clip(closes, 8, None)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": closes * (1 + rng.normal(0, 0.003, n)),
        "close": closes * (1 + rng.normal(0, 0.003, n)),
        "high": closes * 1.02, "low": closes * 0.98,
        "volume": rng.uniform(1e5, 2e6, n),
    })


def _data(df):
    cols, box = build_pnf(df)
    return build_pnf_data(
        cols, box, "冒烟", targets=pnf_targets(df, cols, box),
        history=pnf_history_targets(cols, box), df=df)


def _app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def widget():
    try:
        app = _app()
        from desktop.pnf_widget import PnfWidget
    except Exception as e:  # 无 Qt/offscreen 环境
        pytest.skip(f"PnfWidget 不可用: {e}")
    df = _df()
    data = _data(df)
    w = PnfWidget(font_size=10)
    w.resize(900, 600)
    w.show()
    app.processEvents()
    w.set_data(**data)
    app.processEvents()
    return app, w, df


def test_pnf_widget_renders(widget):
    """默认视野聚焦最近若干列 (非全幅), reset_view 回到全幅, 有 X/O 格子与解读行。"""
    app, w, df = widget
    x0, x1 = w._vb.viewRange()[0]
    n = w._n
    full_span = w._full_x[1] - w._full_x[0]
    assert x1 >= n - 1, "视野右缘应含最后一列"
    assert x1 - x0 < full_span, "默认视野应为最近若干列 (非全幅)"
    assert x1 - x0 <= 120, "默认视野不应超过约100列"
    w.reset_view()
    app.processEvents()
    x0, x1 = w._vb.viewRange()[0]
    assert abs(x0 - w._full_x[0]) < 1e-6 and abs(x1 - w._full_x[1]) < 1e-6
    grid = [it for it in w.scene().items()
            if type(it).__name__ == "PnfGridItem"]
    assert grid, "应有 X/O 点数图格子"
    pinned = [it for it in w._pinned]
    assert pinned, "应有底部解读行"
    assert any("历史" in t.toPlainText() or "准确率" in t.toPlainText()
               for t, _fy in pinned), "应有历史命中统计行"


def test_pnf_widget_wheel_zoom(widget):
    """滚轮向上缩小跨度 (以光标为锚点)。"""
    app, w, df = widget
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    w.reset_view()
    app.processEvents()
    full = w._vb.viewRange()[0]
    view = w.scene().views()[0]
    gpos = view.viewport().mapToGlobal(QPoint(100, 100))
    ev = QWheelEvent(QPointF(100, 100), QPointF(gpos), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    app.processEvents()
    z = w._vb.viewRange()[0]
    assert z[1] - z[0] < full[1] - full[0], "滚轮向上应缩小跨度"


def test_pnf_widget_keyboard_reset(widget):
    """键盘 +/左右/Home 不抛异常, Home 复位到全幅。"""
    app, w, df = widget
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    def key(k):
        return QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier)

    w.reset_view()
    app.processEvents()
    full = w._vb.viewRange()[0]
    w.zoom_about((full[0] + full[1]) / 2, 0, 0.5)
    app.processEvents()
    z0 = w._vb.viewRange()[0]
    assert z0[1] - z0[0] < full[1] - full[0], "先缩放到非全幅"
    w.keyPressEvent(key(Qt.Key.Key_Left))
    app.processEvents()
    p = w._vb.viewRange()[0]
    assert abs(p[0] - z0[0]) > 1e-6, "左键应平移"
    w.keyPressEvent(key(Qt.Key.Key_Home))
    app.processEvents()
    r = w._vb.viewRange()[0]
    assert abs(r[0] - full[0]) < 1e-6 and abs(r[1] - full[1]) < 1e-6

    w.set_data(cols=None, title="占位")
    app.processEvents()
    assert w._n == 0 and w._pinned == []


def test_pnf_widget_export_png(widget, tmp_path):
    """grab_pixmap 可导出 PNG。"""
    app, w, df = widget
    pm = w.grab_pixmap()
    out = tmp_path / "pnf.png"
    assert pm.save(str(out))
    assert out.stat().st_size > 1000


def test_pnf_widget_volume_panels(widget):
    """带 df 传入时渲染列级成交量柱与箱体量 VAP 面板。"""
    app, w, df = widget
    # 共享模块级 widget 可能被前序测试重置为空, 此处重新喂入带量数据
    data = _data(df)
    w.set_data(**data)
    app.processEvents()
    assert w._volumes is not None
    assert w._volumes["col_max"] > 0 and w._volumes["row_max"] > 0
    assert len(w._volumes["col_vols"]) == w._n
    assert w._vol_plot.isVisible(), "列级成交量面板应可见"
    assert w._vap_plot.isVisible(), "箱体量 VAP 面板应可见"
    bars = [it for it in w._vol_plot.items
            if type(it).__name__ == "BarGraphItem"]
    assert bars, "底部面板应有列级成交量柱"
    vap = [it for it in w._vap_plot.items
           if type(it).__name__ == "_VapBarsItem"]
    assert vap, "右侧面板应有箱体量直方图"


def test_pnf_widget_no_volume_graceful(widget):
    """无成交量数据时 (build_pnf_data 未传 df) 不渲染量面板且不抛错。"""
    app, w, df = widget
    data = build_pnf_data(build_pnf(df)[0], build_pnf(df)[1], "无量")
    w.set_data(**data)
    app.processEvents()
    assert w._volumes is None
    assert not w._vol_plot.isVisible()
    assert not w._vap_plot.isVisible()


class _FakeDragEv:
    """模拟 pyqtgraph MouseDragEvent: pos/buttonDownPos 均为场景坐标。"""

    def __init__(self, pos, down, start=False, finish=False):
        from PyQt6.QtCore import Qt
        self._pos = pos
        self._down = down
        self._start = start
        self._finish = finish
        self._btn = Qt.MouseButton.LeftButton

    def button(self):
        return self._btn

    def isStart(self):
        return self._start

    def isFinish(self):
        return self._finish

    def accept(self):
        pass

    def ignore(self):
        pass

    def pos(self):
        return self._pos

    def buttonDownPos(self):
        return self._down


def _drag(ti, dx, dy):
    """对 _DragTextItem 模拟一次 (dx, dy) 像素的拖拽。"""
    sp0 = ti.scenePos()
    from PyQt6.QtCore import QPointF
    ti.mouseDragEvent(_FakeDragEv(sp0, sp0, start=True))
    ti.mouseDragEvent(_FakeDragEv(sp0 + QPointF(dx, dy), sp0, finish=True))


def test_pnf_widget_pinned_text_draggable(widget):
    """视口固定文字标注可拖拽, 拖后位置按视口比例保留 (resize 不归位)。"""
    app, w, df = widget
    data = _data(df)
    w.set_data(**data)
    app.processEvents()
    assert w._pinned
    ti, fy = w._pinned[0]
    p0 = ti.pos()
    _drag(ti, 60, -30)
    app.processEvents()
    moved = ti.pos()
    assert moved != p0, "拖拽后位置应改变"
    vx, vy = ti._vp
    assert abs(vx - p0.x() / w._vb.rect().width()) > 0.01, "_vp 水平比例应更新"
    w._reposition_pinned()
    assert ti.pos() == moved, "视口重排后应保留拖拽位置"


def test_pnf_widget_plot_label_draggable(widget):
    """数据锚定标注 (历史段/目标位文字) 可拖拽, 沿鼠标方向移动。"""
    app, w, df = widget
    data = _data(df)
    w.set_data(**data)
    app.processEvents()
    labels = [it for it in w.plot.items
              if type(it).__name__ == "_DragTextItem"]
    assert labels, "应有可拖拽的数据锚定标注"
    ti = labels[0]
    p0 = ti.pos()
    _drag(ti, 0, 20)
    app.processEvents()
    moved = ti.pos()
    assert moved != p0, "数据锚定标注拖拽后位置应改变"
    assert moved.y() < p0.y(), "向下拖 (scene +y) 对应数据 y 减小 (屏幕下移)"

