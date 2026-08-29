"""P&F 历史段标注渲染器 — 绘制历史 TR 区间、目标线与命中标记。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui

from wyckoff.config import FONT_CANDIDATES

from .. import theme
from .pnf_grid import _DragTextItem, _pen


class PnfHistoryRenderer:
    """历史段渲染器。

    用法:
        renderer = PnfHistoryRenderer(history_data, cols, box, full_y, layer_collector)
        renderer.draw(plot)
    """

    def __init__(self, history, cols, box, full_y, layer_collector=None):
        """
        history: 历史段数据列表
        cols: 当前列数据 (用于获取列总数 n)
        box: 格值
        full_y: (y0, y1) 全幅 Y 范围
        layer_collector: 可选, 用于收集图层 item 的函数 collect(layer, fn)
        """
        self._history = history
        self._cols = cols
        self._box = box
        self._full_y = full_y
        self._collect = layer_collector

    def draw(self, plot):
        if not self._history:
            return
        n = len(self._cols)

        for h in self._history:
            self._draw_history(plot, h, n)

    def _draw_history(self, plot, h, n):
        top = float(h.get("tr_top", 0))
        bottom = float(h.get("tr_bottom", 0))
        c0 = float(h.get("tr_start_col", 0))
        c1 = float(h.get("tr_end_col", n))
        zone = h.get("zone", "")
        yhi = self._full_y[1]

        # TR 上下沿虚线
        for yv in (top, bottom):
            plot.addItem(pg.InfiniteLine(
                pos=yv, angle=0,
                pen=_pen(theme.C_MUTED, 0.8, QtCore.Qt.PenStyle.DotLine, 0.6)))

        _fill, _border = self._chip()

        # 段标签: 仅短标 (段号+区间), 详细语义在顶部信息条
        ti = _DragTextItem(
            f"段{int(h.get('seq', 0))} {bottom:.2f}~{top:.2f}",
            color=theme.C_UP if zone == "吸筹" else theme.C_DOWN,
            anchor=(0.5, 0), fill=_fill, border=_border
        )
        f = QtGui.QFont()
        f.setFamily(FONT_CANDIDATES[0])
        f.setPointSize(10)
        f.setBold(True)
        ti.setFont(f)
        ti.setPos((c0 + c1) / 2, top + (yhi - bottom) * 0.012)
        plot.addItem(ti)

        # 目标线与命中标记
        tx = c1 + 0.3
        if zone == "吸筹" and h.get("up_target") is not None:
            hit = bool(h.get("up_hit"))
            col = theme.C_DOWN if hit else theme.C_MUTED
            style = None if hit else QtCore.Qt.PenStyle.DotLine
            plot.addItem(pg.InfiniteLine(
                pos=float(h["up_target"]), angle=0,
                pen=_pen(col, 1.0, style, 0.95 if hit else 0.7)))
            ti = _DragTextItem(
                f"{'已到' if hit else '未到'} 上涨目标 {h['up_target']:.2f}", col,
                anchor=(0, 0.5), fill=_fill, border=_border
            )
            f = QtGui.QFont()
            f.setFamily(FONT_CANDIDATES[0])
            f.setPointSize(10)
            f.setBold(hit)
            ti.setFont(f)
            ti.setPos(tx, float(h["up_target"]))
            plot.addItem(ti)

        if zone == "派发" and h.get("down_target") is not None:
            hit = bool(h.get("down_hit"))
            col = theme.C_UP if hit else theme.C_MUTED
            style = None if hit else QtCore.Qt.PenStyle.DotLine
            plot.addItem(pg.InfiniteLine(
                pos=float(h["down_target"]), angle=0,
                pen=_pen(col, 1.0, style, 0.95 if hit else 0.7)))
            ti = _DragTextItem(
                f"{'已到' if hit else '未到'} 下跌目标 {h['down_target']:.2f}", col,
                anchor=(0, 0.5), fill=_fill, border=_border
            )
            f = QtGui.QFont()
            f.setFamily(FONT_CANDIDATES[0])
            f.setPointSize(10)
            f.setBold(hit)
            ti.setFont(f)
            ti.setPos(tx, float(h["down_target"]))
            plot.addItem(ti)

    def _chip(self):
        return (pg.mkBrush(theme.C_PANEL), _pen(theme.C_BORDER, 1.0))
