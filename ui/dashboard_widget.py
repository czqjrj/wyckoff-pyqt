"""大盘仪表盘 UI 控件 — 主要指数/大盘走势/市场广度/多指数对比/板块资金/威科夫阶段/资金流。

数据由 worker 线程通过 set_data(dict) 注入 (与 ind_widget/mkt_widget 同模式),
主线程只做渲染; 控件本身不发任何网络请求。

视觉: 全卡片化 — 面板统一为 Card(panel 背景+圆角), 小节用 PanelHeader 命名,
指数卡带方向色左边条与 MA 状态胶囊, 市场广度用按比例红绿分段条, 指标用彩色胶囊。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
)

from . import theme
from .components.card import Card
from .components.panel_header import PanelHeader
from .dash_charts import IndexCompareChart, SectorFlowChart, SseKlineChart

# ── 工具 ──


def _font(size_pt, bold=False):
    f = QFont()
    f.setFamily(theme.ui_font_family())
    f.setPointSize(size_pt)
    f.setBold(bold)
    return f


def _mono_font(size_pt, bold=False):
    f = QFont()
    f.setFamily(theme.mono_font_family())
    f.setPointSize(size_pt)
    f.setBold(bold)
    return f


def _label(text="", size_pt=11, bold=False, color="", mono=False,
           align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft):
    lab = QLabel(text)
    lab.setAlignment(align)
    lab.setFont(_mono_font(size_pt, bold) if mono else _font(size_pt, bold))
    lab.setStyleSheet(f"color:{color};background:transparent;" if color
                      else "background:transparent;")
    return lab


def _pill(text="", fg="", bg="", pt=8, bold=True):
    """胶囊徽标: 圆角背景 + 前景文字。"""
    lab = _label(text, pt, bold, fg)
    lab.setStyleSheet(
        f"color:{fg};background:{bg};border-radius:{theme.radius('full')}px;"
        f"padding:2px 10px;")
    return lab


def _card_style():
    return (f"QFrame#card {{ background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER};"
            f"border-radius:{theme.radius('md')}px; }}")


def _phase_color(phase_base):
    """威科夫阶段 → 强调色 (随 A股红涨绿跌惯例, 吸筹/派发用中性警示色)。"""
    m = {
        "吸筹": theme.C_ACCENT,  # 蓝色: 筑底
        "拉升": theme.C_UP,      # 红: 上行
        "派发": theme.C_AMBER,   # 橙: 警示/滞涨
        "下跌": theme.C_DOWN,    # 绿: 下行
        "牛市": theme.C_UP,
        "熊市": theme.C_DOWN,
        "震荡": theme.C_AMBER,
    }
    return m.get(phase_base, theme.C_TEXT)


def _env_color(tone):
    m = {"bullish": theme.C_UP, "bearish": theme.C_DOWN, "neutral": theme.C_MUTED}
    return m.get(tone, theme.C_TEXT)


def _pct_color(pct):
    if pct > 0:
        return theme.C_UP
    elif pct < 0:
        return theme.C_DOWN
    return theme.C_TEXT


def _tint(color, alpha=38):
    """给主题色做半透明底 (rgba)。"""
    return theme.css_rgba(color, alpha)


# ── 卡片构建 ──


class _IndexCard(QFrame):
    """指数卡片: 方向色左边条 + 名称 + MA胶囊 + 大号价格 + 涨跌/涨跌幅。

    卡片风格在 apply_theme / set_data 时按当前主题重建, 支持深浅主题切换。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("idxCard")
        self.setMinimumHeight(104)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._DIR = None
        self._last = None

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 8)
        v.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(6)
        self._name = _label("--", 12, True, theme.C_TEXT)
        top.addWidget(self._name)
        top.addStretch(1)
        self._ma_pill = _pill("--", theme.C_MUTED, _tint(theme.C_MUTED, 26))
        top.addWidget(self._ma_pill)
        v.addLayout(top)

        self._price = _label("--", 22, True, theme.C_TEXT, mono=True)
        self._price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._price)

        bot = QHBoxLayout()
        bot.setSpacing(8)
        bot.addStretch(1)
        self._chg = _label("", 11, False, "", mono=True)
        self._pct = _label("", 12, True, "", mono=True)
        bot.addWidget(self._chg)
        bot.addWidget(self._pct)
        bot.addStretch(1)
        v.addLayout(bot)

    def _paint_direction(self, pct):
        """按涨跌方向涂色: 左边条 + 价格 + 背景微染 + hover 效果。"""
        c = _pct_color(pct)
        self.setStyleSheet(
            f"QFrame#idxCard{{background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER};border-left:3px solid {c};"
            f"border-radius:{theme.radius('md')}px;"
            f"background-color:{_tint(c, 14)};}}"
            f"QFrame#idxCard:hover{{border:1px solid {c};"
            f"border-left:3px solid {c};background-color:{_tint(c, 30)};}}")
        self._price.setStyleSheet(
            f"color:{c};background:transparent;font-weight:bold;")
        self._chg.setStyleSheet(f"color:{c};background:transparent;")
        self._pct.setStyleSheet(f"color:{c};background:transparent;")
        self._DIR = pct

    def set_data(self, sym, rt, tech):
        self._last = (sym, rt, tech)
        name = rt.get("name", "") if rt else (tech.get("name", "") if tech else "")
        self._name.setText(name or sym)
        self._name.setStyleSheet(f"color:{theme.C_TEXT};background:transparent;")

        price = rt.get("price", 0) if rt else 0
        chg = rt.get("price", 0) - rt.get("prev_close", 0) if rt else 0
        pct = rt.get("pct", 0) if rt else 0

        if price and price > 0:
            self._price.setText(f"{price:,.2f}")
        else:
            self._price.setText("--")
        self._chg.setText(f"{chg:+.2f}" if chg else "")
        self._pct.setText(f"{pct:+.2f}%" if pct else "")
        self._paint_direction(pct)

        # MA 状态胶囊: 多排/空排/交叉
        ma_info = ""
        if tech:
            ma20 = tech.get("ma20")
            ma50 = tech.get("ma50")
            if price and ma20 and ma50:
                if price > ma20 > ma50:
                    ma_info = "多排"
                elif price < ma20 < ma50:
                    ma_info = "空排"
                else:
                    ma_info = "交叉"
        if ma_info:
            mc = theme.C_UP if "多" in ma_info else theme.C_DOWN if "空" in ma_info else theme.C_AMBER
            self._ma_pill.setText(ma_info)
            self._ma_pill.setStyleSheet(
                f"color:{mc};background:{_tint(mc, 26)};"
                f"border-radius:{theme.radius('full')}px;padding:2px 10px;")
        else:
            self._ma_pill.setText("")
            self._ma_pill.setStyleSheet("background:transparent;")


class _RatioBar(QFrame):
    """市场广度比例条: 上涨(红)/平(灰)/下跌(绿) 按数量比例分段。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ratioBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self._up = QFrame()
        self._flat = QFrame()
        self._down = QFrame()
        for w, o in ((self._up, "ratioUp"), (self._flat, "ratioFlat"),
                     (self._down, "ratioDown")):
            w.setObjectName(o)
            lay.addWidget(w)
        self.setFixedHeight(12)
        self._lay = lay
        self.set_data(0, 0, 0)

    def set_data(self, up, down, flat):
        segs = {"ratioUp": self._up, "ratioFlat": self._flat,
                "ratioDown": self._down}
        colors = {"ratioUp": theme.C_UP, "ratioFlat": theme.C_MUTED,
                  "ratioDown": theme.C_DOWN}
        for obj, w in segs.items():
            w.setStyleSheet(
                f"QFrame#{obj}{{background:{colors[obj]};"
                f"border-radius:{theme.radius('full')}px;}}")
        # 各段拉伸权重成正比, 0 → 不占宽 (拉伸权重仍≥1 保证可见)
        total = max(1, up + down + flat)
        self._lay.setStretch(0, max(1, round(up * 20 / total)))
        self._lay.setStretch(1, max(1, round(flat * 20 / total)))
        self._lay.setStretch(2, max(1, round(down * 20 / total)))


class _BreadthCard(Card):
    """市场广度卡片: 大数字 + 比例条 + 涨停/跌停胶囊 + 涨跌比。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        header = PanelHeader("市场广度")
        self._ts_label = _label("", 9, False, theme.C_MUTED)
        header.add_action(self._ts_label)
        super().add_widget(header)

        nums = QHBoxLayout()
        nums.setSpacing(18)
        up_lab = _label("上涨", 10, True, theme.C_MUTED)
        self._up_val = _label("--", 20, True, theme.C_UP, mono=True)
        flat_lab = _label("平盘", 10, True, theme.C_MUTED)
        self._flat_val = _label("--", 20, True, theme.C_MUTED, mono=True)
        down_lab = _label("下跌", 10, True, theme.C_MUTED)
        self._down_val = _label("--", 20, True, theme.C_DOWN, mono=True)
        nums.addWidget(up_lab)
        nums.addWidget(self._up_val)
        nums.addStretch(1)
        nums.addWidget(flat_lab)
        nums.addWidget(self._flat_val)
        nums.addStretch(1)
        nums.addWidget(down_lab)
        nums.addWidget(self._down_val)
        super().add_layout(nums)

        self._ratio_bar = _RatioBar()
        super().add_widget(self._ratio_bar)

        chips = QHBoxLayout()
        chips.setSpacing(10)
        self._lu_pill = _pill("涨停 --", theme.C_UP, _tint(theme.C_UP, 22))
        self._ld_pill = _pill("跌停 --", theme.C_DOWN, _tint(theme.C_DOWN, 22))
        chips.addWidget(self._lu_pill)
        chips.addWidget(self._ld_pill)
        chips.addStretch(1)
        self._ratio_pill = _pill("涨跌比 --", theme.C_MUTED, _tint(theme.C_MUTED, 22))
        chips.addWidget(self._ratio_pill)
        super().add_layout(chips)

    def set_data(self, breadth):
        up = breadth.get("up", 0)
        down = breadth.get("down", 0)
        flat = breadth.get("flat", 0)
        self._up_val.setText(str(up))
        self._down_val.setText(str(down))
        self._flat_val.setText(str(flat))
        self._up_val.setStyleSheet(f"color:{theme.C_UP};background:transparent;")
        self._down_val.setStyleSheet(f"color:{theme.C_DOWN};background:transparent;")
        self._flat_val.setStyleSheet(f"color:{theme.C_MUTED};background:transparent;")
        self._ratio_bar.set_data(up, down, flat)

        lu = breadth.get("limit_up", 0)
        ld = breadth.get("limit_down", 0)
        self._set_pill(self._lu_pill, f"涨停 {lu} 只" if lu else "涨停 --",
                       theme.C_UP if lu else theme.C_MUTED)
        self._set_pill(self._ld_pill, f"跌停 {ld} 只" if ld else "跌停 --",
                       theme.C_DOWN if ld else theme.C_MUTED)
        ratio = up / down if down > 0 else (999 if up > 0 else 0)
        self._set_pill(self._ratio_pill, f"涨跌比 {ratio:.1f}", theme.C_MUTED)

        ts = breadth.get("ts", 0)
        if ts:
            from datetime import datetime
            t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            self._ts_label.setText(f"更新 {t}")

    @staticmethod
    def _set_pill(lab, text, color):
        lab.setText(text)
        lab.setStyleSheet(
            f"color:{color};background:{_tint(color, 22)};"
            f"border-radius:{theme.radius('full')}px;padding:2px 10px;")


# ── 主控件 ──


class DashboardWidget(QWidget):
    """大盘仪表盘: 全卡片化组合, 数据由 set_data() 注入。"""

    load_code = pyqtSignal(str)  # 双击指数 → 加载分析

    def __init__(self, parent=None, on_load=None, font_size=11):
        super().__init__(parent)
        self._font_size = font_size
        self._on_load = on_load
        self._idx_cards: dict[str, _IndexCard] = {}
        self._last_data = None
        self._cards: list[Card] = []
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                             "QScrollArea>QWidget>QWidget{background:transparent;}")
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        self._root = QVBoxLayout(container)
        self._root.setContentsMargins(12, 8, 12, 12)
        self._root.setSpacing(12)

        self._build_index_section()
        self._build_kline_section()
        self._build_breadth_section()
        self._build_charts_section()
        self._build_fundamentals_section()
        self._build_sector_section()

        self._root.addStretch(1)
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _new_card(self):
        card = Card()
        card.setStyleSheet(_card_style())
        self._cards.append(card)
        return card

    # ── 主要指数 ──
    def _build_index_section(self):
        card = self._new_card()
        header = PanelHeader("主要指数")
        self._env_label = _label("", 11, True, "")
        header.add_action(self._env_label)
        card.layout_.insertWidget(0, header)

        grid = QGridLayout()
        grid.setSpacing(10)
        from wyckoff.market_dashboard import DASH_INDICES
        for i, idx_info in enumerate(DASH_INDICES):
            card_w = _IndexCard()
            card_w.mousePressEvent = lambda e, c=idx_info["code"]: self.load_code.emit(c)
            r, c_ = divmod(i, 3)
            grid.addWidget(card_w, r, c_)
            self._idx_cards[idx_info["code"]] = card_w
        card.layout_.addLayout(grid)
        self._root.addWidget(card)

    # ── 大盘走势图 ──
    def _build_kline_section(self):
        card = self._new_card()
        self._sse_chart = SseKlineChart()
        card.add_widget(self._sse_chart)
        self._root.addWidget(card)

    # ── 市场广度 ──
    def _build_breadth_section(self):
        self._breadth_card = _BreadthCard()
        self._breadth_card.setStyleSheet(_card_style())
        self._root.addWidget(self._breadth_card)

    # ── 多指数对比 + 板块资金 (双图卡) ──
    def _build_charts_section(self):
        card = self._new_card()
        two = QHBoxLayout()
        two.setSpacing(24)
        self._index_compare = IndexCompareChart()
        two.addWidget(self._index_compare, 1)
        self._sector_flow = SectorFlowChart()
        two.addWidget(self._sector_flow, 1)
        card.add_layout(two)
        self._root.addWidget(card)

    # ── 威科夫阶段 + 资金流向 (两列卡片) ──
    def _build_fundamentals_section(self):
        two_col = QHBoxLayout()
        two_col.setSpacing(12)

        # 左: 威科夫阶段
        left = self._new_card()
        left.layout_.addWidget(PanelHeader("威科夫阶段"))
        self._phase_badge = _label("--", 15, True, theme.C_TEXT)
        self._phase_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._phase_badge.setStyleSheet(
            f"color:{theme.C_MUTED};background:{_tint(theme.C_MUTED, 18)};"
            f"border:1px solid {theme.C_MUTED};"
            f"border-radius:{theme.radius('full')}px;padding:6px 16px;")
        left.layout_.addWidget(self._phase_badge)
        self._phase_detail = _label("", 10, False, theme.C_MUTED, mono=True)
        self._phase_detail.setWordWrap(True)
        left.layout_.addWidget(self._phase_detail)
        self._phase_events = _label("", 10, False, theme.C_MUTED)
        self._phase_events.setWordWrap(True)
        left.layout_.addWidget(self._phase_events)
        left.layout_.addStretch(1)
        two_col.addWidget(left, 1)

        # 右: 资金流向 + 涨停池
        right = self._new_card()
        right.layout_.addWidget(PanelHeader("资金流向"))
        self._north_title = _label("北向资金", 10, True, theme.C_MUTED)
        right.layout_.addWidget(self._north_title)
        self._north_row = None
        self._north_total = _pill("合计 --", theme.C_MUTED, _tint(theme.C_MUTED, 22))
        right.layout_.addWidget(self._north_total)
        self._north_detail = _label("暂无数据", 10, False, theme.C_MUTED)
        self._north_detail.setWordWrap(True)
        right.layout_.addWidget(self._north_detail)
        right.layout_.addSpacing(6)
        self._zt_title = _label("涨停池", 10, True, theme.C_MUTED)
        right.layout_.addWidget(self._zt_title)
        self._zt_detail = _label("暂无数据", 10, False, theme.C_MUTED)
        self._zt_detail.setWordWrap(True)
        right.layout_.addWidget(self._zt_detail)
        right.layout_.addStretch(1)
        two_col.addWidget(right, 1)

        self._root.addLayout(two_col)

    # ── 板块轮动 ──
    def _build_sector_section(self):
        card = self._new_card()
        card.layout_.addWidget(PanelHeader("板块轮动"))

        self._sector_table = QTableWidget()
        self._sector_table.setColumnCount(4)
        self._sector_table.setHorizontalHeaderLabels(["板块", "涨跌幅", "资金流", "强度"])
        self._sector_table.horizontalHeader().setStretchLastSection(True)
        self._sector_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._sector_table.verticalHeader().setVisible(False)
        self._sector_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._sector_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._sector_table.setShowGrid(True)
        self._sector_table.setAlternatingRowColors(True)
        self._sector_table.setMinimumHeight(200)
        self._sector_table.setMaximumHeight(360)
        self._theme_table()
        card.add_widget(self._sector_table)
        self._root.addWidget(card)

    def _theme_table(self):
        self._sector_table.setStyleSheet(
            f"QTableWidget{{background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
            f"gridline-color:{theme.C_GRID};"
            f"alternate-background-color:{theme.semantic('zebra')};"
            f"selection-background-color:{theme.semantic('sel')};}}"
            f"QTableWidget::item{{padding:4px 6px;}}"
            f"QHeaderView::section{{background:{theme.semantic('header')};color:{theme.C_TEXT};"
            f"border:none;border-right:1px solid {theme.C_BORDER};"
            f"border-bottom:1px solid {theme.C_BORDER};padding:4px 6px;font-weight:bold;}}"
        )

    # ── 数据渲染 ──

    def set_data(self, data: dict):
        """注入仪表盘数据 (由 DashboardThread worker → 主线程调用)。"""
        if not data:
            return
        self._last_data = data
        indices = data.get("indices", {})
        tech = data.get("tech", {})
        breadth = data.get("breadth", {})
        sectors = data.get("sectors", [])
        north = data.get("north", [])
        zt_pool = data.get("zt_pool", [])
        market_env = data.get("market_env", {})

        # 指数卡片
        from wyckoff.market_dashboard import DASH_INDICES
        for idx_info in DASH_INDICES:
            sym = idx_info["code"]
            card = self._idx_cards.get(sym)
            if card:
                card.set_data(sym, indices.get(sym), tech.get(sym))

        # 大盘环境胶囊
        env_name = market_env.get("env", "")
        tone = market_env.get("tone", "")
        env_c = _env_color(tone)
        self._env_label.setText(f"大盘 {env_name}" if env_name else "")
        self._env_label.setStyleSheet(
            f"color:{env_c};background:transparent;font-weight:bold;")

        # 大盘走势 K线图 + 多指数对比 + 板块资金
        self._sse_chart.set_data(data.get("sse_chart"))
        self._index_compare.set_data(data.get("index_compare"))
        self._sector_flow.set_data(data.get("sector_flow"))

        # 市场广度
        if breadth:
            self._breadth_card.set_data(breadth)

        # 威科夫阶段
        sse_tech = tech.get("sh000001", {})
        phase_text = sse_tech.get("phase", "")
        if phase_text:
            base = phase_text.split(" ")[0]
            pc = _phase_color(base)
            self._phase_badge.setText(phase_text)
            self._phase_badge.setStyleSheet(
                f"color:{pc};background:{_tint(pc, 22)};"
                f"border:1px solid {_tint(pc, 120)};"
                f"border-radius:{theme.radius('full')}px;padding:6px 16px;")
        else:
            self._phase_badge.setText("暂无数据")
            self._phase_badge.setStyleSheet(
                f"color:{theme.C_MUTED};background:{_tint(theme.C_MUTED, 18)};"
                f"border:1px solid {theme.C_MUTED};"
                f"border-radius:{theme.radius('full')}px;padding:6px 16px;")
        events = sse_tech.get("events", [])
        if events:
            from wyckoff.config import EVENT_CN
            lines = []
            for e in events[:4]:
                etype = e.get("type", "")
                cn = EVENT_CN.get(etype, etype)
                conf = e.get("conf", 0)
                lines.append(f"● {cn} · 置信 {conf:.0f}%")
            self._phase_events.setText("\n".join(lines))
        else:
            self._phase_events.setText("")
        detail_parts = []
        ma20 = sse_tech.get("ma20")
        ma50 = sse_tech.get("ma50")
        ma200 = sse_tech.get("ma200")
        if ma20:
            detail_parts.append(f"MA20 {ma20:,.2f}")
        if ma50:
            detail_parts.append(f"MA50 {ma50:,.2f}")
        if ma200:
            detail_parts.append(f"MA200 {ma200:,.2f}")
        self._phase_detail.setText("  ·  ".join(detail_parts))

        # 北向资金
        if north:
            parts = []
            total_net = 0.0
            for n in north:
                net = n.get("net")
                mkt = n.get("market", "")
                if net is None:
                    continue
                total_net += net
                parts.append(f"{mkt} {net / 1e8:+.2f}亿")  # EM 单位: 元 → 亿
            nc = theme.C_UP if total_net > 0 else theme.C_DOWN if total_net < 0 else theme.C_MUTED
            self._north_total.setText(f"合计 {total_net / 1e8:+.2f}亿")
            self._north_total.setStyleSheet(
                f"color:{nc};background:{_tint(nc, 22)};"
                f"border-radius:{theme.radius('full')}px;padding:2px 10px;")
            self._north_detail.setText("\n".join(parts) if parts else "暂无数据")
            self._north_detail.setStyleSheet(f"color:{_tint(nc, 220)};background:transparent;")
        else:
            self._north_total.setText("合计 --")
            self._north_total.setStyleSheet(
                f"color:{theme.C_MUTED};background:{_tint(theme.C_MUTED, 22)};"
                f"border-radius:{theme.radius('full')}px;padding:2px 10px;")
            self._north_detail.setText("暂无数据")
            self._north_detail.setStyleSheet("color:%s;background:transparent;" % theme.C_MUTED)

        # 涨停池
        if zt_pool:
            max_board = max((z.get("limit_times", 1) for z in zt_pool), default=1)
            txt = f"最近交易日涨停 {len(zt_pool)} 只"
            if max_board >= 2:
                txt += f" · 最高连板 {max_board} 板"
        else:
            txt = "暂无数据"
        self._zt_detail.setText(txt)
        self._zt_detail.setStyleSheet("color:%s;background:transparent;" % theme.C_TEXT)

        # 板块轮动表格
        self._populate_sector_table(sectors)

    def _populate_sector_table(self, sectors):
        if not sectors:
            self._sector_table.setRowCount(0)
            return
        display = sectors[:20]  # top 20
        self._sector_table.setRowCount(len(display))
        for r, s in enumerate(display):
            name = s.get("name", "")
            pct = s.get("pct", 0)
            flow = s.get("flow20_yi", 0)
            tone = s.get("tone", "neutral")

            self._sector_table.setItem(r, 0, QTableWidgetItem(name))

            pct_item = QTableWidgetItem(f"{pct:+.2f}%")
            pct_item.setForeground(QColor(_pct_color(pct)))
            pct_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._sector_table.setItem(r, 1, pct_item)

            flow_str = f"{flow:+.2f}亿" if flow else "--"
            flow_item = QTableWidgetItem(flow_str)
            fc = theme.C_UP if flow > 0 else theme.C_DOWN if flow < 0 else theme.C_TEXT
            flow_item.setForeground(QColor(fc))
            flow_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._sector_table.setItem(r, 2, flow_item)

            tone_map = {
                "bullish": ("强势", theme.C_UP),
                "bearish": ("弱势", theme.C_DOWN),
                "mixed": ("分化", theme.C_AMBER),
                "neutral": ("平淡", theme.C_MUTED),
            }
            tone_text, tone_color = tone_map.get(tone, ("--", theme.C_TEXT))
            tone_item = QTableWidgetItem(tone_text)
            tone_item.setForeground(QColor(tone_color))
            tone_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._sector_table.setItem(r, 3, tone_item)

    def set_placeholder(self, msg="正在加载大盘数据 ..."):
        """占位提示 (分析中/失败)。"""
        self._phase_badge.setText(msg)
        self._phase_badge.setStyleSheet(
            f"color:{theme.C_MUTED};background:{_tint(theme.C_MUTED, 18)};"
            f"border:1px solid {theme.C_MUTED};"
            f"border-radius:{theme.radius('full')}px;padding:6px 16px;")
        self._phase_detail.setText("")
        self._phase_events.setText("")

    def apply_theme(self):
        """主题切换后重刷卡片/表格/图表样式。"""
        # 卡片边框
        for card in self._cards:
            card.setStyleSheet(_card_style())
        self._breadth_card.setStyleSheet(_card_style())
        self._theme_table()
        # 用当前主题色整卡重绘 (set_data 纯渲染, 无网络)
        if self._last_data:
            self.set_data(self._last_data)