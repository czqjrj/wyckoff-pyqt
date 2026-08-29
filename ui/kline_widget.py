"""pyqtgraph K线图控件 (替换原 matplotlib K线图)。

数据由 AnalysisThread 在 worker 线程通过 build_kline_data() 收集 (K线相关计算
仍在 wyckoff 包内完成), 主线程调用 set_data() 渲染 — pyqtgraph 非线程安全。

交互 (由 ui.base_plot.BasePlotWidget 基类统一提供):
  - 滚轮      以光标为锚点平滑缩放 X 轴 (Y 随可见区间自动重算);
              Ctrl+滚轮 缩放价格轴 Y (双击复位恢复自动)
  - 左键拖拽 平移
  - 双击    复位到全幅
  - 键盘    上箭头 / + 放大, 下箭头 / - 缩小 (以视图中心为锚点),
            左/右箭头逐根步进十字光标 (视口边缘自动跟随),
            Shift+左/右 按 20% 跨度快速平移,
            Home/r 复位, Backspace/f 视图历史, Esc 清除测量尺
  - Shift+左键拖拽  测量尺: 两点间价差/涨跌幅/K线根数/日期跨度
  - 单击    事件/VSA 文本标签 → labelClicked 信号 (由主窗口弹窗解释);
            悬停标签显示一句话 tooltip 预览
  - 十字光标吸附最近一根K线, 图上方信息条实时显示该根 OHLC/涨跌幅/量/均线
"""
import math

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.Qt.QtCore import Qt

from wyckoff.config import _PHASE_STYLE, EVENT_CN, FONT_CANDIDATES, W_RECENT

from . import theme
from .base_plot import BasePlotWidget
from .constants import KLINE_DEFAULT_BARS, KLINE_RIGHT_MARGIN
from .renderers.pnf_grid import _DragTextItem, _LatestBtnItem

# A股配色: 红涨绿跌 (运行时从 theme 动态取色, 支持主题切换)


def _feedback_verdicts(df, segs, symbol, scale):
    """按 标的+周期+起止时间 关联阶段带的反馈标注, 返回 {(key,a,e): verdict}。

    各带 正确/错误 徽标直接画在 K 线上, 一眼可辨阶段判定靠不靠谱。"""
    if not segs or not symbol:
        return {}
    try:
        from wyckoff.storage import _day_fmt, feedback_key, load_feedback
    except Exception:
        return {}
    fbmap = {}
    try:
        for r in load_feedback():
            if r.get("start_dt") and r.get("end_dt"):
                fbmap[feedback_key(r["symbol"], r.get("scale", 240),
                                   r["start_dt"], r["end_dt"])] = r
    except Exception:
        return {}
    out = {}
    for a, e, key, _label in segs:
        if not (0 <= a < e < len(df)):
            continue
        k = feedback_key(symbol, int(scale), _day_fmt(df["day"].iloc[a]),
                         _day_fmt(df["day"].iloc[e]))
        r = fbmap.get(k)
        if r and r.get("verdict"):
            out[(key, int(a), int(e))] = r.get("verdict"), r.get("source")
    return out

_MA_LINES = [
    ("price_ma5", "#f783ac", "MA5"),
    ("price_ma10", "#12b886", "MA10"),
    ("price_ma20", "#1971c2", "MA20"),
    ("price_ma50", "#f08c00", "MA50"),
    ("price_ma200", "#adb5bd", "MA200"),
]

_VOL_MA_LINES = [
    ("vol_ma5", "#f783ac", "量MA5"),
    ("vol_ma10", "#12b886", "量MA10"),
    ("vol_ma20", "#1971c2", "量MA20"),
]

_VSA_DRAW = {"CHOC", "UPT", "TRU", "TRD", "DEM", "SUP", "ABS",
             "TEST", "ETR", "ETF", "BC", "SV"}

# 图层显隐定义: key → 右键菜单显示名。set_layer_visible 控制可见性,
# 状态由主窗口持久化到 settings["kline_layers"]。
LAYER_DEFS = (
    ("phase", "阶段带"),
    ("pivot", "枢轴线"),
    ("ma", "均线/BOLL"),
    ("waves", "波浪"),
    ("events", "事件"),
    ("vsa", "VSA"),
    ("locks", "锁点"),
    ("news", "新闻"),
)


def _brush_alpha(color, alpha):
    c = pg.mkColor(color)
    c.setAlphaF(float(alpha))
    return pg.mkBrush(c)


def _pen(color, width=1.0, style=None):
    kw = {"width": width}
    if style is not None:
        kw["style"] = style
    return pg.mkPen(color, **kw)


class CandlestickItem(pg.GraphicsObject):
    """蜡烛图: 实时逐根绘制, 影线用 0 宽画笔 (任意缩放下恒为 1px 细线)。

    注意不能用 QPicture 预生成: 录制时的画笔宽度会随回放变换等比缩放,
    默认视野下影线被放大成与实体同宽的粗柱, 蜡烛看起来像柱状图。
    影线用 hairline 画笔 (width=0) 保证任何缩放级别都保持 1px。
    """

    def __init__(self, x, opens, closes, lows, highs, width=0.6,
                 up_color=None, down_color=None):
        super().__init__()
        self._x = np.asarray(x, dtype=float)
        self._o = np.asarray(opens, dtype=float)
        self._c = np.asarray(closes, dtype=float)
        self._l = np.asarray(lows, dtype=float)
        self._h = np.asarray(highs, dtype=float)
        self._width = width
        self._up = pg.mkColor(up_color or theme.C_UP)
        self._dn = pg.mkColor(down_color or theme.C_DOWN)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def set_data(self, x, opens, closes, lows, highs):
        """增量更新整组数据 (比重建 item 便宜; 仅当长度/序列变化时调用)。"""
        self._x = np.asarray(x, dtype=float)
        self._o = np.asarray(opens, dtype=float)
        self._c = np.asarray(closes, dtype=float)
        self._l = np.asarray(lows, dtype=float)
        self._h = np.asarray(highs, dtype=float)
        self.prepareGeometryChange()
        self.update()

    def set_last(self, x, open_, close, low, high):
        """仅更新最后一根 K 线 (实时刷新场景, 比全量 set_data 便宜)。"""
        if self._x.size == 0:
            return
        i = self._x.size - 1
        self._x[i] = float(x)
        self._o[i] = float(open_)
        self._c[i] = float(close)
        self._l[i] = float(low)
        self._h[i] = float(high)
        self.prepareGeometryChange()
        self.update()

    def paint(self, p, *args):
        half = self._width / 2.0
        wick = QtGui.QPen()
        wick.setWidth(0)
        # 只绘制可见区间内的柱: 全幅/快速拖拽时大幅减少 Python 循环量
        i0, i1 = 0, self._x.size - 1
        vb = self.getViewBox()
        if vb is not None and self._x.size:
            vr = vb.viewRect()
            if vr is not None and vr.width() > 0:
                lo = max(0, int(math.floor(vr.left())) - 1)
                hi = min(self._x.size - 1, int(math.ceil(vr.right())) + 1)
                if hi >= lo:
                    i0, i1 = lo, hi
        for xi, o, c, lo_, hi_ in zip(self._x[i0:i1 + 1], self._o[i0:i1 + 1],
                                      self._c[i0:i1 + 1], self._l[i0:i1 + 1],
                                      self._h[i0:i1 + 1]):
            if not (np.isfinite(lo_) and np.isfinite(hi_)):
                continue
            col = self._up if c >= o else self._dn
            wick.setColor(col)
            p.setPen(wick)
            p.drawLine(QtCore.QPointF(xi, lo_), QtCore.QPointF(xi, hi_))
            p.setPen(QtGui.QPen(Qt.PenStyle.NoPen))
            p.setBrush(QtGui.QBrush(col))
            p.drawRect(QtCore.QRectF(xi - half, min(o, c), self._width,
                                     max(abs(c - o), 0.04)))

    def boundingRect(self):
        if self._x.size == 0:
            return QtCore.QRectF(0, 0, 1, 1)
        x0, x1 = float(self._x.min()) - 1, float(self._x.max()) + 1
        y0, y1 = float(np.nanmin(self._l)), float(np.nanmax(self._h))
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class PhaseBands(pg.GraphicsObject):
    """威科夫阶段底色带: 垂直色带铺满价格区间, 画在蜡烛之下。"""

    def __init__(self, bands, y_range):
        super().__init__()
        self._bands = bands            # [(x0, x1, color, alpha, label)]
        self._yr = y_range
        self._picture = None
        self._generate()

    def _generate(self):
        pict = QtGui.QPicture()
        p = QtGui.QPainter(pict)
        y = 1e6
        for x0, x1, color, alpha, _label in self._bands:
            c = pg.mkColor(color)
            c.setAlphaF(float(alpha))
            p.fillRect(QtCore.QRectF(x0 - 0.5, -y, (x1 - x0) + 1, 2 * y),
                       QtGui.QBrush(c))
        p.end()
        self._picture = pict

    def paint(self, p, *args):
        p.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if not self._bands:
            return QtCore.QRectF(0, 0, 1, 1)
        y0, y1 = self._yr
        x0 = min(b[0] for b in self._bands) - 0.5
        x1 = max(b[1] for b in self._bands) + 0.5
        return QtCore.QRectF(x0, y0, max(x1 - x0, 1), max(y1 - y0, 1))


class _DateAxis(pg.AxisItem):
    """X 轴日期刻度: 按整数柱索引回查 df['day'] 渲染日期标签。"""

    def __init__(self, orientation="bottom"):
        super().__init__(orientation=orientation)
        self._days = []
        self._minute = False

    def set_days(self, days, minute):
        self._days = list(days)
        self._minute = bool(minute)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._days):
                d = self._days[i]
                if self._minute:
                    out.append(d.strftime("%m-%d %H:%M"))
                else:
                    out.append(d.strftime("%y-%m-%d"))
            else:
                out.append("")
        return out


class _KlineViewBox(pg.ViewBox):
    """价格/量能/累计量 ViewBox。

    - enableMenu=False: 禁用 pyqtgraph 英文原生右键菜单, 统一走主窗口
      的自定义右键菜单 (保存图片/复位视图/图层开关)。
    - 滚轮缩放由基类控件层接管; 此处处理双击复位、单击标签触发解释、
      Shift+左键拖拽测量尺。
    """

    def __init__(self, host, *args, **kwargs):
        kwargs.setdefault("enableMenu", False)
        super().__init__(*args, **kwargs)
        self._host = host

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            if ev.double():
                self._host.reset_view()
                ev.accept()
                return
            label = self._host.hit_label(ev.scenePos())
            if label:
                self._host.labelClicked.emit(label[0], label[1])
                ev.accept()
                return
        super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev):
        """Shift+左键拖拽 = 测量尺 (仅价格面板); 其余保持默认平移。"""
        if (ev.button() == Qt.MouseButton.LeftButton
                and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and self._host._measure_target_ok(self)):
            ev.accept()
            self._host._measure_update(ev.buttonDownScenePos(ev.button()),
                                       ev.scenePos(), final=ev.isFinish())
            return
        super().mouseDragEvent(ev)


class KlineWidget(BasePlotWidget):
    """pyqtgraph K线图: 价格 / 成交量 / 波段累计量 三栏, X 轴联动。

    继承 BasePlotWidget 获得统一交互 (滚轮/键盘/拖拽边界/双击复位/视图历史)。
    set_data(**kline_data) 接收 build_kline_data() 的返回。
    """

    labelClicked = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._days = []
        self._is_minute = False
        self._n = 0
        self._fb_verdicts = {}
        self._chart_lo = np.array([])
        self._chart_hi = np.array([])
        self._full_x = (0.0, 1.0)
        self._full_y = (0.0, 1.0)
        self._full_vol = (0.0, 1.0)
        self._date_axis = None
        self.price_plot = None
        self.vol_plot = None
        self.cum_plot = None
        # 图层显隐: item 注册表 + 各层开关状态 (主窗口右键菜单控制/持久化)
        self._layer_items = {}
        self._layer_on = {k: True for k, _n in LAYER_DEFS}
        # 测量尺 (Shift+左键拖拽): [curve, text] 或 None
        self._measure_items = None
        # OHLC 信息条: 主窗口把 QLabel 注入 info_bar, 十字光标驱动刷新
        self.info_bar = None
        self._df_ref = None
        self._candle_item = None  # 增量更新用: 最近一次渲染的 CandlestickItem
        self._help_item = None  # ? 快捷键帮助浮层
        self._latest_btn = None  # 右下角 "回到最新" 悬浮按钮
        self._bookmarks = {}  # 视图书签 {1-9: (x0,x1,y0,y1)}
        self._build_plots()
        self.price_plot.setTitle("输入 A 股代码 (如 600104 / sh600104 / 000001), "
                                 "点击\"开始分析\"加载 K 线图。")

    # ── 布局 ──
    def _build_plots(self):
        self.ci.clear()
        self.ci.setContentsMargins(8, 8, 8, 8)
        self.ci.setSpacing(6)

        price_vb = _KlineViewBox(self)
        self.price_plot = pg.PlotItem(viewBox=price_vb)
        vol_vb = _KlineViewBox(self)
        self.vol_plot = pg.PlotItem(viewBox=vol_vb)
        self._date_axis = _DateAxis("bottom")
        cum_vb = _KlineViewBox(self)
        self.cum_plot = pg.PlotItem(viewBox=cum_vb,
                                    axisItems={"bottom": self._date_axis})

        self.ci.addItem(self.price_plot, 0, 0)
        self.ci.addItem(self.vol_plot, 1, 0)
        self.ci.addItem(self.cum_plot, 2, 0)
        gl = self.ci.layout
        gl.setRowStretchFactor(0, 32)
        gl.setRowStretchFactor(1, 10)
        gl.setRowStretchFactor(2, 7)

        for plot in (self.price_plot, self.vol_plot, self.cum_plot):
            plot.showAxis("bottom", False)
            plot.hideButtons()
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getViewBox().setMouseEnabled(x=True, y=False)
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                axis.setPen(_pen(theme.C["axis"], 1))
                axis.setTextPen(_pen(theme.C_TEXT, 1))
        self.cum_plot.showAxis("bottom", True)

        # 基类注册: 三栏 X 联动, price 为主面板 (滚轮/键盘/历史作用对象);
        # 价格面板开启 y_zoom 允许 Ctrl+滚轮 手动缩放价格轴。
        for i, plot in enumerate(
                (self.price_plot, self.vol_plot, self.cum_plot)):
            self.register_plot(plot, sync=True, primary=(i == 0),
                               y_zoom=(i == 0))

    # ── 主题 ──
    def apply_theme(self):
        """主题切换时刷新控件/视口/坐标轴配色, 无需重渲染数据。"""
        self.setBackground(pg.mkColor(theme.C_PANEL))
        for plot in (self.price_plot, self.vol_plot, self.cum_plot):
            if plot is None:
                continue
            plot.getViewBox().setBackgroundColor(pg.mkColor(theme.C_PANEL))
            for ax_name in ("left", "bottom"):
                axis = plot.getAxis(ax_name)
                if axis is not None:
                    axis.setPen(_pen(theme.C["axis"], 1))
                    axis.setTextPen(_pen(theme.C_TEXT, 1))

    _HELP_LINES = (
        "滚轮 缩放   拖拽 平移   Shift+拖拽 测量尺   双击 复位全幅",
        "+/− 缩放   ←→ 十字逐根步进   ↑↓ 缩放Y轴",
        "Shift+←→ 快速平移   PgUp/PgDn 大步平移   Home/R 全幅",
        "Backspace/F 视图历史   End 回到最新   Esc 清除测量尺",
        "数字键 取书签   Shift+数字 存书签   ? 关闭本帮助",
    )

    def toggle_help(self):
        """? 键切换快捷键速查浮层 (视口固定; Esc 或再按 ? 关闭)。"""
        if self._help_item is not None:
            self._help_item.setVisible(not self._help_item.isVisible())
            return
        ti = _DragTextItem("\n".join(self._HELP_LINES), color=theme.C_TEXT,
                           anchor=(0.5, 0.5), border=_pen(theme.C_BORDER, 1.0),
                           fill=pg.mkBrush(theme.C_PANEL))
        f = self._font()
        f.setPointSize(self._fs(0))
        ti.setFont(f)
        ti._ephemeral = False
        ti._pin_anchor = (0.5, 0.5)
        ti._pin_fx = 0.5
        ti._pin_fy = 0.40
        ti.setParentItem(self.price_plot.vb)
        ti.set_vp(0.5, 0.40)
        self._help_item = ti

    def keyPressEvent(self, ev):
        key = ev.key()
        mods = ev.modifiers()
        if key in (Qt.Key.Key_Question, Qt.Key.Key_Slash):
            self.toggle_help()
            ev.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._help_item is not None and self._help_item.isVisible():
                self._help_item.hide()
                ev.accept()
                return
        if key == Qt.Key.Key_End:
            self.jump_to_latest()
            ev.accept()
            return
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            idx = int(key) - int(Qt.Key.Key_1) + 1
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.save_bookmark(idx)
            else:
                self.recall_bookmark(idx)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def jump_to_latest(self):
        """回到最新K线 (End 键 / 右下角按钮): 保持当前缩放级别，仅平移到最新柱。"""
        if not self._n:
            return
        if self.price_plot is None:
            return
        vb = self.price_plot.vb
        x0, x1 = vb.viewRange()[0]
        span = x1 - x0
        lim = self._x_limits.get(self.price_plot)
        if lim:
            lo, hi = lim
            # 目标: 最新柱在视口右侧留白区之前 (即 x1 约等于 n-1)
            target_x1 = min(hi, self._n - 1)
            target_x0 = max(lo, target_x1 - span)
            if target_x1 - target_x0 < self.MIN_APPLY_SPAN:
                target_x0 = target_x1 - span
        else:
            target_x1 = self._n - 1
            target_x0 = target_x1 - span
        self.apply_view(target_x0, target_x1)
        self._update_latest_btn()

    def _update_latest_btn(self):
        """离开最新行情区 (右缘看不到最后一根K线) 时显示回到最新按钮。"""
        btn = self._latest_btn
        if btn is None:
            return
        if not self._n or self.price_plot is None:
            btn.hide()
            return
        _, x1 = self.price_plot.vb.viewRange()[0]
        btn.setVisible(bool(x1 < self._n - 0.5))

    def _ensure_latest_btn(self):
        """懒创建回到最新按钮 (首次 set_data 后调用)。"""
        if self._latest_btn is not None:
            return
        btn = _LatestBtnItem(self)
        btn._ephemeral = False
        btn._pin_fx = 0.997
        btn._pin_fy = 0.985
        f = self._font()
        f.setPointSize(self._fs(0))
        btn.setFont(f)
        btn.setParentItem(self.price_plot.vb)
        btn.set_vp(0.997, 0.985)
        self._latest_btn = btn

    def on_x_range_changed(self, x0, x1, source_vb=None):
        """X 轴范围变化时更新回到最新按钮可见性。"""
        self._update_latest_btn()

    def save_bookmark(self, idx):
        """存视图书签 (Shift+数字键 1-9)。"""
        if not self._n or self.price_plot is None:
            return
        xr, yr = self.price_plot.vb.viewRange()
        self._bookmarks[int(idx)] = (float(xr[0]), float(xr[1]),
                                     float(yr[0]), float(yr[1]))

    def recall_bookmark(self, idx):
        """取视图书签 (数字键 1-9); 未存的号忽略。"""
        key = self._bookmarks.get(int(idx))
        if key is None or self.price_plot is None:
            return
        self.price_plot.vb.setRange(xRange=key[:2], yRange=key[2:], padding=0)

    # ── 数据入口 ──
    def set_data(self, df=None, title="", pivots=None, events=None, waves=None,
                 draw_waves=True, locks=None, tr=None, profile=None, phase=None,
                 segs=None, sector=None, vsa_signals=None, wave_cum=None,
                 wave_segs=None, up_mask=None, caption=None, symbol=None,
                 scale=240, news_markers=None, **extra):
        # 批量更新: 三栏图表构建期间禁用重绘 (减少中间状态闪烁)
        self.setUpdatesEnabled(False)
        try:
            self._finish_anims()
            self._measure_clear()
            self._layer_items = {}
            self._latest_btn = None  # 旧按钮随旧 PlotItem 失效
            self.clear_plots()
            self._days = []
            self._n = 0
            if df is None or len(df) == 0:
                self._df_ref = None
                self._build_plots()
                self.price_plot.setTitle(title or "暂无 K 线数据")
                if self.info_bar is not None:
                    self.info_bar.setText("")
                return
            n = len(df)
            self._n = n
            self._has_data = True
            self._df_ref = df
            self._days = df["day"].tolist()
            self._fb_verdicts = _feedback_verdicts(df, segs or [], symbol, scale)
            try:
                self._is_minute = df["day"].dt.hour.nunique() > 1
            except Exception:
                self._is_minute = False
            self._chart_lo = df["low"].values.astype(float)
            self._chart_hi = df["high"].values.astype(float)
            # 最新K线右侧留白若干根, 观察延伸/突破空间 (全幅/复位时可见)
            frac, mlo, mhi = KLINE_RIGHT_MARGIN
            self._margin = int(min(max(round(n * frac), mlo), mhi))
            self._full_x = (0.0, float(n - 1 + self._margin))
            ylo, yhi = float(df["low"].min()), float(df["high"].max())
            pad = (yhi - ylo) * 0.06 if yhi > ylo else 1.0
            self._full_y = (ylo - pad, yhi + pad)
            vmax = float(np.nanmax(df["volume"].values)) / 1e4
            self._full_vol = (0.0, max(vmax * 1.15, 1.0))

            self._build_plots()
            for plot in (self.price_plot, self.vol_plot, self.cum_plot):
                self.set_full_x(plot, self._full_x)
            self._date_axis.set_days(self._days, self._is_minute)
            self._build_price(df, title, pivots or [], events or [],
                              [list(w) for w in (waves or [])],
                              bool(draw_waves), locks or [], tr, profile,
                              segs or [], sector, vsa_signals or [],
                              news_markers or [])
            self._build_volume(df, wave_segs or [])
            self._build_cum(df, wave_cum, wave_segs, caption)

            n_view = min(self._n, KLINE_DEFAULT_BARS)
            view_end = float(self._n - 1)
            self.apply_view(view_end - (n_view - 1), view_end, push=False)
            self._rescale_price_y(view_end - (n_view - 1), view_end)
            self._attach_crosshairs()
            self._apply_layers()
            self._update_info(view_end)
        finally:
            self.setUpdatesEnabled(True)
        self._ensure_latest_btn()
        self._update_latest_btn()

    def refresh_last_bar(self, df):
        """增量刷新最后一根 K 线 (实时行情场景): 只更新蜡烛 + 信息条。

        不重建图层/不重置视图, 比全量 set_data 便宜得多。传入的 df 需与当前
        数据等长 (仅最后一行的 OHLC 被采用)。数据不匹配时回退全量 set_data。
        """
        if df is None or len(df) == 0:
            return
        if not self._has_data or self._candle_item is None or self._n != len(df):
            self.set_data(df=df)
            return
        i = self._n - 1
        self._candle_item.set_last(
            float(i),
            float(df["open"].iloc[i]),
            float(df["close"].iloc[i]),
            float(df["low"].iloc[i]),
            float(df["high"].iloc[i]))
        self._df_ref = df
        self._chart_lo[i] = float(df["low"].iloc[i])
        self._chart_hi[i] = float(df["high"].iloc[i])
        # 视野可能处于最新列附近, 刷新价格轴自适应以包含新高低
        self._rescale_price_y(i - 1, i)
        self._update_info(i)

    # ── 十字光标 ──
    def _fmt_x(self):
        days = self._days
        minute = self._is_minute

        def fmt(i):
            idx = int(round(i))
            if 0 <= idx < len(days):
                d = days[idx]
                try:
                    return (d.strftime("%m-%d %H:%M") if minute
                            else d.strftime("%Y-%m-%d"))
                except Exception:
                    return str(d)
            return ""

        return fmt

    def _attach_crosshairs(self):
        self.detach_crosshairs()
        if self._n <= 0:
            return
        fmt_x = self._fmt_x()
        # snap=True: 竖线吸附最近一根K线; on_move: 驱动 OHLC 信息条刷新
        self.attach_crosshair(self.price_plot, fmt_x, lambda v: f"{v:.2f}",
                              snap=True, on_move=self._update_info)
        self.attach_crosshair(self.vol_plot, fmt_x, lambda v: f"{v:.1f}万手",
                              snap=True)
        self.attach_crosshair(self.cum_plot, fmt_x, lambda v: f"{v:.0f}",
                              snap=True)

    # ── 图层显隐 ──
    def _add(self, plot, item, layer=None, **kw):
        """addItem 并登记到图层注册表 (layer=None 表示常显)。"""
        if layer is not None and layer in self._layer_on:
            self._layer_items.setdefault(layer, []).append(item)
        plot.addItem(item, **kw)

    def set_layer_visible(self, key, on):
        """切换图层可见性 (phase/pivot/ma/waves/events/vsa/locks)。"""
        if key not in self._layer_on:
            return
        self._layer_on[key] = bool(on)
        for it in self._layer_items.get(key, []):
            it.setVisible(bool(on))

    def layers_state(self):
        return dict(self._layer_on)

    def set_layers_state(self, state):
        """恢复各层开关 (主窗口启动/重建后调用), 再应用到当前 items。"""
        for k in self._layer_on:
            if k in state:
                self._layer_on[k] = bool(state[k])

    def _apply_layers(self):
        for k, on in self._layer_on.items():
            if not on:
                for it in self._layer_items.get(k, []):
                    it.setVisible(False)

    # ── 主图: 蜡烛 + 均线 + 波段 + 事件 + 锁 + VSA ──
    def _build_price(self, df, title, pivots, events, waves, draw_waves,
                     locks, tr, profile, segs, sector, vsa_signals,
                     news_markers=None):
        x = np.arange(self._n)
        plot = self.price_plot
        plot.setTitle(title or "", color=theme.C_TEXT, size=f"{self._fs(3)}pt")

        if segs:
            bands = [(s0, s1, _PHASE_STYLE[key][1], _PHASE_STYLE[key][2], label)
                     for s0, s1, key, label in segs if key in _PHASE_STYLE]
            if bands:
                bands_item = PhaseBands(bands, self._full_y)
                bands_item.setZValue(-100)
                self._add(plot, bands_item, "phase")
                # 阶段徽标 (短名+全名, 彩色底片) 按序错开, 避免相邻带标签重叠
                top = self._full_y[1]
                band_h = (top - self._full_y[0]) * 0.040
                for i, (s0, s1, key, label) in enumerate(
                        [s for s in segs if s[2] in _PHASE_STYLE]):
                    color, short = _PHASE_STYLE[key][1], _PHASE_STYLE[key][3]
                    xpos = float(min(s0 + 4, max(s0, s1 - 1)))
                    grow = (i % 3)
                    ypos = top - grow * band_h - band_h * 0.45
                    self._text(plot, xpos, ypos,
                               f" {short}·{label}  {i + 1} ",
                               theme.C_TEXT, anchor=(0, 1), delta=1, bold=True,
                               fill=_brush_alpha(color, 0.95), layer="phase")
                    verdict = self._fb_verdicts.get((key, s0, s1)) if hasattr(
                        self, "_fb_verdicts") else None
                    if verdict:
                        v, src = verdict
                        ok = v == "correct"
                        tag = "✓ 正确" if ok else "✗ 错误"
                        if src == "auto":
                            tag += "(自动)"
                        xv = float(min(s1 - 2, max(s1, s0 + 1)))
                        self._text(plot, xv, ypos, f" {tag} ",
                                   theme.C_TEXT, anchor=(1, 1), delta=1, bold=True,
                                   fill=_brush_alpha(
                                       theme.C_UP if ok else theme.C_DOWN, 0.95),
                                   layer="phase")

        candle = CandlestickItem(
            x, df["open"].values, df["close"].values,
            df["low"].values, df["high"].values)
        plot.addItem(candle)
        self._candle_item = candle

        legend = plot.addLegend(offset=(12, 8))
        for col, color, label in _MA_LINES:
            if col in df.columns:
                curve = plot.plot(x, df[col].values,
                                  pen=_pen(color, 1), connect="finite")
                legend.addItem(curve, label)
                self._layer_items.setdefault("ma", []).append(curve)
        if "boll_up" in df.columns and "boll_dn" in df.columns:
            bu = plot.plot(x, df["boll_up"].values,
                           pen=_pen(theme.C_UP, 0.8, Qt.PenStyle.DashLine),
                           connect="finite")
            bd = plot.plot(x, df["boll_dn"].values,
                           pen=_pen(theme.C_DOWN, 0.8, Qt.PenStyle.DashLine),
                           connect="finite")
            legend.addItem(bu, "BOLL上轨")
            legend.addItem(bd, "BOLL下轨")
            self._layer_items.setdefault("ma", []).extend([bu, bd])
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

        for p in pivots[-4:]:
            line = pg.InfiniteLine(
                pos=p["price"], angle=0,
                pen=_pen(theme.C_DOWN if p["type"] == "low" else theme.C_UP,
                         0.7, Qt.PenStyle.DashLine))
            self._add(plot, line, "pivot")

        x_end = self._n - 1
        if tr:
            plot.addItem(pg.InfiniteLine(
                pos=tr["top"], angle=0,
                pen=_pen(theme.C_UP, 1.0, Qt.PenStyle.DashDotLine)))
            plot.addItem(pg.InfiniteLine(
                pos=tr["bottom"], angle=0,
                pen=_pen(theme.C_DOWN, 1.0, Qt.PenStyle.DashDotLine)))
            self._text(plot, x_end, tr["top"], f" TR上轨 {tr['top']:.2f}",
                       theme.C_UP, anchor=(1, 0.5), delta=-2)
            self._text(plot, x_end, tr["bottom"], f" TR下轨 {tr['bottom']:.2f}",
                       theme.C_DOWN, anchor=(1, 0.5), delta=-2)
        if profile:
            poc = profile["poc"]
            plot.addItem(pg.InfiniteLine(
                pos=poc, angle=0,
                pen=_pen(theme.C.get("poc", theme.C_AMBER), 1.2, Qt.PenStyle.DotLine)))
            self._text(plot, x_end, poc, f" POC {poc:.2f}",
                       theme.C.get("poc", theme.C_AMBER), anchor=(1, 0.5), delta=-2)

        if draw_waves and len(waves) >= 2:
            self._draw_waves(plot, waves)
        self._draw_events(plot, events)
        if vsa_signals:
            self._draw_vsa(plot, df, vsa_signals)
        if locks or any(e["type"] in ("UTAD", "BC", "UT", "SOW", "LPSY") for e, _s, _d in events):
            self._draw_locks(plot, events, locks)
        if news_markers:
            self._draw_news(plot, news_markers)

        if sector and sector.get("name") and sector.get("main20") is not None:
            s20 = sector["main20"] / 1e8
            sc = theme.C_UP if s20 >= 0 else theme.C_DOWN
            self._text(plot, x_end, self._full_y[1],
                       f"板块 {sector['name']} · 近20日主力 {s20:+.2f}亿",
                       sc, anchor=(1, 1), delta=-1, bold=True,
                       fill=_brush_alpha(theme.C_PANEL, 0.9))

        # 最新收盘价: 水平虚线 + 右侧留白区的价格标签 (红涨绿跌)
        if self._n >= 1:
            c_now = float(df["close"].iloc[-1])
            c_prev = (float(df["close"].iloc[-2])
                      if self._n >= 2 else c_now)
            lcol = theme.C_UP if c_now >= c_prev else theme.C_DOWN
            line = pg.InfiniteLine(pos=c_now, angle=0,
                                   pen=_pen(lcol, 0.9,
                                            Qt.PenStyle.DashLine))
            line.setZValue(-5)
            plot.addItem(line)
            tag = pg.TextItem(f"{c_now:.2f}", color=theme.C_TEXT,
                              anchor=(1, 0.5), fill=pg.mkBrush(lcol))
            tag.setFont(self._font(-1, bold=True))
            tag.setPos(self._full_x[1], c_now)
            plot.addItem(tag)

    def _draw_waves(self, plot, waves):
        ymin, ymax = self._full_y
        off = (ymax - ymin) * 0.04
        for i in range(len(waves) - 1):
            w0, w1 = waves[i], waves[i + 1]
            x0, y0 = w0[0], w0[1]
            x1, y1 = w1[0], w1[1]
            up = y1 >= y0
            col = theme.C_UP if up else theme.C_DOWN
            tag = "上升浪" if up else "下跌浪"
            curve = pg.PlotCurveItem([x0, x1], [y0, y1], pen=_pen(col, 1.4))
            self._add(plot, curve, "waves")
            ang = math.degrees(math.atan2(-(y1 - y0), x1 - x0)) if x1 != x0 else 0
            arrow = pg.ArrowItem(pos=(x1, y1), angle=ang, tipAngle=25,
                                 tailLen=10, headLen=8, tailWidth=1.4,
                                 headWidth=1.4, pen=_pen(col),
                                 brush=pg.mkBrush(col))
            self._add(plot, arrow, "waves")
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            yt = my + (off if up else -off)
            self._text(plot, mx, yt, f"{tag} {y0:.2f}→{y1:.2f}", col,
                       bold=True, delta=-2,
                       fill=_brush_alpha(theme.C_PANEL, 0.7), layer="waves")
        for i, wpt in enumerate(waves):
            wx, wy = wpt[0], wpt[1]
            wlabel = wpt[2] if len(wpt) >= 3 else str(i + 1)
            wave_color = theme.C_ACCENT
            dot = pg.PlotDataItem(
                [wx], [wy], symbol="o", symbolSize=5,
                symbolPen=_pen(wave_color), symbolBrush=pg.mkBrush(wave_color))
            self._add(plot, dot, "waves")
            self._text(plot, wx + 1.5, wy, str(wlabel), wave_color,
                       anchor=(0, 0.5), delta=-1, bold=True, layer="waves")

    def _draw_events(self, plot, events):
        for e, sign, dy in events:
            col = e["color"]
            label = str(e["type"])
            ix, price = e["idx"], e["price"]
            ty = price + sign * dy
            stem = pg.PlotCurveItem([ix, ix], [price, ty], pen=_pen(col, 0.8))
            dot = pg.PlotDataItem(
                [ix], [price], symbol="o", symbolSize=4,
                symbolPen=_pen(col), symbolBrush=pg.mkBrush(col))
            ti = pg.TextItem(label, color=col, anchor=(0.5, 0.5))
            ti.setFont(self._font(-2, bold=True))
            ti.setPos(ix, ty)
            ti.ev_label = label
            ti.ev_conf = e.get("conf")
            # 悬停预览: 类型中文名 + 置信度 (点击弹窗看完整解释)
            tip = f"{label} {EVENT_CN.get(label, '')}".strip()
            conf = e.get("conf")
            if isinstance(conf, (int, float)):
                tip += f"\n置信度 {int(conf)}/100"
            ti.setToolTip(tip)
            self._add(plot, stem, "events")
            self._add(plot, dot, "events")
            self._add(plot, ti, "events")

    def _draw_vsa(self, plot, df, vsa_signals):
        ymin, ymax = self._full_y
        try:
            from wyckoff.vsa_explain import explain as _vsa_explain
        except Exception:
            _vsa_explain = None
        for s in vsa_signals:
            if s["label"] not in _VSA_DRAW:
                continue
            ix = s["idx"]
            if ix >= self._n:
                continue
            col = s["color"]
            lab = s["label"]
            ly = float(df["low"].iloc[ix])
            y_off = (ymax - ymin) * 0.025
            ty = ly - y_off
            stem = pg.PlotCurveItem([ix, ix], [ly, ty], pen=_pen(col, 0.4))
            ti = pg.TextItem(lab, color=col, anchor=(0.5, 0))
            ti.setFont(self._font(-4, bold=True))
            ti.setPos(ix, ty)
            ti.ev_label = lab
            ti.ev_conf = None
            # 悬停预览: 一句话含义 (来自 VSA_EXPLAIN.meaning)
            tip = ""
            if _vsa_explain is not None:
                try:
                    ex = _vsa_explain(lab) or {}
                    tip = str(ex.get("meaning", "")).strip()
                except Exception:
                    tip = ""
            desc = str(s.get("desc") or "").strip()
            if desc:
                tip = f"{desc}\n{tip}" if tip else desc
            if tip:
                ti.setToolTip(f"{lab} {tip}"[:300])
            self._add(plot, stem, "vsa")
            self._add(plot, ti, "vsa")

    def _draw_news(self, plot, markers):
        """新闻情绪图层: 按发布日画竖直点线 (红=偏多/绿=偏空),
        悬停显示标题+情绪分+价格验证结论; 与威科夫事件直观对齐。"""
        ymin, ymax = self._full_y
        for m in markers:
            ix = int(m.get("idx", -1))
            if ix < 0 or ix >= self._n:
                continue
            score = float(m.get("score", 0.0) or 0.0)
            col = theme.C_UP if score > 0 else theme.C_DOWN
            ln = pg.InfiniteLine(pos=float(ix), angle=90, movable=False,
                                 pen=_pen(col, 0.7, Qt.PenStyle.DotLine))
            ln.setZValue(-10)
            tip = f"{m.get('src', '')} {m.get('title', '')}\n情绪 {score:+.2f}"
            v = m.get("valid")
            if v == "confirmed":
                tip += "\n✓ 后续量价确认 (已加权)"
            elif v == "rejected":
                tip += "\n✗ 市场证伪·借利好派发嫌疑 (已降权)"
            ln.setToolTip(tip)
            self._add(plot, ln, "news")

    def _draw_locks(self, plot, events, locks):
        sell_types = {"UTAD", "BC", "UT", "SOW", "LPSY"}
        defs = [(lx, ly, str(lno), True) for lx, ly, lno in locks]
        defs += [(e["idx"], e["price"], "", False)
                 for e, _s, _d in events
                 if e["type"] in sell_types and e["idx"] >= self._n - W_RECENT]
        defs.sort(key=lambda d: d[0])
        if not defs:
            return
        ymin, ymax = self._full_y
        mid = (ymin + ymax) / 2
        base_dy = (ymax - ymin) * 0.06
        for lx, ly, label, is_buy in defs:
            sign = 1 if ly >= mid else -1
            ly2 = ly - sign * base_dy * 0.7
            color = theme.C_UP if is_buy else theme.C_DOWN
            dark = theme.C["up_dark"] if is_buy else theme.C["down_dark"]
            if label:
                ti = pg.TextItem(label, color=theme.C_TEXT, anchor=(0, 0.5),
                                 border=_pen(dark, 1), fill=pg.mkBrush(color))
                ti.setFont(self._font(-2, bold=True))
                ti.setPos(lx + 0.5, ly2)
                self._add(plot, ti, "locks")
            else:
                sym = pg.PlotDataItem(
                    [lx], [ly2], symbol="d", symbolSize=9,
                    symbolPen=_pen(dark), symbolBrush=pg.mkBrush(color))
                self._add(plot, sym, "locks")
            if label == "3":
                self._text(plot, lx + 3.0, ly2, "✓ 买点", theme.C_UP,
                           anchor=(0, 0.5), delta=-1, bold=True, layer="locks")

    # ── 成交量 ──
    def _build_volume(self, df, wave_segs):
        x = np.arange(self._n)
        plot = self.vol_plot
        vol = df["volume"].values.astype(float) / 1e4
        up = df["close"].values >= df["open"].values
        finite = np.isfinite(vol)
        xs = x[finite]
        heights = vol[finite]
        brushes = [pg.mkBrush(theme.C_UP if u else theme.C_DOWN) for u in up[finite]]
        plot.addItem(pg.BarGraphItem(x=xs, height=heights, width=0.6,
                                     brushes=brushes, pen=None))
        legend = plot.addLegend(offset=(12, 4))
        for col, color, label in _VOL_MA_LINES:
            if col in df.columns:
                curve = plot.plot(x, df[col].values / 1e4,
                                  pen=_pen(color, 0.8), connect="finite")
                legend.addItem(curve, label)
        legend.setLabelTextColor(pg.mkColor(theme.C_TEXT))

        if "vol_ratio_20" in df.columns and self._n:
            _vr = float(df["vol_ratio_20"].iloc[-1])
            _vc = theme.C_UP if _vr >= 1.2 else theme.C_DOWN if _vr <= 0.8 else theme.C_MUTED
            ti = pg.TextItem(f"量比 {_vr:.2f}", color=_vc, anchor=(1, 1),
                             fill=_brush_alpha(theme.C_PANEL, 0.85))
            ti.setFont(self._font(-1, bold=True))
            ti.setPos(self._n - 1, self._full_vol[1])
            plot.addItem(ti)

        for a, _b, _d in wave_segs[1:]:
            for p in (self.vol_plot, self.cum_plot):
                p.addItem(pg.InfiniteLine(
                    pos=a, angle=90,
                    pen=_pen(theme.C_MUTED, 0.7, Qt.PenStyle.DotLine)))

        plot.setYRange(*self._full_vol, padding=0)
        plot.setXRange(*self._full_x, padding=0)

    # ── 波段累计量 ──
    def _build_cum(self, df, wave_cum, wave_segs, caption):
        x = np.arange(self._n)
        plot = self.cum_plot
        volume = df["volume"].values.astype(float)
        cum = np.asarray(wave_cum, dtype=float) if wave_cum is not None \
            else np.zeros(self._n)
        if wave_segs:
            cmax = float(np.nanmax(np.abs(cum)))
            scale = cmax if cmax > 0 else 1.0
            cum_norm = cum / scale * 100
            for a, b, direction in wave_segs:
                xs = x[a:b + 1]
                ys = cum_norm[a:b + 1]
                wcol = theme.C_UP if direction > 0 else theme.C_DOWN if direction < 0 else theme.C_MUTED
                curve = plot.plot(xs, ys, pen=_pen(wcol, 1.0))
                zero = pg.PlotDataItem(xs, np.zeros_like(xs))
                plot.addItem(pg.FillBetweenItem(curve, zero,
                                                brush=_brush_alpha(wcol, 0.22)))
                tot = float(volume[a:b + 1].sum()) / 1e4
                arrow = "↑" if direction > 0 else "↓" if direction < 0 else ""
                peak = float(np.nanmax(np.abs(ys))) if len(ys) else 0.0
                y_txt = peak + 6 if direction >= 0 else -(peak + 6)
                self._text(plot, (a + b) / 2, y_txt,
                           f"{arrow}{tot:.0f}万手", wcol,
                           delta=-3, bold=True)
            plot.setYRange(-110, 110, padding=0)
        else:
            cmax = float(np.nanmax(np.abs(cum)))
            scale = cmax if cmax > 0 else 1.0
            cum_norm = cum / scale * 100
            curve = plot.plot(x, cum_norm, pen=_pen(theme.C_MUTED, 0.9))
            zero = pg.PlotDataItem(x, np.zeros_like(x))
            plot.addItem(pg.FillBetweenItem(curve, zero,
                                             brush=_brush_alpha(theme.C_MUTED, 0.25)))
            plot.setYRange(0, 100, padding=0)
        plot.setXRange(*self._full_x, padding=0)

        if caption:
            text, color = caption
            if text:
                plot.setLabel("bottom", text=text, color=color,
                              **{"font-size": f"{self._fs(1)}pt"})

    # ── 视图 / 交互 ──
    def _fs(self, delta=0):
        return max(6, self._font_size + delta)

    def _font(self, delta=0, bold=False):
        f = QtGui.QFont()
        f.setFamily(FONT_CANDIDATES[0])
        f.setPointSize(self._fs(delta))
        f.setBold(bold)
        return f

    def _text(self, plot, x, y, text, color, anchor=(0.5, 0.5),
              delta=0, bold=False, fill=None, layer=None):
        kwargs = {}
        if fill is not None:
            kwargs["fill"] = fill
        ti = pg.TextItem(text, color=color, anchor=anchor, **kwargs)
        ti.setFont(self._font(delta, bold))
        ti.setPos(float(x), float(y))
        self._add(plot, ti, layer)
        return ti

    def hit_label(self, scene_pos):
        for item in self.scene().items(scene_pos):
            if isinstance(item, pg.TextItem):
                label = getattr(item, "ev_label", None)
                if label:
                    return label, getattr(item, "ev_conf", None)
        return None

    # ── 测量尺 (Shift+左键拖拽) ──
    def _measure_target_ok(self, vb):
        """测量尺仅在价格面板生效。"""
        return self._has_data and vb is self.price_plot.getViewBox()

    def _measure_update(self, sp0, sp1, final=False):
        pvb = self.price_plot.getViewBox()
        try:
            a = pvb.mapSceneToView(QtCore.QPointF(sp0))
            b = pvb.mapSceneToView(QtCore.QPointF(sp1))
        except Exception:
            return
        x0, y0 = float(a.x()), float(a.y())
        x1, y1 = float(b.x()), float(b.y())
        if self._measure_items is None:
            curve = pg.PlotCurveItem(
                pen=_pen(theme.C_ACCENT, 1.2, Qt.PenStyle.DashLine))
            tag = pg.TextItem("", color=theme.C_TEXT, anchor=(0.5, 0.5),
                              fill=_brush_alpha(theme.C_PANEL, 0.94))
            tag.setFont(self._font(-1, bold=True))
            tag.setZValue(60)
            for it in (curve, tag):
                self.price_plot.addItem(it, ignoreBounds=True)
            self._measure_items = (curve, tag)
        curve, tag = self._measure_items
        curve.setData([x0, x1], [y0, y1])
        chg = (y1 - y0) / y0 * 100 if y0 else 0.0
        arrow = "▲" if y1 >= y0 else "▼"
        bars = int(round(abs(x1 - x0)))
        days = ""
        i_a, i_b = int(round(min(x0, x1))), int(round(max(x0, x1)))
        if (self._days and 0 <= i_a < len(self._days)
                and 0 <= i_b < len(self._days)):
            try:
                days = (f" · {self._days[i_a].strftime('%m-%d')}"
                        f"→{self._days[i_b].strftime('%m-%d')}")
            except Exception:
                days = ""
        tag.setText(f"{arrow} {y0:.2f} → {y1:.2f}   {chg:+.2f}%"
                    f" · {bars}根{days}")
        off = (self._full_y[1] - self._full_y[0]) * 0.03
        tag.setPos((x0 + x1) / 2, max(y0, y1) + off)

    def _measure_clear(self):
        if not getattr(self, "_measure_items", None):
            return
        for it in self._measure_items:
            try:
                self.price_plot.removeItem(it)
            except Exception:
                pass
        self._measure_items = None

    def clear_measure(self):
        """Esc 清除测量尺。"""
        had = bool(getattr(self, "_measure_items", None))
        self._measure_clear()
        return had

    def reset_view(self):
        """复位全幅并清除测量尺。"""
        super().reset_view()
        self._measure_clear()

    # ── OHLC 信息条 ──
    def _update_info(self, idx):
        """十字光标/键盘步进驱动: 刷新图上方 OHLC 摘要 (主窗口注入 info_bar)。"""
        lab = getattr(self, "info_bar", None)
        if lab is None:
            return
        html = self._ohlc_html(idx)
        if html:
            lab.setText(html)

    def _ohlc_html(self, idx):
        df = getattr(self, "_df_ref", None)
        if df is None or not len(df):
            return ""
        i = int(round(idx))
        if not (0 <= i < len(df)):
            return ""
        o = float(df["open"].iloc[i])
        h = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])
        prev = float(df["close"].iloc[i - 1]) if i > 0 else o
        pct = (c - prev) / prev * 100 if prev else 0.0
        up = c >= prev
        col = theme.C_UP if up else theme.C_DOWN
        muted = theme.C_MUTED
        dstr = ""
        if 0 <= i < len(self._days):
            try:
                d = self._days[i]
                fmt = "%m-%d %H:%M" if self._is_minute else "%Y-%m-%d"
                dstr = d.strftime(fmt)
            except Exception:
                dstr = str(self._days[i])
        vol = float(df["volume"].iloc[i]) / 1e4
        mas = []
        for col_name, mcolor, mlabel in _MA_LINES:
            if col_name in df.columns and np.isfinite(df[col_name].iloc[i]):
                mas.append(f'<span style="color:{mcolor};">{mlabel}'
                           f' {float(df[col_name].iloc[i]):.2f}</span>')
        arrow = "▲" if pct >= 0 else "▼"
        parts = [
            f'<span style="color:{muted};">{dstr}</span>',
            f"开{o:.2f} 高{h:.2f} 低{low:.2f} 收<b>{c:.2f}</b>",
            f'<b style="color:{col};">{arrow}{pct:+.2f}%</b>',
            f'<span style="color:{muted};">量</span>{vol:.0f}万手',
        ]
        parts.extend(mas)
        return "&nbsp;&nbsp;".join(parts)

    # ── 视图范围存取 (主窗口视图记忆用) ──
    def view_range(self):
        if not self._has_data or self.price_plot is None:
            return None
        return tuple(self.price_plot.getViewBox().viewRange()[0])

    def zoom_x_about(self, cx, factor):
        """兼容旧接口: 以数据坐标 cx 为锚点缩放 X (基类实现)。"""
        self.zoom_about(cx, factor)

    def on_x_range_changed(self, x0, x1, source_vb=None):
        """基类联动钩子: 价格面板 Y 随可见区间自适应。"""
        if self._n:
            self._rescale_price_y(x0, x1)

    def _rescale_price_y(self, x0, x1):
        vb = self.price_plot.getViewBox()
        full_span = self._full_x[1] - self._full_x[0]
        if x1 - x0 >= full_span - 0.5:
            vb.setYRange(self._full_y[0], self._full_y[1], padding=0)
            return
        i0 = max(0, int(math.floor(x0)))
        i1 = min(self._n - 1, int(math.ceil(x1)))
        if i0 > i1 or i1 < 0 or i0 >= self._n:
            vb.setYRange(self._full_y[0], self._full_y[1], padding=0)
            return
        lo = float(np.min(self._chart_lo[i0:i1 + 1]))
        hi = float(np.max(self._chart_hi[i0:i1 + 1]))
        if hi - lo < 1e-9:
            return
        pad = (hi - lo) * 0.04
        y0 = max(self._full_y[0], lo - pad)
        y1 = min(self._full_y[1], hi + pad)
        if y1 - y0 > 1e-9:
            vb.setYRange(y0, y1, padding=0)

    def grab_pixmap(self):
        """整图快照 (供导出 PNG)。"""
        return self.grab()
