"""主视图: 三栏布局 (自选股 | 图表标签页 | 结论) - 纯 UI 组合, 无业务逻辑。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .components import FlowLayout, PanelHeader, SectionList
from .constants import IND_ASPECT, MKT_ASPECT
from .ind_widget import IndScroll, IndWidget
from .kline_widget import KlineWidget
from .mkt_widget import MktWidget
from .news_widget import NewsWidget
from .pnf_widget import PnfWidget
from .watch_card import WatchCardDelegate


class _CanvasFrame(QFrame):
    """图表外层容器: 统一圆角/边框/背景。"""

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.setObjectName("canvasFrame")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        layout.addWidget(canvas)


def _action_btn(text, checkable=False, tooltip=""):
    """图表页头的小动作按钮 (与解读页 ttsBtn 同款样式)。"""
    b = QPushButton(text)
    b.setObjectName("ttsBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(22)
    b.setCheckable(checkable)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def _menu_btn(text, tooltip="", menu=None):
    """图表页头带下拉菜单的按钮。"""
    b = QPushButton(text)
    b.setObjectName("ttsBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(22)
    if menu is not None:
        b.setMenu(menu)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def _chart_hint(text):
    """图表页头的交互提示条 (灰字, 右缘)。"""
    lab = QLabel(text)
    lab.setObjectName("chartHint")
    return lab


class _LazyCalibContainer(QWidget):
    """校准中心懒加载容器。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._win = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._pending_methods = []

    def _ensure(self):
        if self._win is None:
            from .calibration_center import CalibrationCenter
            self._win = CalibrationCenter(None, settings=self._settings)
            self._layout.addWidget(self._win)
            while self._pending_methods:
                name, args, kwargs = self._pending_methods.pop(0)
                try:
                    getattr(self._win, name)(*args, **kwargs)
                except Exception:
                    pass
        return self._win

    def showEvent(self, ev):
        super().showEvent(ev)
        self._ensure()

    def _delegate(self, name, *args, **kwargs):
        if self._win is None:
            self._pending_methods.append((name, args, kwargs))
            return
        return getattr(self._win, name)(*args, **kwargs)

    def render_all(self):
        self._delegate("render_all")

    def begin_update(self):
        self._delegate("begin_update")

    def end_update(self):
        self._delegate("end_update")

    def refresh_sync_url(self):
        self._delegate("refresh_sync_url")

    def refresh_feedback(self, *args, **kwargs):
        self._delegate("refresh_feedback", *args, **kwargs)

    def apply_theme(self):
        # 刷新自身内联样式
        if hasattr(self, "kline_widget") and self.kline_widget.info_bar is not None:
            info = self.kline_widget.info_bar
            info.setStyleSheet(
                f"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
                "border-radius:4px;padding:3px 10px;font-weight:bold;"
            )
        # 委托子控件
        self._delegate("apply_theme")

    @property
    def tabs(self):
        win = self._ensure()
        return win.tabs


class ChartTabs(QTabWidget):
    """中央图表标签页容器。"""

    def __init__(self, font_size, on_load=None, parent=None):
        super().__init__(parent)
        self.setTabsClosable(True)
        self._font_size = font_size
        self._on_load = on_load
        self._build_tabs()

    def apply_theme(self):
        """主题切换时刷新标签栏样式。"""
        self.setStyleSheet(
            f"QTabBar::tab {{ background: {theme.C_PANEL}; color: {theme.C_TEXT}; "
            f"border: 1px solid {theme.C_BORDER}; padding: 6px 12px; }}"
            f"QTabBar::tab:selected {{ background: {theme.C_ACCENT}; color: {theme.C_PANEL}; }}"
            f"QTabBar::tab:hover {{ background: {theme.C.get('btn_hover', theme.C_BORDER)}; }}"
        )

    def _build_tabs(self):
        # K线图
        self.tab_kline = QWidget()
        self._build_kline_tab()
        self.addTab(self.tab_kline, "K线图")

        # P&F 点数图
        self.tab_pnf = QWidget()
        self._build_pnf_tab()
        self.addTab(self.tab_pnf, "P&F 点数图")

        # 技术指标
        self.tab_ind = QWidget()
        self._build_ind_tab()
        self.addTab(self.tab_ind, "技术指标")

        # 资金透视
        self.tab_mkt = QWidget()
        self._build_mkt_tab()
        self.addTab(self.tab_mkt, "资金透视")

        # 相关新闻
        self.tab_news = QWidget()
        self._build_news_tab()
        self.addTab(self.tab_news, "相关新闻")

        # 产业链地图
        self.tab_chain = QWidget()
        self._build_chain_tab()
        self.addTab(self.tab_chain, "产业链")

        # 解读
        self.tab_interp = QWidget()
        self._build_interp_tab()
        self.addTab(self.tab_interp, "解读")

        # 综合选股 / 校准中心 / 今日入场点: 均为懒创建 Tab, 不占启动页。
        # 由 MainWindow 按需创建 (菜单入口, 或 设置→基本→启动自动显示)。
        # 固定分析 Tab 去掉关闭按钮 (动态 Tab 由 setTabsClosable 自动带关闭键)。
        for i in range(self.count()):
            self.tabBar().setTabButton(i, self.tabBar().ButtonPosition.RightSide, None)

    def _build_kline_tab(self):
        layout = QVBoxLayout(self.tab_kline)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        layout.setSpacing(theme.spacing("1"))

        self.kline_widget = KlineWidget(font_size=self._font_size)
        info = QLabel("")
        info.setObjectName("klineInfoBar")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        info.setStyleSheet(
            f"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
            "border-radius:4px;padding:3px 10px;font-weight:bold;"
        )
        info.setFixedHeight(26)
        self.kline_widget.info_bar = info
        layout.addWidget(info)
        layout.addWidget(_CanvasFrame(self.kline_widget), 1)

    def _build_pnf_tab(self):
        layout = QVBoxLayout(self.tab_pnf)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        self.pnf_widget = PnfWidget(font_size=self._font_size)
        layout.addWidget(_CanvasFrame(self.pnf_widget))

    def _build_ind_tab(self):
        layout = QVBoxLayout(self.tab_ind)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        layout.setSpacing(theme.spacing("1"))

        # 页头工具栏: 默认视野/复位/保存/面板放大 — 交互入口从右键菜单提到页头
        from PyQt6.QtWidgets import QMenu
        self.ind_header = PanelHeader("技术指标")
        self.ind_hint = _chart_hint("滚轮缩放 · 拖拽平移 · 双击复位 · R全幅")
        self.ind_header.add_action(self.ind_hint)
        self.ind_focus_btn = _action_btn(
            "聚焦最近", checkable=True,
            tooltip="默认视野: 聚焦最近 N 根 (勾选) / 显示全幅 (取消)")
        self.ind_header.add_action(self.ind_focus_btn)
        self.ind_reset_btn = _action_btn("复位视图",
                                         tooltip="全部指标面板回到全幅 (快捷键 Home/R)")
        self.ind_header.add_action(self.ind_reset_btn)
        self.ind_save_btn = _action_btn("保存图片",
                                        tooltip="导出当前技术指标图为 PNG")
        self.ind_header.add_action(self.ind_save_btn)
        self.ind_panels_menu = QMenu(self.ind_header)
        self.ind_panels_btn = _menu_btn("面板 ▾", menu=self.ind_panels_menu,
                                        tooltip="放大单个指标面板 / 恢复网格")
        self.ind_header.add_action(self.ind_panels_btn)
        layout.addWidget(self.ind_header)

        self.ind_widget = IndWidget(font_size=self._font_size)
        sc = IndScroll(self.ind_widget, aspect=IND_ASPECT)
        self.ind_scroll = sc
        self.ind_widget.setMinimumSize(700, 1100)
        layout.addWidget(_CanvasFrame(sc))

    def _build_mkt_tab(self):
        layout = QVBoxLayout(self.tab_mkt)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        layout.setSpacing(theme.spacing("1"))

        # 页头工具栏: 与技术指标页同款交互入口
        from PyQt6.QtWidgets import QMenu
        self.mkt_header = PanelHeader("资金透视")
        self.mkt_hint = _chart_hint("滚轮缩放 · 拖拽平移 · 双击复位 · R全幅")
        self.mkt_header.add_action(self.mkt_hint)
        self.mkt_focus_btn = _action_btn(
            "聚焦最近", checkable=True,
            tooltip="默认视野: 日期面板聚焦最近 N 根 (勾选) / 显示全幅 (取消)")
        self.mkt_header.add_action(self.mkt_focus_btn)
        self.mkt_reset_btn = _action_btn("复位视图",
                                         tooltip="全部面板回到全幅 (快捷键 Home/R)")
        self.mkt_header.add_action(self.mkt_reset_btn)
        self.mkt_save_btn = _action_btn("保存图片",
                                        tooltip="导出当前资金透视图为 PNG")
        self.mkt_header.add_action(self.mkt_save_btn)
        self.mkt_panels_menu = QMenu(self.mkt_header)
        self.mkt_panels_btn = _menu_btn("面板 ▾", menu=self.mkt_panels_menu,
                                        tooltip="放大单个面板 / 单面板复位 / 恢复布局")
        self.mkt_header.add_action(self.mkt_panels_btn)
        layout.addWidget(self.mkt_header)

        self.mkt_widget = MktWidget(font_size=self._font_size)
        # 与技术指标页同一容器: 宽度铺满、高度按 MKT_ASPECT 比例、竖向滚动,
        # 面板长宽比例不随窗口形状被压扁
        sc = IndScroll(self.mkt_widget, aspect=MKT_ASPECT)
        self.mkt_scroll = sc
        self.mkt_widget.setMinimumSize(700, 500)
        layout.addWidget(_CanvasFrame(sc))

    def _build_news_tab(self):
        layout = QVBoxLayout(self.tab_news)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.news_widget = NewsWidget(font_size=self._font_size)
        layout.addWidget(self.news_widget)

    def _build_chain_tab(self):
        from .chain_widget import ChainWidget
        layout = QVBoxLayout(self.tab_chain)
        layout.setContentsMargins(0, 0, 0, 0)
        self.chain_widget = ChainWidget(
            font_size=self._font_size,
            on_load=self._on_load or (lambda c: None))
        layout.addWidget(self.chain_widget)

    def _build_interp_tab(self):
        layout = QVBoxLayout(self.tab_interp)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.spacing("1"))

        header = PanelHeader("AI解读")
        self.interp_regenerate_btn = QPushButton("重新生成")
        self.interp_regenerate_btn.setObjectName("ttsBtn")
        self.interp_regenerate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interp_regenerate_btn.setFixedHeight(22)
        header.add_action(self.interp_regenerate_btn)

        self.interp_tts_btn = QPushButton("▶ 语音朗读")
        self.interp_tts_btn.setObjectName("ttsBtn")
        self.interp_tts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interp_tts_btn.setFixedHeight(22)
        header.add_action(self.interp_tts_btn)

        self.interp_chat_btn = QPushButton("AI 问股")
        self.interp_chat_btn.setObjectName("ttsBtn")
        self.interp_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interp_chat_btn.setFixedHeight(22)
        header.add_action(self.interp_chat_btn)
        header.add_stretch()
        self.interp_header = header  # 主窗口追加 A-/A+ 缩放按钮用
        layout.addWidget(header)
        self.interp_text = QTextBrowser()
        self.interp_text.setOpenExternalLinks(True)
        self.interp_text.setObjectName("sectionText")
        layout.addWidget(self.interp_text, 1)


class WatchDock(QDockWidget):
    """自选股停靠面板。"""

    def __init__(self, parent=None):
        super().__init__("自选股", parent)
        self.setObjectName("dockWatch")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        self.panel = QWidget()
        self._build_panel()
        self.setWidget(self.panel)

    def _build_panel(self):
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(theme.spacing("1"), theme.spacing("1"), theme.spacing("1"), theme.spacing("1"))
        layout.setSpacing(theme.spacing("1"))

        # 标题栏
        header = PanelHeader("自选股")
        btn_coll = QPushButton("«")
        btn_coll.setObjectName("ttsBtn")
        btn_coll.setFixedSize(26, 22)
        btn_coll.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_coll.setToolTip("折叠左栏 (Ctrl+Shift+L)")
        header.add_action(btn_coll)
        self.btn_collapse = btn_coll
        layout.addWidget(header)

        # 列表
        self.watch_list = QListWidget()
        self.watch_list.setObjectName("watchList")
        self.watch_list.setItemDelegate(WatchCardDelegate(self.watch_list))
        self.watch_list.setSpacing(theme.spacing("1"))
        self.watch_list.setMouseTracking(True)
        self.watch_list.setUniformItemSizes(True)  # 关键: 所有项高度一致, 加速布局
        self.watch_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.watch_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.watch_list, 1)

        # 底部按钮
        from .components.card import CardFooter
        btn_add = QPushButton("＋")
        btn_add.setToolTip("把当前股票加入自选")
        btn_del = QPushButton("－")
        btn_del.setToolTip("从自选删除选中项")
        footer = CardFooter(actions=[btn_add, btn_del])
        self.btn_add = btn_add
        self.btn_del = btn_del
        layout.addWidget(footer)


class RightDock(QDockWidget):
    """右侧分析结论停靠面板。"""

    def __init__(self, parent=None):
        super().__init__("分析面板", parent)
        self.setObjectName("dockRight")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        self.panel = QWidget()
        self._build_panel()
        self.setWidget(self.panel)

    def _build_panel(self):
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName("rightSplitter")
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)
        self.right_splitter = splitter  # 主窗口别名引用用

        # 上: 信号汇总
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(theme.spacing("2"), theme.spacing("1"), theme.spacing("2"), theme.spacing("1"))
        top_layout.setSpacing(theme.spacing("1"))

        header = PanelHeader("信号汇总")
        btn_coll = QPushButton("»")
        btn_coll.setObjectName("ttsBtn")
        btn_coll.setFixedSize(26, 22)
        btn_coll.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_coll.setToolTip("折叠右栏 (Ctrl+Shift+R)")
        header.add_action(btn_coll)
        self.btn_collapse = btn_coll
        top_layout.addWidget(header)

        self.summary_scroll = QScrollArea()
        self.summary_scroll.setObjectName("summaryScroll")
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.summary_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.summary_grid = QWidget()
        self.summary_grid.setObjectName("cardGrid")
        self.summary_layout = FlowLayout(self.summary_grid, margin=theme.spacing("1"), h_spacing=theme.spacing("2"), v_spacing=theme.spacing("2"))
        self.summary_grid.setLayout(self.summary_layout)
        self.summary_scroll.setWidget(self.summary_grid)
        # FlowLayout 自适应换行, 无需监听视口 resize 手动重排

        top_layout.addWidget(self.summary_scroll, 1)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.C_BORDER};border:none;margin:{theme.spacing('1')}px 0;")
        top_layout.addWidget(line)

        # 下: 分析结论
        bottom = QWidget()
        bot_layout = QVBoxLayout(bottom)
        bot_layout.setContentsMargins(theme.spacing("2"), theme.spacing("1"), theme.spacing("2"), theme.spacing("1"))
        bot_layout.setSpacing(theme.spacing("1"))

        header = PanelHeader("分析结论")
        self.tts_btn = QPushButton("▶ 语音朗读")
        self.tts_btn.setObjectName("ttsBtn")
        self.tts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tts_btn.setFixedHeight(22)
        self.tts_btn.setToolTip("朗读本页分析结论 (设置→语音播报 可配置引擎/音色/语速)")
        header.add_action(self.tts_btn)
        header.add_stretch()
        bot_layout.addWidget(header)

        conc = QHBoxLayout()
        conc.setContentsMargins(0, 0, 0, 0)
        conc.setSpacing(theme.spacing("1"))

        self.section_list = SectionList(width=150)
        conc.addWidget(self.section_list)

        self.section_text = QTextBrowser()
        self.section_text.setOpenExternalLinks(True)
        self.section_text.setObjectName("sectionText")
        conc.addWidget(self.section_text, 1)

        bot_layout.addLayout(conc, 1)

        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 420])

        layout.addWidget(splitter, 1)


class MainView(QWidget):
    """
    主视图组件: 组合 ChartTabs + WatchDock + RightDock。
    仅负责 UI 布局, 不包含任何业务逻辑/线程/信号连接。
    """

    def __init__(self, font_size=12, on_load=None, parent=None):
        super().__init__(parent)
        self._font_size = font_size
        self._on_load = on_load
        self._build_ui()

    def _build_ui(self):
        # 中央标签页
        self.chart_tabs = ChartTabs(self._font_size, on_load=self._on_load)

        # 停靠面板
        self.dock_watch = WatchDock()
        self.dock_right = RightDock()

        # 布局: 中央 widget + 两侧 dock
        # 注意: QMainWindow 才能管理 dock, 这里只暴露 dock 供主窗口 addDockWidget
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(self.chart_tabs)

    def setup_docks(self, main_window):
        """由 MainWindow 调用, 注册停靠面板。"""
        main_window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_watch)
        main_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_right)

    # ── 便捷访问器 ────────────────────────────────────────────
    @property
    def kline_widget(self):
        return self.chart_tabs.kline_widget

    @property
    def pnf_widget(self):
        return self.chart_tabs.pnf_widget

    @property
    def ind_widget(self):
        return self.chart_tabs.ind_widget

    @property
    def ind_scroll(self):
        return self.chart_tabs.ind_scroll

    @property
    def ind_header(self):
        return self.chart_tabs.ind_header

    @property
    def ind_hint(self):
        return self.chart_tabs.ind_hint

    @property
    def ind_focus_btn(self):
        return self.chart_tabs.ind_focus_btn

    @property
    def ind_reset_btn(self):
        return self.chart_tabs.ind_reset_btn

    @property
    def ind_save_btn(self):
        return self.chart_tabs.ind_save_btn

    @property
    def ind_panels_btn(self):
        return self.chart_tabs.ind_panels_btn

    @property
    def ind_panels_menu(self):
        return self.chart_tabs.ind_panels_menu

    @property
    def mkt_widget(self):
        return self.chart_tabs.mkt_widget

    @property
    def mkt_scroll(self):
        return getattr(self.chart_tabs, "mkt_scroll", None)

    @property
    def mkt_header(self):
        return self.chart_tabs.mkt_header

    @property
    def mkt_hint(self):
        return self.chart_tabs.mkt_hint

    @property
    def mkt_focus_btn(self):
        return self.chart_tabs.mkt_focus_btn

    @property
    def mkt_reset_btn(self):
        return self.chart_tabs.mkt_reset_btn

    @property
    def mkt_save_btn(self):
        return self.chart_tabs.mkt_save_btn

    @property
    def mkt_panels_btn(self):
        return self.chart_tabs.mkt_panels_btn

    @property
    def mkt_panels_menu(self):
        return self.chart_tabs.mkt_panels_menu

    @property
    def news_widget(self):
        return self.chart_tabs.news_widget

    @property
    def chain_widget(self):
        return self.chart_tabs.chain_widget

    @property
    def interp_text(self):
        return self.chart_tabs.interp_text

    @property
    def interp_regenerate_btn(self):
        return self.chart_tabs.interp_regenerate_btn

    @property
    def interp_tts_btn(self):
        return self.chart_tabs.interp_tts_btn

    @property
    def interp_chat_btn(self):
        return self.chart_tabs.interp_chat_btn

    @property
    def watch_list(self):
        return self.dock_watch.watch_list

    @property
    def watch_add_btn(self):
        return self.dock_watch.btn_add

    @property
    def watch_del_btn(self):
        return self.dock_watch.btn_del

    @property
    def summary_layout(self):
        return self.dock_right.summary_layout

    @property
    def section_list(self):
        return self.dock_right.section_list

    @property
    def section_text(self):
        return self.dock_right.section_text

    @property
    def tts_btn(self):
        return self.dock_right.tts_btn
