"""P&F 成交量渲染器 — 列级量柱 (底部) + 箱体量 VAP (右侧)。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui

from .. import theme


class PnfVolumeRenderer:
    """成交量渲染器: 底部列量柱 + 右侧 VAP。

    用法:
        renderer = PnfVolumeRenderer(cols, box, volumes)
        renderer.render(vol_plot, vap_plot)
    """

    def __init__(self, cols, box, volumes):
        self._cols = cols
        self._box = float(box)
        self._volumes = volumes or None

    def render(self, vol_plot, vap_plot):
        """绘制到指定的两个 PlotItem。"""
        if not self._volumes:
            if vol_plot is not None:
                vol_plot.setVisible(False)
            if vap_plot is not None:
                vap_plot.setVisible(False)
            return

        has_vol = (self._volumes.get("col_max", 0) > 0
                   and self._volumes.get("row_max", 0) > 0)

        if vol_plot is not None:
            vol_plot.setVisible(has_vol)
        if vap_plot is not None:
            vap_plot.setVisible(has_vol)

        if not has_vol or vol_plot is None or vap_plot is None:
            return

        n = len(self._cols)
        col_vols = self._volumes["col_vols"]
        col_max = self._volumes["col_max"]
        heights = [(v / col_max if col_max > 0 else 0.0) for v in col_vols]

        brushes = []
        for c in self._cols:
            base = pg.mkColor(theme.C_UP if c["type"] == "X" else theme.C_DOWN)
            base.setAlphaF(0.85)
            brushes.append(QtGui.QBrush(base))

        vol_plot.addItem(pg.BarGraphItem(
            x=list(range(n)), height=heights, width=0.88, brushes=brushes))

        row_max = self._volumes["row_max"]
        vap_plot.addItem(_VapBarsItem(self._volumes["row_vols"], self._box, row_max))
        vap_plot.getViewBox().setXRange(0, row_max, padding=0)

    def get_col_heights(self):
        """返回归一化列高度 (供视口 Y 自适应用)。"""
        if not self._volumes:
            return []
        col_vols = self._volumes["col_vols"]
        col_max = self._volumes["col_max"]
        return [(v / col_max if col_max > 0 else 0.0) for v in col_vols]

    def render_vap(self, vap_plot):
        """只渲染右侧箱体量 VAP 面板。"""
        if not self._volumes or vap_plot is None:
            return
        row_max = self._volumes["row_max"]
        vap_plot.addItem(_VapBarsItem(self._volumes["row_vols"], self._box, row_max))
        vap_plot.getViewBox().setXRange(0, row_max, padding=0)


class _VapBarsItem(pg.GraphicsObject):
    """箱体量 Volume-at-Price: 每个价格行一根横向量条。"""

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
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        for row, v in self._row_vols.items():
            if v <= 0:
                continue
            frac = v / row_max
            c = pg.mkColor(base)
            c.setAlphaF(0.15 + 0.85 * frac)
            p.setBrush(QtGui.QBrush(c))
            p.drawRect(QtCore.QRectF(0.0, row * box - box * 0.45,
                                     max(v, 1e-9), box * 0.9))
        p.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))

    def boundingRect(self):
        box = self._box
        if not self._row_vols:
            return QtCore.QRectF(0, 0, 1, 1)
        rows = list(self._row_vols)
        y0 = min(rows) * box - box
        y1 = max(rows) * box + box
        return QtCore.QRectF(0, y0, max(self._row_max, 1.0), y1 - y0)
