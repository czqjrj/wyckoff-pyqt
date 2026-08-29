"""P&F 目标渲染器 — 绘制当前 TR/POC/威科夫计数目标线与三档标签。"""
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore

from .. import theme
from .pnf_grid import _brush_alpha, _DragTextItem, _pen


class PnfTargetsRenderer:
    """当前 TR 与目标渲染器。

    用法:
        renderer = PnfTargetsRenderer(targets, cols, box, full_y, layer_collector)
        renderer.draw(plot)
    """

    def __init__(self, targets, cols, box, full_y, layer_collector=None):
        """
        targets: 目标数据字典
        cols: 列数据
        box: 格值
        full_y: (y0, y1) 全幅 Y 范围
        layer_collector: 图层收集器函数
        """
        self._targets = targets or {}
        self._cols = cols
        self._box = box
        self._full_y = full_y
        self._collect = layer_collector

    def draw(self, plot):
        if not self._targets:
            return

        n = len(self._cols)
        if self._collect is None:
            def collect(layer, fn):
                fn()
        else:
            collect = self._collect

        # POC/价值区 (图层 "poc")
        collect("poc", lambda: self._draw_poc(plot))

        # TR/目标线 (图层 "targets")
        collect("targets", lambda: self._draw_target_levels(plot, n))

    def _draw_poc(self, plot):
        """POC 价值中枢 + 价值区色带。"""
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

    def _draw_target_levels(self, plot, n):
        tr_top = float(self._targets["tr_top"])
        tr_bottom = float(self._targets["tr_bottom"])
        direction = self._targets.get("direction", "range")
        c0 = float(self._targets.get("tr_start_col", 0))
        c1 = float(self._targets.get("tr_end_col", n))
        cend = n + 0.5
        box = self._box
        _, t_edge = self._zone_colors()

        # TR 上下沿 (虚线)
        plot.addItem(pg.InfiniteLine(
            pos=tr_top, angle=0,
            pen=_pen(t_edge, 1.2, QtCore.Qt.PenStyle.DashLine)))
        plot.addItem(pg.InfiniteLine(
            pos=tr_bottom, angle=0,
            pen=_pen(t_edge, 1.2, QtCore.Qt.PenStyle.DashLine)))

        # 计数起/止 双箭头
        _fill, _border = self._chip()
        for xc in (c0, c1):
            plot.addItem(pg.PlotCurveItem([xc, xc], [tr_bottom, tr_top],
                                          pen=_pen(theme.C_MUTED, 1.2)))
            for (yp, ang) in ((tr_top, -90), (tr_bottom, 90)):
                plot.addItem(pg.ArrowItem(
                    pos=(xc, yp), angle=ang, tailLen=0, headLen=8, headWidth=8,
                    pen=_pen(theme.C_MUTED), brush=pg.mkBrush(theme.C_MUTED)))

        # ── 三档目标绘制: 保守/中/激进 ──
        cause = float(self._targets.get("cause", 0))
        cols_count = int(self._targets.get("columns", 0))
        active_up = direction in ("up", "range")
        active_dn = direction in ("down", "range")

        self._draw_up_tiers(plot, tr_top, tr_bottom, cend, box, _fill, _border,
                            active_up, cause, cols_count)
        self._draw_dn_tiers(plot, tr_top, tr_bottom, cend, box, _fill, _border,
                            active_dn, cause, cols_count)

        # 近端参考目标
        self._draw_near_targets(plot, tr_top, tr_bottom, box, _fill, _border,
                                active_up, active_dn, direction)

    def _draw_up_tiers(self, plot, tr_top, tr_bottom, cend, box, fill, border,
                       active, cause, cols_count):
        up_tiers = [
            ("横向计数上方目标_保守", "上方概率_保守", "上方空间_保守%", 1.6, None, 0.95 if active else 0.4, "保"),
            ("横向计数上方目标_中",    "上方概率_中",    "上方空间_中%",    1.1, QtCore.Qt.PenStyle.DashLine, 0.80 if active else 0.35, "中"),
            ("横向计数上方目标",        "上方概率_激进",  "上方空间_激进%",  0.9, QtCore.Qt.PenStyle.DotLine,  0.65 if active else 0.3,  "激"),
        ]
        label_x = cend + 0.4
        label_dy = box * 1.6

        for idx, (tk, pk, sk, lw, style, a, label) in enumerate(up_tiers):
            if tk not in self._targets:
                continue
            t = float(self._targets[tk])
            prob = self._targets.get(pk)
            sp = self._targets.get(sk)

            if prob is not None and prob >= 0.7:
                col = theme.C_DOWN
            elif prob is not None and prob >= 0.5:
                col = theme.C_DOWN
            else:
                col = theme.C_DOWN
            alpha_mod = 1.0 if prob is not None and prob >= 0.7 else (0.7 if prob is not None and prob >= 0.5 else 0.5)

            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a)))

            if tk == "横向计数上方目标":
                plot.addItem(pg.PlotCurveItem(
                    [cend, cend], [tr_top, t], pen=_pen(col, 1.4, None, a)))
                plot.addItem(pg.ArrowItem(
                    pos=(cend, t), angle=-90, tailLen=0, headLen=9, headWidth=9,
                    pen=_pen(col, 1.0, None, a), brush=pg.mkBrush(col)))

            p_pct = f"{int(prob*100)}%" if prob is not None else ""
            sign = "+" if (sp or 0) > 0 else ""
            sp_s = f"{sign}{sp:.1f}%" if sp is not None else ""
            lbl = f"{label}{t:.2f}{sp_s}{p_pct}"

            direction = self._targets.get("direction", "range")
            y_off = idx * label_dy if direction == "up" else (idx - 1) * label_dy
            ti = _DragTextItem(
                lbl, col, anchor=(0, 0.5), bold=(idx == 0), delta=0, alpha=a,
                fill=fill, border=border
            )
            ti.setPos(label_x, t + y_off)
            plot.addItem(ti)

    def _draw_dn_tiers(self, plot, tr_top, tr_bottom, cend, box, fill, border,
                       active, cause, cols_count):
        dn_tiers = [
            ("横向计数下方目标_保守", "下方概率_保守", "下方空间_保守%", 1.6, None, 0.95 if active else 0.4, "保"),
            ("横向计数下方目标_中",    "下方概率_中",    "下方空间_中%",    1.1, QtCore.Qt.PenStyle.DashLine, 0.80 if active else 0.35, "中"),
            ("横向计数下方目标",        "下方概率_激进",  "下方空间_激进%",  0.9, QtCore.Qt.PenStyle.DotLine,  0.65 if active else 0.3,  "激"),
        ]
        label_x = cend + 0.4
        label_dy = box * 1.6

        for idx, (tk, pk, sk, lw, style, a, label) in enumerate(dn_tiers):
            if tk not in self._targets:
                continue
            t = float(self._targets[tk])
            prob = self._targets.get(pk)
            sp = self._targets.get(sk)

            if prob is not None and prob >= 0.7:
                col = theme.C_UP
            elif prob is not None and prob >= 0.5:
                col = theme.C_UP
            else:
                col = theme.C_UP
            alpha_mod = 1.0 if prob is not None and prob >= 0.7 else (0.7 if prob is not None and prob >= 0.5 else 0.5)

            plot.addItem(pg.InfiniteLine(pos=t, angle=0, pen=_pen(col, lw, style, a)))

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

            direction = self._targets.get("direction", "range")
            y_off = (1 - idx) * label_dy if direction == "down" else (idx - 1) * label_dy
            ti = _DragTextItem(
                lbl, col, anchor=(0, 0.5), bold=(idx == 0), delta=0, alpha=a,
                fill=fill, border=border
            )
            ti.setPos(label_x, t + y_off)
            plot.addItem(ti)

    def _draw_near_targets(self, plot, tr_top, tr_bottom, box, fill, border,
                           active_up, active_dn, direction):
        cend = len(self._cols) + 0.5
        label_dy = box * 1.6

        # 上涨近端
        near = self._targets.get("近端上方目标")
        if isinstance(near, (int, float)):
            far_up = self._targets.get("横向计数上方目标")
            if far_up is None or abs(float(near) - float(far_up)) > box:
                a = 0.85 if active_up else 0.4
                plot.addItem(pg.InfiniteLine(
                    pos=float(near), angle=0,
                    pen=_pen(theme.C_DOWN, 1.0, QtCore.Qt.PenStyle.DotLine, a)))
                sp = self._targets.get("上方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                ti = _DragTextItem(
                    lbl, theme.C_DOWN, anchor=(0, 0.5), delta=0, alpha=a,
                    fill=fill, border=border
                )
                ti.setPos(cend + 0.4, float(near) - label_dy)
                plot.addItem(ti)

        # 下跌近端
        near = self._targets.get("近端下方目标")
        if isinstance(near, (int, float)):
            far_dn = self._targets.get("横向计数下方目标")
            if far_dn is None or abs(float(near) - float(far_dn)) > box:
                a = 0.85 if active_dn else 0.4
                plot.addItem(pg.InfiniteLine(
                    pos=float(near), angle=0,
                    pen=_pen(theme.C_UP, 1.0, QtCore.Qt.PenStyle.DotLine, a)))
                sp = self._targets.get("下方空间_近端%")
                sign = "+" if (sp or 0) > 0 else ""
                lbl = f"近{float(near):.2f}{sign}{sp:.1f}%" if sp is not None else f"近端{float(near):.2f}"
                ti = _DragTextItem(
                    lbl, theme.C_UP, anchor=(0, 0.5), delta=0, alpha=a,
                    fill=fill, border=border
                )
                ti.setPos(cend + 0.4, float(near) + label_dy)
                plot.addItem(ti)

    def _zone_colors(self):
        direction = self._targets.get("direction", "range")
        tr_c0 = int(self._targets.get("tr_start_col", 0))
        tr_top = float(self._targets.get("tr_top", 0))
        tr_bottom = float(self._targets.get("tr_bottom", 0))
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

    def _chip(self):
        return (pg.mkBrush(theme.C_PANEL), _pen(theme.C_BORDER, 1.0))
