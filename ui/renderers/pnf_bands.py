"""P&F 区间底色带渲染器 — 绘制历史段与当前 TR 的垂直色带。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui


class PnfBandsRenderer:
    """点数图区间底色带渲染器。

    用法:
        renderer = PnfBandsRenderer(bands, y_range)
        plot.addItem(renderer.create_item())
    """

    def __init__(self, bands, y_range):
        """
        bands: [(x0, x1, color, alpha), ...]
        y_range: (y0, y1) 全幅 Y 范围
        """
        self._bands = bands
        self._yr = y_range

    def create_item(self):
        return _PnfBandsItem(self._bands, self._yr)


class _PnfBandsItem(pg.GraphicsObject):
    """内部 GraphicsObject。"""

    def __init__(self, bands, y_range):
        super().__init__()
        self._bands = bands
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
