"""pyqtgraph 资金透视控件 (替换原 matplotlib plot_market 资金透视图)。

数据由 AnalysisThread 在 worker 线程通过 chart.build_market_data() 收集 (文本/
标题/解读均在 worker 侧算好), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

排版与技术指标页 (IndWidget) 同一套布局体系:
  外层 IndScroll 容器: 宽度铺满可视区, 高度 = 宽度 × MKT_ASPECT, 竖向滚动
  → 面板长宽比例恒定, 不随窗口形状被压扁/拉伸。
  行结构 (行高按 MKT_ASPECT 总高确定性分配):
    标题行 + 估值卡行
    主力资金流向 (全宽单行, 高度为 2×2 网格行的 2 倍)
    供需强度 | 当前筹码堆积形态
    股东户数变化 | 资金分项
    底部综合文案 + 解读提示 (全宽)

交互 (继承自 BasePlotWidget):
  - 滚轮    以光标为锚点缩放 X 轴 (各面板 Y 为全量数据固定范围)
  - 左键拖拽 平移 (范围限制在数据全幅内)
  - 双击    复位到全幅
  - 键盘    上箭头 / + 放大, 下箭头 / - 缩放 (以视图中心为锚点),
            左/右箭头逐根步进十字光标 (视口边缘自动跟随),
            Shift+左右箭头按 20% 跨度快速平移,
            Home/r 复位, Backspace/f 视图历史
  - 十字光标 通过 attach_crosshair 挂载
"""
import datetime as _dt

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui
from pyqtgraph.Qt.QtCore import Qt, pyqtSignal

from wyckoff.config import FONT_CANDIDATES

from . import theme
from .base_plot import BasePlotWidget, HoverHighlightMixin
from .constants import MKT_ASPECT, MKT_DEFAULT_BARS


def _pen(color, width=1.0, style=None, alpha=1.0):
    pen = pg.mkPen(color, width=width)
    if style is not None:
        pen.setStyle(style)
    if alpha < 1.0:
        c = pen.color()
        c.setAlphaF(float(alpha))
        pen.setColor(c)
    return pen


def _brush_alpha(color, alpha):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    return pg.mkBrush(c)


def _parse_day(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    return None


class _DateAxis(pg.AxisItem):
    """X 轴日期刻度: 按整数柱索引回查日期渲染标签。"""

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._days = []

    def set_days(self, days):
        self._days = list(days)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._days):
                d = self._days[i]
                if d is None:
                    out.append("")
                else:
                    out.append(d.strftime("%y-%m-%d"))
            else:
                out.append("")
        return out


class MktWidget(HoverHighlightMixin, BasePlotWidget):
    """pyqtgraph 资金透视: 标题 + 估值卡 + 主力资金流向全宽 + 4面板 2×2 + 底部总结。

    继承 BasePlotWidget 获得统一交互 (滚轮/键盘/拖拽边界/双击复位/视图历史/十字光标),
    HoverHighlightMixin 提供面板悬停重点描边。日期面板默认视野聚焦最近
    MKT_DEFAULT_BARS 根 (设置 Chart.MKT_DEFAULT_BARS 覆盖, 0=全幅)。
    """

    # 资金透视不需要 Y 轴缩放 (各面板 Y 固定全幅范围)
    ZOOM_IN = 0.8
    ZOOM_OUT = 1.25
    ANIM_MS = 0  # 资金透视原版无缩放动画, 关闭以保证即时响应
    MIN_SPAN_X = 3.0
    SPAN_EPS = 0.5
    MIN_APPLY_SPAN = 1.0
    PAN_STEP = 0.1

    # 默认视野柱数 (0=全幅); 外部按设置覆盖
    default_bars = MKT_DEFAULT_BARS

    crosshair_moved = pyqtSignal(str, float, float)
    status_message = pyqtSignal(str)

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent, background=theme.C_PANEL)
        self._font_size = int(font_size)
        self._n = 0
        self._date_axes = {}
        self.plots = {}
        self._empty = True
        self._crosshairs = []
        self._focused = None
        self._crosshair_values = {}

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 高度由外层 IndScroll 按 MKT_ASPECT×宽度决定 (与技术指标页一致),
        # 此处只保留最小可读下限。
        self.setMinimumSize(700, 500)
        self._build_plots(empty=True)
        self._hh_start()

    # ── 布局 ──
    def _build_plots(self, empty=False):
        self.clear_plots()
        self._hh_hide()
        self._focused = None
        self.ci.clear()
        self.ci.setContentsMargins(18, 14, 18, 14)
        self.ci.setSpacing(12)
        self.plots = {}
        self._date_axes = {}
        self._full_x = {}

        # 顶部估值卡行 (横幅 + 估值合并一行, 靠左对齐图表)
        head = self.ci.addLabel("", row=0, col=0, colspan=2, justify="left",
                                size=f"{self._fs(1)}pt")
        self.header_label = head

        # 主力资金流向单独一行全宽 + 其余 4 面板 2×2
        self._add_plot("main_flow", "主力资金流向", 1, 0, colspan=2)
        self._add_plot("sub_flow", "资金分项", 3, 1)
        self._add_plot("chips", "当前筹码堆积形态", 2, 1)
        self._add_plot("holders", "股东户数变化", 3, 0)
        self._add_plot("sd", "供需强度", 2, 0)

        cap = self.ci.addLabel("", row=4, col=0, colspan=2, justify="center",
                               size=f"{self._fs(1)}pt", bold=True)
        self.cap_label = cap
        ins = self.ci.addLabel("", row=5, col=0, colspan=2, justify="center",
                               size=f"{self._fs(0)}pt")
        self.insights_label = ins

        if empty:
            self._n = 0
            self._show_empty(True)

    # ── 行高分配 ──
    # 与技术指标页同一确定性口径: 整图总高 = 宽度 × MKT_ASPECT (外层 IndScroll
    # 保证), 行高按该总高显式设定 —— 与控件自身 height() 解耦, 窗口拉高不再
    # 拉伸图表面板 (多出的空间交给外层滚动条), 面板长宽比恒定不漂移。
    _TEXT_PAD = {"header_label": 14, "cap_label": 20, "insights_label": 16}
    # 主力资金流向与其他图表面板等高 (原为 2 倍, 按需求调整为 1/2)
    _PLOT_WEIGHTS = {1: 1.0, 2: 1.0, 3: 1.0}   # row → 权重
    _MIN_PLOT_ROW_H = 110.0                     # 窗口过小时面板的最低可读高度

    def _apply_row_heights(self):
        """按 MKT_ASPECT×宽度 的总高分配行高 (文本按内容, 图表按 2:1:1)。"""
        lay = self.ci.layout
        _, tm, _, bm = self.ci.getContentsMargins()
        spacing = 12
        total = float(self.width()) * MKT_ASPECT
        usable = max(total - tm - bm - spacing * 5,
                     4 * self._MIN_PLOT_ROW_H)

        min_plot_h = max(self._MIN_PLOT_ROW_H, total * 0.07)
        text_h = 0.0
        for attr, pad in self._TEXT_PAD.items():
            lab = getattr(self, attr, None)
            need = 30.0
            if lab is not None and getattr(lab, "item", None) is not None:
                need = float(lab.item.boundingRect().height())
            h = max(need + pad, 24.0)
            lay.setRowFixedHeight({"header_label": 0, "cap_label": 4,
                                   "insights_label": 5}[attr], h)
            text_h += h

        plot_space = max(usable - text_h, 4 * min_plot_h)
        w_sum = sum(self._PLOT_WEIGHTS.values())
        for row, w in self._PLOT_WEIGHTS.items():
            lay.setRowFixedHeight(
                row, max(plot_space * w / w_sum, min_plot_h))

    def _apply_focus_row_heights(self):
        """单面板放大态的行高: 文本行按内容, 图表面板独占其余高度。"""
        lay = self.ci.layout
        _, tm, _, bm = self.ci.getContentsMargins()
        spacing = 12
        total = float(self.width()) * MKT_ASPECT
        usable = max(total - tm - bm - spacing * 3, self._MIN_PLOT_ROW_H)

        text_h = 0.0
        for attr, pad, row in (("header_label", 10, 0),
                               ("cap_label", 20, 2),
                               ("insights_label", 16, 3)):
            lab = getattr(self, attr, None)
            need = 24.0
            if lab is not None and getattr(lab, "item", None) is not None:
                need = float(lab.item.boundingRect().height())
            h = max(need + pad, 24.0)
            lay.setRowFixedHeight(row, h)
            text_h += h
        lay.setRowFixedHeight(1, max(usable - text_h,
                                     self._MIN_PLOT_ROW_H))

    def _apply_current_row_heights(self):
        """按当前视图状态分配行高 (网格 / 单面板放大)。"""
        if getattr(self, "_focused", None):
            self._apply_focus_row_heights()
        else:
            self._apply_row_heights()

    def showEvent(self, ev):
        super().showEvent(ev)
        # 标签页切换显示时尺寸可能未变 (无 resizeEvent), 补一次行高应用
        if hasattr(self, "cap_label"):
            self._wrap_bottom_labels()
            self._apply_current_row_heights()

    def _add_plot(self, key, title, row, col, colspan=1, stretch=13):
        is_date = key != "chips"
        axis = _DateAxis("bottom") if is_date else pg.AxisItem("bottom")
        self._date_axes[key] = axis
        plot = pg.PlotItem(axisItems={"bottom": axis})
        # 标题样式与技术指标页一致 (C_TEXT + fs(2), 非加粗)
        plot.setTitle(title, color=theme.C_TEXT, size=f"{self._fs(2)}pt")
        plot.hideButtons()
        plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.getViewBox().setMouseEnabled(x=True, y=False)
        for ax_name in ("left", "bottom"):
            ax = plot.getAxis(ax_name)
            ax.setPen(_pen(theme.C["axis"], 1))
            ax.setTextPen(_pen(theme.C_TEXT, 1))
        self.ci.addItem(plot, row, col, colspan=colspan)
        self.plots[key] = plot

        # 注册到 BasePlotWidget: 各面板独立 X (不同于 K线/指标联动), 不缩放 Y
        self.register_plot(plot, sync=False, y_zoom=False)

        # 声明初始全幅 (稍后在 _date_plot 中更新)
        self.set_full_x(plot, (0.0, 1.0))

        # 添加十字光标
        self._attach_crosshair_for(key)
        return row

    def _attach_crosshair_for(self, key):
        """为指定面板挂载十字光标 (延迟绑定 fmt_x/fmt_y, 在 set_data 时更新)。"""
        plot = self.plots[key]

        def fmt_x(i, k=key):
            if k == "chips":
                return f"{i:.3f}"
            if k in self._date_axes:
                idx = int(round(i))
                days = getattr(self._date_axes[k], '_days', [])
                if 0 <= idx < len(days) and days[idx] is not None:
                    return days[idx].strftime("%Y-%m-%d")
            return ""

        def fmt_y(v, k=key):
            if k in ("main_flow", "sub_flow"):
                return f"{v / 1e8:+.2f}亿"
            if k == "chips":
                return f"{v:.2f}"
            if k == "holders":
                return f"{v:,.0f}"
            if k == "sd":
                return f"{v:,.0f}"
            return f"{v:.2f}"

        # attach_crosshair 已追加到 self._crosshairs, 无需重复 append;
        # 日期面板吸附整数柱位 (读数对齐柱心), chips(分布权重) 不吸附
        snap = key != "chips"
        ch = self.attach_crosshair(plot, fmt_x, fmt_y, font_size=self._fs(0),
                                   snap=snap)
        ch._panel_key = key
        ch.sigPositionChanged.connect(
            lambda ch=ch: self._on_crosshair_moved(ch))

    def _show_empty(self, flag=True):
        self._empty = bool(flag)
        self._has_data = not flag
        for key, plot in self.plots.items():
            plot.setTitle("", color=theme.C_MUTED, size=f"{self._fs(1)}pt")
            plot.clear()
        if flag:
            self.header_label.setText("")
            self.cap_label.setText("")
            self.insights_label.setText("")

    def _wrap_bottom_labels(self):
        """限制文案行文本宽度, 超宽自动换行, 避免横向溢出画布。"""
        w = max(self.width() - 64, 280)
        for lab in (self.header_label, self.cap_label, self.insights_label):
            if lab is not None and lab.item is not None:
                lab.item.setTextWidth(float(w))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "cap_label"):
            self._wrap_bottom_labels()
            self._apply_current_row_heights()

    # ── 主题 ──
    def apply_theme(self):
        """主题切换时刷新控件/视口/坐标轴配色。"""
        self.setBackground(pg.mkColor(theme.C_PANEL))
        for plot in self.plots.values():
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                if axis is not None:
                    axis.setPen(_pen(theme.C["axis"], 1))
                    axis.setTextPen(_pen(theme.C_TEXT, 1))

    # ── 数据入口 ──
    def set_data(self, data=None, **extra):
        if isinstance(data, dict):
            d = data
        elif extra:
            d = extra
        else:
            d = None

        self.detach_crosshairs()
        self._build_plots(empty=True)
        if not d:
            self._show_empty(True)
            return

        self._empty = False
        self._has_data = True

        self.header_label.setText(
            self._render_header_html(d), size=f"{self._fs(1)}pt")

        self._draw_main_flow(d.get("main_flow"))
        self._draw_sub_flow(d.get("sub_flow"))
        self._draw_chips(d.get("chips"))
        self._draw_holders(d.get("holders"))
        self._draw_sd(d.get("sd"))

        self.cap_label.setText(
            self._render_caps_html(d), size=f"{self._fs(1)}pt")
        self.insights_label.setText(
            self._render_insights_html(d), size=f"{self._fs(0)}pt")
        self._wrap_bottom_labels()

        self._set_panel_title("main_flow", (d.get("main_flow") or {}).get("title"))
        self._set_panel_title("sub_flow", (d.get("sub_flow") or {}).get("title"))
        self._set_panel_title("chips", (d.get("chips") or {}).get("title"))
        self._set_panel_title("holders", (d.get("holders") or {}).get("title"))
        self._set_panel_title("sd", (d.get("sd") or {}).get("title"))

        chips = d.get("chips") or {}
        if chips.get("shape_txt"):
            self._set_chips_shape(chips["shape_txt"], chips["shape_color"])

        # 更新十字光标的 fmt_x (绑定最新日期)
        self._update_crosshair_fmt_x(d)

        self._relayout()
        # 默认视野: 日期面板聚焦最近 N 根 (0=全幅), Home/双击看全幅
        self.apply_default_view()

    def set_placeholder(self, msg):
        """显示占位文案 (分析中/失败), 清空面板数据。"""
        self._build_plots(empty=True)
        if msg:
            self.header_label.setText(msg)
            self.cap_label.setText("")
            self.insights_label.setText("")
        self._relayout()

    def apply_default_view(self):
        """重新应用默认视野: default_bars>0 时日期面板聚焦最近 N 根, 否则全幅。

        chips 面板 X 轴是分布权重 (非日期), 不做聚焦。
        """
        n_bars = int(getattr(self, "default_bars", MKT_DEFAULT_BARS) or 0)
        for key in ("main_flow", "sub_flow", "holders", "sd"):
            plot = self.plots.get(key)
            full = self._full_x.get(key)
            if plot is None or full is None:
                continue
            if n_bars > 0 and (full[1] - full[0]) + 1 > n_bars:
                self.apply_view(full[1] - (n_bars - 1), full[1],
                                plot=plot, push=False)
            else:
                self.apply_view(full[0], full[1], plot=plot, push=False)

    # ── 面板化独立控制: 单个面板放大 / 恢复网格 ──
    def focus_panel(self, key):
        """把指定面板放大为单面板视图 (其余面板隐藏, 交互仍完整)。"""
        if key not in self.plots:
            return
        if self._focused == key:
            return
        self._focused = key
        self._apply_panel_layout()
        self._apply_focus_row_heights()
        self.reset_view()
        # 添加聚焦高亮
        self._add_focus_highlight(self.plots[key])

    def show_grid(self):
        """从单面板放大态恢复完整布局。"""
        if self._focused is None:
            return
        self._focused = None
        self._apply_panel_layout()
        self._apply_row_heights()
        self.apply_default_view()
        # 移除聚焦高亮
        self._remove_focus_highlight()

    def _remove_focus_highlight(self):
        """移除聚焦面板的高亮边框。"""
        if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
            try:
                self._focus_highlight.scene().removeItem(self._focus_highlight)
            except Exception:
                pass
            self._focus_highlight = None

    def is_focused(self):
        """是否处于单面板放大态; 返回当前面板 key 或 None。"""
        return self._focused

    @property
    def focused_panel(self):
        return self._focused

    def _add_focus_highlight(self, plot):
        """为聚焦面板添加高亮边框。"""
        from PyQt6.QtWidgets import QGraphicsRectItem
        if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
            try:
                self._focus_highlight.scene().removeItem(self._focus_highlight)
            except Exception:
                pass
        pen = pg.mkPen(theme.C_ACCENT, width=2.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._focus_highlight = QGraphicsRectItem()
        self._focus_highlight.setPen(pen)
        self._focus_highlight.setZValue(30)
        vb = plot.getViewBox()
        self._focus_highlight.setRect(vb.sceneBoundingRect())
        self.scene().addItem(self._focus_highlight)
        # 监听视图变化更新高亮框
        def _update_highlight():
            if hasattr(self, '_focus_highlight') and self._focus_highlight is not None:
                self._focus_highlight.setRect(vb.sceneBoundingRect())
        vb.sigXRangeChanged.connect(_update_highlight)
        vb.sigYRangeChanged.connect(_update_highlight)

    def _apply_panel_layout(self):
        """按 _focused 重置 ci 布局: None=完整布局, key=单面板放大。

        pyqtgraph GraphicsLayout.removeItem/addItem 支持摘挂 PlotItem 与
        LabelItem, 面板数据/坐标轴/十字光标随之保留。
        """
        self.ci.clear()
        self.ci.setContentsMargins(18, 14, 18, 14)
        self.ci.setSpacing(12)
        focused = self._focused
        if focused and focused not in self.plots:
            focused = self._focused = None

        if focused:
            self.ci.addItem(self.header_label, 0, 0, colspan=2)
            self.ci.addItem(self.plots[focused], 1, 0, colspan=2)
            self.ci.addItem(self.cap_label, 2, 0, colspan=2)
            self.ci.addItem(self.insights_label, 3, 0, colspan=2)
        else:
            self.ci.addItem(self.header_label, 0, 0, colspan=2)
            self.ci.addItem(self.plots["main_flow"], 1, 0, colspan=2)
            self.ci.addItem(self.plots["sub_flow"], 3, 1)
            self.ci.addItem(self.plots["chips"], 2, 1)
            self.ci.addItem(self.plots["holders"], 3, 0)
            self.ci.addItem(self.plots["sd"], 2, 0)
            self.ci.addItem(self.cap_label, 4, 0, colspan=2)
            self.ci.addItem(self.insights_label, 5, 0, colspan=2)
        self.ci.layout.invalidate()
        self.update()

    def _relayout(self):
        """重建后强制重算行高 (总高 = 宽度×MKT_ASPECT, 文本按内容自适应)。"""
        self._wrap_bottom_labels()
        self.ci.layout.invalidate()
        self.ci.resize(self.width(), int(self.width() * MKT_ASPECT))
        self._apply_row_heights()
        self.update()

    def _update_crosshair_fmt_x(self, d):
        """更新已挂载十字光标的日期格式化函数 (绑定最新数据的 days)。

        chips 面板 X 轴是分布权重 (非日期), 跳过日期更新。
        """
        for ch in self._crosshairs:
            plot = ch.plot
            key = None
            for k, p in self.plots.items():
                if p is plot:
                    key = k
                    break
            if key is None or key == "chips":
                continue
            data_key = key
            if data_key in d:
                days = d[data_key].get("days") or []
                ds = [_parse_day(d) for d in days]

                def fmt(i, ds=ds):
                    idx = int(round(i))
                    if 0 <= idx < len(ds) and ds[idx] is not None:
                        return ds[idx].strftime("%Y-%m-%d")
                    return ""
                ch._fmt_x = fmt

    # ── 各面板绘制 (配色/线型与 plot_market 一致) ──
    def _draw_main_flow(self, mf):
        plot = self.plots["main_flow"]
        if not mf:
            self._empty_panel("main_flow")
            return
        days = mf["days"]
        x = np.arange(len(days))
        vals = np.asarray(mf["vals"], dtype=float)
        plot, n = self._date_plot("main_flow", days)
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen(theme.C_MUTED, 0.8)))
        brushes = [pg.mkBrush(theme.C_UP if v >= 0 else theme.C_DOWN) for v in vals]
        plot.addItem(pg.BarGraphItem(x=x, height=vals, width=0.72,
                                     brushes=brushes, pen=None))
        legend = plot.addLegend(offset=(12, 4))
        if mf.get("ma5"):
            l5 = plot.plot(x, np.asarray(mf["ma5"], dtype=float),
                           pen=_pen(theme.C_ACCENT, 1.6), connect="finite")
            legend.addItem(l5, "5日均线")
        if mf.get("ma20"):
            l20 = plot.plot(x, np.asarray(mf["ma20"], dtype=float),
                            pen=_pen(theme.C_DOWN, 1.6), connect="finite")
            legend.addItem(l20, "20日均线")
        cum = np.asarray(mf["cum"], dtype=float)
        plot.addItem(pg.FillBetweenItem(
            pg.PlotCurveItem(x, cum, connect="finite"),
            pg.PlotCurveItem(x, np.zeros(len(x))),
            brush=_brush_alpha(theme.C_MUTED, 0.08)))
        lc = plot.plot(x, cum, pen=_pen(theme.C_MUTED, 1.8,
                                        Qt.PenStyle.DashDotLine),
                       connect="finite")
        legend.addItem(lc, "累计净流入")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        plot.setYRange(*self._yrange(vals, cum), padding=0)

    def _draw_sub_flow(self, sf):
        plot = self.plots["sub_flow"]
        if not sf:
            self._empty_panel("sub_flow")
            return
        days = sf["days"]
        n = len(days)
        plot, n = self._date_plot("sub_flow", days)
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=_pen(theme.C_MUTED, 0.8)))
        legend = plot.addLegend(offset=(12, 4))
        if "super" in sf:
            x = np.arange(n)
            wd2 = 0.62
            specs = (("super", theme.C_UP, "超大单"), ("large", theme.C_ACCENT, "大单"),
                     ("mid", theme.C_DOWN, "中单"), ("small", theme.C_MUTED, "小单"))
            ymax = 0.0
            for key, color, lab in specs:
                arr = np.asarray(sf[key], dtype=float)
                offsets = {"super": -1.5 * wd2 / 4, "large": -0.5 * wd2 / 4,
                           "mid": 0.5 * wd2 / 4, "small": 1.5 * wd2 / 4}
                bars = pg.BarGraphItem(x=x + offsets[key], height=arr,
                                       width=wd2 / 4, pen=None,
                                       brush=_brush_alpha(color, 0.9))
                plot.addItem(bars)
                legend.addItem(bars, lab)
                if len(arr):
                    ymax = max(ymax, float(np.nanmax(np.abs(arr))))
            plot.setYRange(-ymax * 1.15, ymax * 1.15, padding=0)
        else:
            x = np.arange(len(sf["vals"]))
            vals = np.asarray(sf["vals"], dtype=float)
            brushes = [pg.mkBrush(theme.C_UP if v >= 0 else theme.C_DOWN) for v in vals]
            plot.addItem(pg.BarGraphItem(x=x, height=vals, width=0.72,
                                         brushes=brushes, pen=None))
            plot.setYRange(*self._yrange0(vals), padding=0)
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

    def _draw_chips(self, chips):
        plot = self.plots["chips"]
        if not chips:
            self._empty_panel("chips")
            return
        prices = np.asarray(chips["prices"], dtype=float)
        weights = np.asarray(chips["weights"], dtype=float)
        cur = float(chips["cur"])
        poc = float(chips["poc"])
        h = (prices[1] - prices[0]) * 0.85 if len(prices) > 1 else 1.0
        brushes = [pg.mkBrush(theme.C_UP if p >= cur else theme.C_DOWN) for p in prices]
        plot.addItem(pg.BarGraphItem(x0=np.zeros(len(prices)), x1=weights,
                                     y=prices - h / 2, height=h,
                                     brushes=brushes, pen=None))
        plot.addItem(pg.InfiniteLine(pos=poc, angle=0,
                                     pen=_pen(theme.C.get("poc", theme.C_AMBER), 1.4,
                                               Qt.PenStyle.DotLine)))
        plot.addItem(pg.InfiniteLine(pos=cur, angle=0,
                                     pen=_pen(theme.C_TEXT, 1.4,
                                               Qt.PenStyle.DotLine)))
        xmax = float(np.nanmax(weights))
        if not np.isfinite(xmax) or xmax <= 0:
            xmax = 1.0
        y0, y1 = float(prices[0]), float(prices[-1])
        pad_y = (y1 - y0) * 0.03
        self._text(plot, xmax * 0.02, y1 - pad_y, f"现价 {cur:.2f}",
                   theme.C_TEXT, anchor=(0, 1), delta=-2, bold=True)
        self._text(plot, xmax * 1.13, y0 + pad_y, f"POC {poc:.2f}",
                   theme.C.get("poc", theme.C_AMBER), anchor=(1, 0), delta=-2, bold=True)
        plot.setXRange(0.0, xmax * 1.15, padding=0)
        plot.setYRange(y0, y1, padding=0)
        plot.enableAutoRange(x=False, y=False)
        self._full_x["chips"] = (0.0, xmax * 1.15)
        # 更新该面板的 X 全幅
        self.set_full_x(plot, self._full_x["chips"])

    def _draw_holders(self, holders):
        plot = self.plots["holders"]
        if not holders:
            self._empty_panel("holders")
            return
        days = holders["days"]
        plot, n = self._date_plot("holders", days)
        x = np.arange(len(days))
        nums = np.asarray(holders["nums"], dtype=float)
        ratios = holders["ratios"]
        cols = [pg.mkBrush(theme.C_UP if r > 0 else theme.C_DOWN) for r in ratios]
        plot.addItem(pg.BarGraphItem(x=x, height=nums, width=0.6,
                                     brushes=cols, pen=None))
        plot.plot(x, nums, pen=_pen(theme.C_MUTED, 1.5), symbol="d", symbolSize=4,
                  symbolPen=pg.mkPen(theme.C_MUTED), symbolBrush=pg.mkBrush(theme.C_MUTED))
        for i, (xi, yi, lab) in enumerate(zip(x, nums, holders["labels"])):
            self._text(plot, float(xi), float(yi), lab, theme.C_MUTED,
                       anchor=(0.5, 0), delta=-3, bold=True)
        hi = float(np.nanmax(nums)) if len(nums) else 1.0
        plot.setYRange(0.0, max(hi * 1.25, 1e-9), padding=0)

    def _draw_sd(self, sd):
        plot = self.plots["sd"]
        if not sd:
            self._empty_panel("sd")
            return
        days = sd["days"]
        plot, n = self._date_plot("sd", days)
        x = np.arange(len(days))
        dem = np.asarray(sd["demand"], dtype=float)
        sup = np.asarray(sd["supply"], dtype=float)
        wd2 = 0.36
        legend = plot.addLegend(offset=(12, 4))
        bd = pg.BarGraphItem(x=x - wd2 / 2, height=dem, width=wd2, pen=None,
                             brush=pg.mkBrush(theme.C_UP))
        bs = pg.BarGraphItem(x=x + wd2 / 2, height=sup, width=wd2, pen=None,
                             brush=pg.mkBrush(theme.C_DOWN))
        plot.addItem(bd)
        plot.addItem(bs)
        legend.addItem(bd, "需求")
        legend.addItem(bs, "供给")
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))
        hi = max(float(np.nanmax(dem)), float(np.nanmax(sup))) if len(dem) else 1.0
        plot.setYRange(0.0, max(hi * 1.15, 1e-9), padding=0)

    # ── 辅助 ──
    def _render_header_html(self, d):
        """顶部估值卡: 横幅 + 估值合并一行, 简洁富文本。"""
        if not d:
            return ""
        items = d.get("header_items")
        if not items:
            txt = d.get("header") or ""
            return txt
        sep = f"&nbsp;<span style=\"color:{theme.C_BORDER};\">|</span>&nbsp;"
        parts = []
        for label, value, color in items:
            if label is None:
                parts.append(
                    f"<span style=\"color:{theme.C_ACCENT};font-weight:bold;\">"
                    f"{value}</span>")
            else:
                vc = color or theme.C_TEXT
                parts.append(
                    f"<span style=\"color:{theme.C_MUTED};\">{label}</span>&nbsp;"
                    f"<b style=\"color:{vc};\">{value}</b>")
        return sep.join(parts)

    def _render_caps_html(self, d):
        if not d:
            return ""
        items = d.get("caps_items")
        if items:
            sep = f"&nbsp;<span style=\"color:{theme.C_BORDER};\">·</span>&nbsp;"
            spans = [
                f"<span style=\"color:{c or theme.C_TEXT};font-weight:bold;\">{t}</span>"
                for t, c in items
            ]
            return sep.join(spans)
        txt = d.get("caps") or ""
        if not txt:
            return ""
        color = d.get("caps_color") or theme.C_TEXT
        return f"<span style=\"color:{color};font-weight:bold;\">{txt}</span>"

    def _render_insights_html(self, d):
        if not d:
            return ""
        items = d.get("insights_items")
        if items:
            sep = f"&nbsp;<span style=\"color:{theme.C_BORDER};\">|</span>&nbsp;"
            spans = [
                f"<span style=\"color:{c or theme.C_MUTED};\">{t}</span>"
                for t, c in items
            ]
            return sep.join(spans)
        return d.get("insights") or ""

    def _empty_panel(self, key):
        plot = self.plots[key]
        plot.clear()
        plot.setTitle("", color=theme.C_MUTED, size=f"{self._fs(1)}pt")
        self._full_x[key] = (0.0, 1.0)

    def _yrange(self, *series):
        parts = []
        for s in series:
            a = np.asarray(s, dtype=float).ravel()
            if a.size:
                parts.append(a)
        if not parts:
            return 0.0, 1.0
        allv = np.concatenate(parts)
        lo = float(np.nanmin(allv))
        hi = float(np.nanmax(allv))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi - lo < 1e-12:
            return 0.0, 1.0
        pad = (hi - lo) * 0.06
        return lo - pad, hi + pad

    def _yrange0(self, vals):
        hi = float(np.nanmax(np.abs(np.asarray(vals, dtype=float))))
        if not np.isfinite(hi) or hi <= 0:
            return 0.0, 1.0
        return -hi * 1.15, hi * 1.15

    def _fs(self, delta=0):
        return max(6, self._font_size + delta)

    def _font(self, delta=0, bold=False):
        f = QtGui.QFont()
        f.setFamily(FONT_CANDIDATES[0])
        f.setPointSize(self._fs(delta))
        f.setBold(bold)
        return f

    def _text(self, plot, x, y, text, color, anchor=(0.5, 0.5),
              delta=0, bold=False):
        ti = pg.TextItem(text, color=color, anchor=anchor)
        ti.setFont(self._font(delta, bold))
        ti.setPos(float(x), float(y))
        plot.addItem(ti)
        return ti

    def _date_plot(self, key, days):
        if key in self._date_axes:
            self._date_axes[key].set_days([_parse_day(d) for d in days])
        plot = self.plots[key]
        n = len(days)
        full = (0.0, float(max(n - 1, 1)))
        self._full_x[key] = full
        # 先更新 X 全幅限制, 再设置视图范围 (避免 setXRange 被旧 limits 夹紧)
        self.set_full_x(plot, full)
        plot.setXRange(*full, padding=0)
        return plot, n

    def _set_panel_title(self, key, title):
        if title:
            self.plots[key].setTitle(title, color=theme.C_TEXT,
                                     size=f"{self._fs(2)}pt")

    def _set_chips_shape(self, shape_txt, color):
        plot = self.plots["chips"]
        t = plot.titleLabel.text
        plot.titleLabel.setText(
            f"<span style='color:{theme.C_TEXT};font-size:{self._fs(2)}pt;'>"
            f"{t}</span><br/>"
            f"<span style='color:{color};font-size:{self._fs(1)}pt;'>"
            f"{shape_txt}</span>")

    # ── BasePlotWidget 钩子: X 联动后回调 (资金透视各面板 Y 固定, 无需处理) ──
    def on_x_range_changed(self, x0, x1, source_vb=None):
        pass

    def _on_crosshair_moved(self, crosshair):
        """十字光标移动时更新状态栏显示当前面板数值。"""
        key = getattr(crosshair, "_panel_key", "")
        if key and hasattr(self, "_crosshair_values"):
            self._crosshair_values[key] = (crosshair._x, crosshair._y)
            self.crosshair_moved.emit(key, crosshair._x, crosshair._y)

    def _status(self, msg):
        """发送状态栏消息 (由主窗口连接)。"""
        if hasattr(self, "status_message"):
            self.status_message.emit(msg)

    def _show_shortcuts_help(self):
        """显示快捷键帮助对话框。"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("资金透视快捷键")
        dlg.setModal(True)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        text = (
            "<b>面板聚焦:</b><br>"
            "&nbsp;&nbsp;1-5 &nbsp;聚焦对应面板 (主力资金/资金分项/筹码/股东/供需)<br>"
            "&nbsp;&nbsp;0 / Esc / G &nbsp;恢复完整布局<br><br>"
            "<b>视图控制:</b><br>"
            "&nbsp;&nbsp;滚轮 &nbsp;以光标为锚点缩放 X 轴<br>"
            "&nbsp;&nbsp;左键拖拽 &nbsp;平移<br>"
            "&nbsp;&nbsp;双击 &nbsp;复位全幅<br>"
            "&nbsp;&nbsp;↑/+/= &nbsp;放大, ↓/- &nbsp;缩小 (视图中心锚点)<br>"
            "&nbsp;&nbsp;←/→ &nbsp;平移日期面板, Shift+←/→ &nbsp;快速平移<br>"
            "&nbsp;&nbsp;Home/r &nbsp;复位, Backspace/f &nbsp;视图历史后退/前进<br><br>"
            "<b>功能键:</b><br>"
            "&nbsp;&nbsp;H &nbsp;显示此帮助<br>"
            "&nbsp;&nbsp;Ctrl+S &nbsp;切换面板联动缩放模式<br>"
            "&nbsp;&nbsp;Ctrl+C &nbsp;复制当前面板数据"
        )
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(lab)
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(dlg.accept)
        lay.addWidget(btn_box)
        dlg.exec()

    def _copy_panel_data(self):
        """复制当前聚焦面板数据到剪贴板。"""
        from PyQt6.QtWidgets import QApplication
        key = self._focused or "main_flow"
        plot = self.plots.get(key)
        if not plot:
            return
        items = plot.listDataItems()
        if not items:
            return
        lines = [f"面板: {key}"]
        for item in items:
            if hasattr(item, "getData"):
                x, y = item.getData()
                if x is not None and y is not None:
                    lines.append(f"  数据点: {len(x)} 个")
                    break
        QApplication.clipboard().setText("\n".join(lines))
        self._status("已复制面板数据摘要到剪贴板")

    def _toggle_sync_mode(self):
        """切换面板 X 轴联动模式 (资金透视默认各面板独立)。"""
        self._sync_x = not getattr(self, "_sync_x", False)
        if self._sync_x:
            for plot in self.plots.values():
                self._synced.add(plot)
        else:
            self._synced.clear()
        self._status(f"面板联动: {'开启' if self._sync_x else '关闭'}")

    # ── 键盘: 左右箭头平移日期面板 + 数字键聚焦面板, 其余交给基类 ──
    def keyPressEvent(self, ev):
        if not self._has_data:
            return super().keyPressEvent(ev)
        key = ev.key()
        if key == Qt.Key.Key_1:
            self.focus_panel("main_flow")
            ev.accept()
            return
        if key == Qt.Key.Key_2:
            self.focus_panel("sub_flow")
            ev.accept()
            return
        if key == Qt.Key.Key_3:
            self.focus_panel("chips")
            ev.accept()
            return
        if key == Qt.Key.Key_4:
            self.focus_panel("holders")
            ev.accept()
            return
        if key == Qt.Key.Key_5:
            self.focus_panel("sd")
            ev.accept()
            return
        if key in (Qt.Key.Key_0, Qt.Key.Key_Escape, Qt.Key.Key_G):
            if self._focused is not None:
                self.show_grid()
            ev.accept()
            return
        if key == Qt.Key.Key_H:
            self._show_shortcuts_help()
            ev.accept()
            return
        if key == Qt.Key.Key_C and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._copy_panel_data()
            ev.accept()
            return
        if key == Qt.Key.Key_S and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._toggle_sync_mode()
            ev.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            frac = -0.1 if key == Qt.Key.Key_Left else 0.1
            for name in ("main_flow", "sub_flow", "holders", "sd"):
                plot = self.plots.get(name)
                if plot is not None:
                    self.pan_by(frac, plot=plot)
            ev.accept()
            return
        super().keyPressEvent(ev)

    # ── 兼容旧接口: zoom_x_about(key, cx, factor) → 指定面板缩放 ──
    def zoom_x_about(self, *args, **kwargs):
        """兼容: zoom_x_about(cx, factor) 或 zoom_x_about(key, cx, factor)。"""
        if len(args) >= 3:
            key, cx, factor = args[0], args[1], args[2]
            plot = self.plots.get(key)
            if plot is None:
                return
            self.zoom_about(cx, factor, plot=plot)
            return
        return super().zoom_x_about(*args, **kwargs)

    # ── 视图重置 (BasePlotWidget.reset_view 会遍历所有已注册面板) ──
    def reset_view(self):
        super().reset_view()

    def reset_plot(self, key):
        """复位指定面板到全幅 (兼容旧接口)。"""
        plot = self.plots.get(key)
        lim = self._x_limits.get(plot) if plot is not None else None
        if plot is not None and lim is not None:
            self.apply_view(lim[0], lim[1], plot=plot, push=False)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
