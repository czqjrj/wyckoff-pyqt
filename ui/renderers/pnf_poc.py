"""P&F POC/价值区渲染器 (从目标渲染器中分离, 如需单独控制图层可见性)。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore

from .. import theme
from .pnf_grid import _brush_alpha, _DragTextItem, _pen


class PnfPOCRenderer:
    """POC 价值中枢渲染器 (可独立作为图层 "poc" 控制显隐)。"""

    def __init__(self, targets, cols, box, full_y):
        self._targets = targets or {}
        self._cols = cols
        self._box = box
        self._full_y = full_y

    def draw(self, plot):
        poc = self._targets.get("poc")
        vah = self._targets.get("vah")
        val_ = self._targets.get("val")
        if not (poc and vah and val_ and val_ < vah):
            return

        from pyqtgraph import LinearRegionItem
        vr = LinearRegionItem(
            [val_, vah], orientation='horizontal',
            brush=_brush_alpha(theme.C_AMBER, 0.12),
            pen=_pen(theme.C_AMBER, 0.8, QtCore.Qt.PenStyle.NoPen))
        plot.addItem(vr)

        plot.addItem(pg.InfiniteLine(
            pos=float(poc), angle=0,
            pen=_pen(theme.C_AMBER, 1.2, QtCore.Qt.PenStyle.DashLine, 0.9)))

        _fill, _border = self._chip()
        box = self._box
        tr_top = float(self._targets.get("tr_top", 0))
        tr_bottom = float(self._targets.get("tr_bottom", 0))
        c1 = float(self._targets.get("tr_end_col",
                           max(0, len(self._cols) - 1))) + 0.2

        if abs(float(poc) - tr_top) > box * 1.5 and abs(float(poc) - tr_bottom) > box * 1.5:
            tr_pos = self._targets.get("tr_position%", 50)
            poc_y = float(poc) + (box * 1.2 if tr_pos < 50 else -box * 1.2)
            poc_anchor = (0, 0) if tr_pos < 50 else (0, 1)
            ti = _DragTextItem(
                f"POC {float(poc):.2f} (价值中枢)", theme.C_AMBER,
                anchor=poc_anchor, bold=True, delta=0,
                fill=_fill, border=_border
            )
            ti.setPos(c1, poc_y)
            plot.addItem(ti)

    def _chip(self):
        return (pg.mkBrush(theme.C_PANEL), _pen(theme.C_BORDER, 1.0))
