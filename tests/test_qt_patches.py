"""ui._qt_patches: pyqtgraph TextItem 场景信号兼容垫片回归测试。"""

import pytest

pytest.importorskip("PyQt6")


def test_patch_applied_on_ui_import():
    """导入 ui 包即完成 TextItem.paint 防护 (幂等)。"""
    from pyqtgraph.graphicsItems.TextItem import TextItem

    from ui import _apply_qt_patches

    assert getattr(TextItem, "_wk_sigpatch", False)
    for _ in range(2):
        assert _apply_qt_patches() is True  # 重复应用不打乱


def test_textitem_paint_on_plain_scene_no_crash():
    """TextItem 渲染落在无 sigPrepareForPaint 的普通 QGraphicsScene 时不抛异常。

    对应 `test_market_dashboard::test_chart_widgets_render_offline` 的复现:
    pyqtgraph 0.14.0 的 TextItem.paint 场景切换时无 hasattr 保护直连
    scene.sigPrepareForPaint, 普通 QGraphicsScene (无 view) 渲染会炸。
    """
    from PyQt6.QtWidgets import QApplication, QGraphicsScene

    from ui import _apply_qt_patches

    _apply_qt_patches()
    _ = QApplication.instance() or QApplication([])

    import pyqtgraph as pg

    scene = QGraphicsScene()  # 普通 scene, 无 view
    ti = pg.TextItem("待机", anchor=(0.5, 0.5))
    scene.addItem(ti)

    # 先用真实 pyqtgraph 场景让 _lastScene 绑定一次 (模拟部件重建后的遗留状态)
    from PyQt6.QtGui import QImage, QPainter

    img = QImage(200, 120, QImage.Format.Format_ARGB32)
    painter = QPainter(img)
    scene.render(painter)  # 普通 scene 渲染路径, 原版在此抛 AttributeError
    painter.end()
    assert ti.scene() is scene