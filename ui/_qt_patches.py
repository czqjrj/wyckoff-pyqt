"""pyqtgraph/Qt 兼容垫片 (上游 bug 规避)。

pyqtgraph 0.14.0 的 `TextItem.paint` 在场景切换时无条件访问
`scene.sigPrepareForPaint` (仅 `pg.GraphicsScene` 的私有信号), 而 Qt 6 下的普通
`QGraphicsScene` 没有该信号 → `AttributeError`, 导致图表控件在场景切换/部件重建
时崩溃 (测试 `test_chart_widgets_render_offline` 复现)。ViewBox 内同类访问均带
`hasattr` 保护, 唯 TextItem 遗漏。

本垫片: 仅当 TextItem 落在无该信号的场景时, 给该场景挂一个 no-op 信号对象,
让上游 `paint` 原逻辑 (connect/disconnect/updateTransform) 原样跑通, 用完即删。
对真正的 `pg.GraphicsScene` 零影响。
"""
from __future__ import annotations


class _NoOpSignal:
    """与 Qt Signal 同接口的 no-op 占位 (connect/disconnect 均无害)。"""

    __slots__ = ()

    def connect(self, _cb):
        pass

    def disconnect(self, _cb):
        pass


_APPLIED = False


def _apply() -> bool:
    """对 pyqtgraph TextItem.apply 防护; 已打则跳过。成功返回 True。"""
    global _APPLIED
    if _APPLIED:
        return True
    try:
        from pyqtgraph.graphicsItems.TextItem import TextItem
    except Exception:  # noqa: BLE001 - pyqtgraph 未装/异常时不阻断
        return False
    if getattr(TextItem, "_wk_sigpatch", False):
        _APPLIED = True
        return True

    _orig_paint = TextItem.paint

    def _paint(self, p, *args):
        s = self.scene()
        if s is not None and not hasattr(type(s), "sigPrepareForPaint"):
            s.sigPrepareForPaint = _NoOpSignal()
            try:
                return _orig_paint(self, p, *args)
            finally:
                try:
                    del s.sigPrepareForPaint
                except (AttributeError, RuntimeError):
                    pass
        return _orig_paint(self, p, *args)

    TextItem.paint = _paint
    TextItem._wk_sigpatch = True
    _APPLIED = True
    return True