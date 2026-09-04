"""主窗口: 三栏布局 (自选股 | 图表 | 结论) + 后台分析线程。

移植自 legacy/wyckoff_desktop_wx.py (wxPython), 改用 PyQt6:
  - 后台线程 + 跨线程信号回主线程换图 (原 wx.CallAfter 等价物)
  - 绘制数据在 worker 线程收集, 主线程交由 pyqtgraph 控件渲染
  - 右面板: 信号汇总卡片 + VSA 信号标签条 + 分节结论 (HTML 富文本)
  - 中央: K线图 / P&F / 技术指标 / 资金透视 / 解读 五个标签页
"""
import os
import re
import threading
import time

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import wyckoff.profile_sync as psync
from wyckoff._log import log_exc, log_msg
from wyckoff.analysis import _ANALYSIS_CACHE, _ANALYSIS_LOCK
from wyckoff.config import (
    EVENT_CN,
    PERIOD_OPTIONS,
    SCALE_OPTIONS,
    VSA_CN,
)
from wyckoff.pinyin import (
    ensure_full_market_index,
    load_pinyin_cache,
    load_watchlist_stocks,
    local_search_stock,
)
from wyckoff.settings_keys import DEFAULTS, S
from wyckoff.storage import (
    load_settings,
    load_watchlist,
    save_settings,
    save_watchlist,
)
from wyckoff.utils import normalize_symbol
from wyckoff.vsa_explain import LONG_ONLY_NOTE, explain

from . import theme
from .analysis_ctrl import AnalysisController
from .chart_manager import ChartManager
from .code_search import CodeSearchDialog
from .extra_windows import (
    AlertsWindow,
    CompareWindow,
    EtfMonitorWindow,
    HoldingsWindow,
    NotesWindow,
    NteamWindow,
    PortfolioWindow,
    ScanWindow,
    ScreenerWidget,
)
from .settings_dialog import SettingsDialog
from .state_manager import StateManager
from .threads import (
    AutoSyncThread,
    LabelAiThread,
    ScanMarketThread,
    StatusTicker,
    WatchRTThread,
    WatchScanThread,
)

# ──────────────────────────────────────────── 结论 HTML 渲染 ────────────────────────────────────────────

_CALLOUT_PREFIXES = (
    "解读:", "含义:", "建议:", "共振结论:", "当前进度:", "风险提示:", "风控:",
    "退出规则:", "仓位参考:", "期望最优:", "同期买入持有:", "→", "结论:", "注:",
)


def classify_line(stripped, ln):
    if stripped.endswith(":") and not ln.startswith(" "):
        return ("subhead", stripped[:-1])
    if re.match(r"^\d{4}-\d{2}-\d{2}", stripped):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\S+)\s+@\s*([\d.]+)\s+(.*)$", stripped)
        if m:
            return ("event", m.groups())
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\S+)\s+(.*)$", stripped)
        if m:
            return ("datarow", m.groups())
    if stripped.startswith("•"):
        return ("bullet", stripped[1:].strip())
    if stripped.startswith("近期支撑") or stripped.startswith("近期阻力"):
        return ("level", stripped)
    if stripped.startswith(_CALLOUT_PREFIXES):
        return ("callout", stripped)
    m = re.match(r"^([^:]{1,24}):\s*(.*)$", stripped)
    if m:
        return ("keyval", m.groups())
    if "▲" in stripped or "▼" in stripped:
        return ("target", stripped)
    return ("plain", stripped)


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def section_html(title, lines, font_size=11):
    bull = "买" in title
    bear = "卖" in title
    sub_c = theme.C_ACCENT
    sub_bg = theme.css_rgba(theme.C_ACCENT, 16)
    out = []
    out.append(f'<div style="font-family:\'{theme.ui_font_family()}\';'
               f'font-size:{font_size}pt;color:{theme.C_TEXT};">')
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        kind, data = classify_line(stripped, raw)
        if kind == "subhead":
            out.append(f'<div style="margin-top:8px;margin-bottom:2px;color:{sub_c};'
                       f'background-color:{sub_bg};font-weight:bold;padding:2px 6px;'
                       f'border-radius:3px;">▍ {_esc(data)}</div>')
        elif kind == "event":
            d, t, v, rest = data
            out.append(f'<div style="margin:1px 0;color:{theme.C_TEXT};">'
                       f'<span style="color:{theme.C_MUTED};">{_esc(d)}</span> '
                       f'<b>{_esc(t)}</b> @ <b>{_esc(v)}</b> {_esc(rest)}</div>')
        elif kind == "datarow":
            d, t, rest = data
            out.append(f'<div style="margin:1px 0;"><span style="color:{theme.C_MUTED};">{_esc(d)}</span> '
                       f'<b>{_esc(t)}</b> {_esc(rest)}</div>')
        elif kind == "bullet":
            out.append(f'<div style="margin:1px 0;padding-left:12px;">• {_esc(data)}</div>')
        elif kind == "level":
            color = theme.C_UP if "支撑" in stripped else theme.C_DOWN
            out.append(f'<div style="margin:2px 0;color:{color};font-weight:bold;">{_esc(stripped)}</div>')
        elif kind == "callout":
            bg = theme.css_rgba(theme.C_UP if bull else theme.C_DOWN if bear else theme.C_AMBER, 12)
            border = theme.C_UP if bull else theme.C_DOWN if bear else theme.C_AMBER
            out.append(f'<div style="margin:3px 0;background-color:{bg};border-left:3px solid {border};'
                       f'padding:4px 8px;border-radius:2px;">{_esc(stripped)}</div>')
        elif kind == "keyval":
            k, v = data
            out.append(f'<div style="margin:1px 0;color:{theme.C_TEXT};"><span style="color:{theme.C_MUTED};">{_esc(k)}:</span> {_esc(v)}</div>')
        elif kind == "target":
            up = "▲" in stripped
            color = theme.C_UP if up else theme.C_DOWN
            out.append(f'<div style="margin:2px 0;color:{color};">{_esc(stripped)}</div>')
        else:
            out.append(f'<div style="margin:1px 0;">{_esc(stripped)}</div>')
    out.append("</div>")
    return "".join(out)


# ──────────────────────────────────────────── 主窗口 ────────────────────────────────────────────

class MainWindow(QMainWindow):
    _tts_done_sig = pyqtSignal(bool, str)
    # 准确度后台评估完成 → 回主线程刷新准确度窗口 (Qt 控件只能在拥有线程访问)
    _accuracy_done_sig = pyqtSignal()

    def __init__(self):
        super().__init__()
        # 初始化状态管理器 (统一管理设置/自选股/窗口状态)
        self._state_mgr = StateManager(
            self,
            load_settings_fn=load_settings,
            save_settings_fn=save_settings,
            load_watchlist_fn=load_watchlist,
            save_watchlist_fn=save_watchlist,
        )
        self._state_mgr.load_settings()
        self.settings = self._state_mgr.settings  # 保持兼容性
        self._watchlist = self._state_mgr.watchlist

        # 初始化图表管理器 (统一管理四大图表)
        self._chart_mgr = ChartManager(self)

        # 初始化分析控制器 (统一管理分析启动/完成/错误/缓存)
        self._analysis_ctrl = AnalysisController(self, self.settings, self._chart_mgr)

        # 兼容性属性 (逐步迁移到各 Manager)
        self._analyzing = False
        self._current_code = ""
        self._current_name = ""
        self._applying_panel_state = False
        self._closing = False
        self._accuracy_done_sig.connect(self._on_accuracy_done)
        self._rt_threads = {}
        self._scan_threads = {}
        self._label_ai_threads = {}
        self._last_rt = {}
        self._spirit = None
        self._watch_pct_prev = {}
        self._watch_move_at = 0.0
        self._tts_playing = False
        self._tts_done_sig.connect(self._on_tts_done)
        self._text_font_size = int(self.settings.get(S.UI.TEXT_FONT_SIZE, 11) or 11)
        self._last_summary = None
        self._last_sections = None
        self._last_interp_lines = None
        self._last_segs = None
        self._last_df = None
        self._last_symbol = ""
        self._last_scale = 240
        self._last_datalen = 700

        theme.set_theme(str(self.settings.get(S.General.THEME, "light") or "light"))
        # 界面字体/字号 (设置→基本→界面字体), 需在 set_theme 后、setStyleSheet 前生效
        theme.set_ui_font(
            family=str(self.settings.get(S.UI.FONT_FAMILY, "") or ""),
            size=int(self.settings.get(S.UI.FONT_SIZE, 12) or 12),
            watch=int(self.settings.get(S.UI.WATCH_FONT_SIZE, 12) or 12))

        self.setWindowTitle("Wyckoff 威科夫分析客户端")
        self.resize(1480, 900)
        self.setStyleSheet(theme.QSS)

        load_pinyin_cache()
        load_watchlist_stocks()
        # 全市场拼音索引: 快速读盘已有文件, 缺失/过期时后台下载构建,
        # 保证键盘精灵可搜索任意 A 股 (而非仅限于自选股)。
        # 用 lambda 延迟解析 ensure_full_market_index: 让测试/conftest 能在窗口
        # 创建后仍可替换该函数, 避免后台线程在替换前就绑定到真实网络实现。
        # 线程体捕获一切异常: 后台维护线程绝不能让未处理异常逃逸(避免 pytest 报
        # PytestUnhandledThreadExceptionWarning, 生产环境静默退出也不可接受)。
        def _bg_full_market_index():
            try:
                ensure_full_market_index()
                self._full_market_index_done = True
            except BaseException as e:  # noqa: BLE001 后台线程兜底
                log_exc("全市场拼音索引后台构建失败", e)

        threading.Thread(target=_bg_full_market_index, daemon=True).start()

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._build_menu()
        self._apply_panel_state()

        # 信号连接放在 _apply_panel_state 之后, 避免 restoreState / toggle_panel
        # 触发 visibilityChanged → _sync_panel_btns 覆盖正确的设置值
        self.dock_watch.visibilityChanged.connect(
            lambda vis: self._sync_panel_btns("watch", vis))
        self.dock_right.visibilityChanged.connect(
            lambda vis: self._sync_panel_btns("right", vis))

        QApplication.instance().installEventFilter(self)

        if bool(self.settings.get(S.General.START_MAXIMIZED, True)):
            self.showMaximized()

        self._reload_watchlist()
        # 启动即做一次自选股扫描, 让状态栏中间头条尽快填充 (不依赖 auto_scan 开关)
        # 用挂在窗口上的单发定时器而非裸 singleShot: 窗口关闭即失效, 避免僵尸回调
        # 设置→基本「启动时不分析任何股票」开启时跳过自选股首扫与自动载入。
        no_startup = bool(self.settings.get(S.General.START_NO_ANALYSIS, True))
        if not no_startup and self._watchlist:
            self._startup_scan_timer = QTimer(self)
            self._startup_scan_timer.setSingleShot(True)
            self._startup_scan_timer.timeout.connect(self._startup_ticker_scan)
            self._startup_scan_timer.start(4000)
        # 启动默认: 优先加载上次退出前最后分析的股票 (自动记住), 否则用设置的默认股票
        last_code, last_scale, last_period = self._state_mgr.get_last_analyzed()
        default_load = str(self.settings.get(S.General.DEFAULT_LOAD, "") or "").strip()
        startup_code = last_code or default_load
        if not no_startup and startup_code:
            if last_code:
                # 恢复上次的周期/时间段选择
                self.cb_scale.setCurrentText(last_scale)
                self.cb_period.setCurrentText(last_period)
            self._load_code(startup_code)

        # 启动自动显示: 综合选股 / 校准中心 / 今日入场点 (设置→基本)
        self._auto_show_tabs()

        # ── 启动体检阶段进度 (D7): 状态栏阶段提示 + 耗时统计 + 体检汇总 ──
        # 阶段由真实标记驱动 (拼音索引后台完成 / 自选股首扫回执), 定时器只做
        # 推进与兜底, 全程无网络、无阻塞。
        self._boot_start = time.monotonic()
        self._full_market_index_done = False
        self._watch_rt_seen = False
        self._boot_phase = 0
        self._boot_phase_t0 = self._boot_start
        self._boot_times: dict[str, float] = {}
        self._boot_report = None
        self._boot_done = False
        self.boot_label = QLabel("")
        self.boot_label.setTextFormat(Qt.TextFormat.RichText)
        self.statusBar().addPermanentWidget(self.boot_label)
        self._boot_timer = QTimer(self)
        self._boot_timer.setInterval(500)
        self._boot_timer.timeout.connect(self._advance_boot_progress)
        self._boot_timer.start()

    @property
    def _panel_widths(self):
        return self._state_mgr.panel_widths

    @_panel_widths.setter
    def _panel_widths(self, v):
        self._state_mgr.panel_widths = v

    # ── 构建界面 ──
    def _build_menu(self):
        m = self.menuBar()

        # 文件: 导出与退出
        f = m.addMenu("文件")
        f.addAction("导出分析报告", "Ctrl+E", self.export_report)
        f.addAction("导出当前图表...", self._export_current_fig)
        f.addAction("导出全部图表...", self._export_all_figs)
        f.addSeparator()
        f.addAction("退出", "Ctrl+Q", self.close)

        # 分析: 当前标的的分析执行与配套操作
        a = m.addMenu("分析")
        a.addAction("开始分析", "F5", self.on_analyze)
        a.addAction("强制刷新分析", "Ctrl+F5", lambda: self.on_analyze(force_refresh=True))
        a.addSeparator()
        a.addAction("个股备注", self.open_notes)

        # 自选: 自选股管理与盯盘监测
        w = m.addMenu("自选")
        w.addAction("添加当前股票到自选股", "Ctrl+D", self.add_current_to_watch)
        w.addSeparator()
        w.addAction("自选股预警", self.open_alerts)
        w.addAction("刷新自选股行情", self._refresh_watch_rt)

        # 选股: 市场范围的扫描/筛股 (与单股分析区分开)
        s = m.addMenu("选股")
        s.addAction("扫描自选股", self.scan_watchlist)
        s.addAction("全市场扫描", self.scan_market)
        s.addAction("板块扫描", self.open_sector)
        s.addAction("综合选股", "Ctrl+Shift+S", self._switch_to_screener)
        s.addAction("今日入场点", self.on_scan_entries)
        s.addAction("待观察候选池", self.open_candidates)
        s.addSeparator()
        s.addAction("扫描中心", "Ctrl+Shift+M", self.open_scan_center)

        # 指数: 常用指数一键加载
        x = m.addMenu("指数")
        x.addAction("上证指数", lambda: self._load_code("sh000001"))
        x.addAction("深证成指", lambda: self._load_code("sz399001"))
        x.addAction("创业板指", lambda: self._load_code("sz399006"))
        x.addAction("科创50", lambda: self._load_code("sh000688"))
        x.addSeparator()
        x.addAction("沪深300", lambda: self._load_code("sh000300"))
        x.addAction("上证50", lambda: self._load_code("sh000016"))
        x.addAction("中证500", lambda: self._load_code("sh000905"))
        x.addAction("中证1000", lambda: self._load_code("sh000852"))
        x.addAction("中证A500", lambda: self._load_code("sh000510"))
        x.addSeparator()
        x.addAction("全部指数...", self._open_index_dialog)

        # 资金: 国家队/宽基ETF 资金流向跟踪
        g = m.addMenu("资金")
        g.addAction("国家队持仓透视", self.open_holdings)
        g.addAction("ETF 三因子份额监测", self.open_etf_monitor)
        g.addAction("国家队ETF跟踪", self.open_nteam)

        # 工具: 持仓/对比 + 信号验证 + 本地数据维护
        t = m.addMenu("工具")
        t.addAction("我的持仓 (盈亏跟踪)", self.open_portfolio)
        t.addAction("多股票对比", self.open_compare)
        t.addSeparator()
        t.addAction("校准中心", "Ctrl+Shift+A", lambda: self.open_accuracy_center())
        t.addSeparator()
        t.addAction("清除行情缓存", self._clear_market_cache)

        # 视图: 标签页切换与界面显隐
        v = m.addMenu("视图")
        _tab_names = ("K线图", "P&F 点数图", "技术指标", "资金透视", "相关新闻", "解读")
        for i, t_ in enumerate(_tab_names):
            v.addAction(t_, lambda i=i: self.tabs.setCurrentIndex(i))
        v.addSeparator()
        self.act_toggle_watch = QAction("显示自选股面板", self)
        self.act_toggle_watch.setCheckable(True)
        self.act_toggle_watch.setChecked(True)
        self.act_toggle_watch.setShortcut("Ctrl+Shift+W")
        self.act_toggle_watch.triggered.connect(
            lambda on: self.toggle_panel("watch", bool(on)))
        self.act_toggle_right = QAction("显示分析面板", self)
        self.act_toggle_right.setCheckable(True)
        self.act_toggle_right.setChecked(True)
        self.act_toggle_right.setShortcut("Ctrl+Shift+R")
        self.act_toggle_right.triggered.connect(
            lambda on: self.toggle_panel("right", bool(on)))
        v.addAction(self.act_toggle_watch)
        v.addAction(self.act_toggle_right)
        v.addSeparator()
        self.act_toggle_theme = v.addAction(self._theme_toggle_text(),
                                            self.toggle_theme)

        # 设置: 应用级配置
        p = m.addMenu("设置")
        p.addAction("设置...", "Ctrl+,", self.open_settings)

        h = m.addMenu("帮助")
        h.addAction("使用说明", self.show_help)
        h.addAction("关于", self._show_about)

    # ── 面板折叠 ──
    def toggle_panel(self, side, visible):
        dock = self.dock_watch if side == "watch" else self.dock_right
        key = "watch" if side == "watch" else "right"
        if not visible:
            # 折叠前记住当前宽度, 供再次展开时恢复
            self._panel_widths[key] = max(0, dock.width())
            self._persist_panel_widths()
        dock.setVisible(visible)
        if visible and self._panel_widths.get(key, 0):
            # 展开时恢复折叠前宽度 (需等在布局中可见后 resize 才生效)
            self._restore_panel_width(dock, key)
        if visible:
            # 展开微动效: 淡入 (非阻塞, 不影响 isVisible 同步语义; 失败静默)
            try:
                from .animation import fade_in
                fade_in(dock)
            except Exception:
                pass
        act = self.act_toggle_watch if side == "watch" else self.act_toggle_right
        if act is not None:
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)
        btn = self.btn_toggle_watch if side == "watch" else self.btn_toggle_right
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(visible)
            btn.blockSignals(False)
        if not getattr(self, "_applying_panel_state", False):
            self._remember_panel_state()

    def _restore_panel_width(self, dock, key):
        """面板重新显示后异步恢复折叠前宽度 (QMainWindow 布局需先处理可见性)。"""
        want = self._panel_widths.get(key, 0)
        if not want:
            return

        def _do():
            try:
                dock.resize(want, dock.height())
            except Exception:
                pass
        # 用 singleShot 让布局先完成 setVisible 处理, resize 才不被覆盖
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, _do)

    def _persist_panel_widths(self):
        try:
            self.settings["panel_widths"] = self._panel_widths
            save_settings(self.settings)
        except Exception:
            pass

    def _sync_panel_btns(self, side, visible):
        if self._closing:
            return
        act = self.act_toggle_watch if side == "watch" else self.act_toggle_right
        if act is not None:
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)
        btn = self.btn_toggle_watch if side == "watch" else self.btn_toggle_right
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(visible)
            btn.blockSignals(False)
        if not getattr(self, "_applying_panel_state", False):
            self._remember_panel_state()

    def _apply_panel_state(self):
        left = bool(self.settings.get(S.UI.LEFT_PANEL_VISIBLE, True))
        right = bool(self.settings.get(S.UI.RIGHT_PANEL_VISIBLE, True))
        self._applying_panel_state = True
        try:
            self.toggle_panel("watch", left)
            self.toggle_panel("right", right)
        finally:
            self._applying_panel_state = False

    def _remember_panel_state(self):
        self.settings[S.UI.LEFT_PANEL_VISIBLE] = self.dock_watch.isVisible()
        self.settings[S.UI.RIGHT_PANEL_VISIBLE] = self.dock_right.isVisible()
        try:
            import base64
            self.settings[S.UI.DOCK_STATE] = base64.b64encode(
                self.saveState(version=1)).decode("ascii")
        except Exception:
            pass
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def _vsep(self):
        f = QFrame()
        f.setObjectName("vsep")
        f.setFrameShape(QFrame.Shape.VLine)
        f.setFixedHeight(24)
        return f

    def _build_toolbar(self):
        tb = QWidget()
        tb.setObjectName("topBar")
        self.addToolBarBreak()
        lay = QHBoxLayout(tb)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        title = QLabel("威科夫分析")
        title.setObjectName("brandTitle")
        # 签名: 品牌前一条 accent 竖标 (读盘笔尖), 呼应面板标题左侧色条。
        # 用 QFrame + AutoFillBackground + 调色板, QLabel 背景在 X11 下不一定绘制。
        from PyQt6.QtGui import QPalette
        from PyQt6.QtWidgets import QFrame
        nib = QFrame()
        nib.setFixedWidth(4)
        nib.setFixedHeight(22)
        nib.setAutoFillBackground(True)
        _np = nib.palette()
        _np.setColor(QPalette.ColorRole.Window, QColor(theme.C_ACCENT))
        nib.setPalette(_np)
        lay.addWidget(nib)
        lay.addSpacing(6)
        lay.addWidget(title)
        lay.addWidget(self._vsep())

        lab = QLabel("周期:")
        lab.setStyleSheet(f"color:{theme.C_MUTED};")
        lay.addWidget(lab)
        self.cb_scale = _combo(list(SCALE_OPTIONS.keys()), self.settings.get(S.General.DEFAULT_SCALE, "日线"))
        lay.addWidget(self.cb_scale)

        lab = QLabel("区间:")
        lab.setStyleSheet(f"color:{theme.C_MUTED};")
        lay.addWidget(lab)
        self.cb_period = _combo(list(PERIOD_OPTIONS.keys()), self.settings.get(S.General.DEFAULT_PERIOD, "近3年"))
        lay.addWidget(self.cb_period)

        self.btn_analyze = _button("开始分析", primary=True)
        self.btn_analyze.clicked.connect(self.on_analyze)
        # 固定最小宽度: "开始分析"↔"正在分析中…" 切换时避免工具栏抖动
        _fm = self.btn_analyze.fontMetrics()
        self.btn_analyze.setMinimumWidth(
            max(_fm.horizontalAdvance("正在分析中…"), _fm.horizontalAdvance("开始分析")) + 24)
        lay.addWidget(self.btn_analyze)

        btn_refresh = _button("刷新")
        btn_refresh.clicked.connect(self.on_refresh)
        lay.addWidget(btn_refresh)

        # 信号头条: 滚动播报自选股高胜率信号; 点击载入对应标的分析,
        # 悬停暂停滚动 (原位于底部状态栏, 移至刷新按钮与股票信息之间)
        lay.addSpacing(8)
        self.status_ticker = StatusTicker()
        self.status_ticker.clicked_code.connect(self._load_code)
        self.status_ticker.setFixedWidth(300)
        lay.addWidget(self.status_ticker)

        lay.addStretch(1)

        # 工具栏「设置/加入自选/左栏/右栏」按钮已移除:
        # 设置走菜单 (文件→设置), 加入自选走菜单/右键, 左右栏开关走视图菜单;
        # 属性保留为 None, 兼容 toggle_panel/_sync_panel_btns 里的旧引用。
        self.btn_toggle_watch = None
        self.btn_toggle_right = None

        lay.addWidget(self._vsep())

        self.tb_name = QLabel("")
        self.tb_name.setObjectName("tbName")
        self.tb_code = QLabel("")
        self.tb_code.setObjectName("tbCode")
        self.tb_price = QLabel("")
        self.tb_price.setObjectName("tbPrice")
        self.tb_pct = QLabel("")
        self.tb_pct.setObjectName("tbPct")
        self.tb_advice = QLabel("")
        self.tb_advice.setObjectName("tbAdvice")
        for w in (self.tb_name, self.tb_code, self.tb_price,
                  self.tb_pct, self.tb_advice):
            lay.addWidget(w)

        # 账户登录入口: 未登录显示「登录」, 已登录显示账户名 (点击弹菜单可退出/同步)
        self._account_btn = _button("登录")
        self._account_btn.setObjectName("accountBtn")
        self._account_btn.setToolTip("账户登录 / 多设备私有数据同步")
        self._account_btn.clicked.connect(self._on_account_click)

        # 同步按钮: 双向同步 (pull→merge→push)
        self._sync_btn = _button("同步数据")
        self._sync_btn.setObjectName("syncBtn")
        self._sync_btn.setToolTip("双向同步: 下载远端变更 + 上传本地变更")
        self._sync_btn.setEnabled(False)  # 直到登录才启用
        self._sync_btn.clicked.connect(self._run_account_sync)

        # 新增: 仅从云端下载按钮
        self._pull_btn = _button("从云下载")
        self._pull_btn.setObjectName("pullBtn")
        self._pull_btn.setToolTip("从云端下载私有数据到本地 (不推送)")
        self._pull_btn.setEnabled(False)  # 直到登录才启用
        self._pull_btn.clicked.connect(self._run_pull)

        lay.addSpacing(4)
        lay.addWidget(self._sync_btn)
        lay.addWidget(self._pull_btn)
        lay.addSpacing(8)
        lay.addWidget(self._account_btn)
        self._refresh_account_state()

        tb.setToolTip("主工具栏")
        tbwrap = QToolBar("主工具栏", self)
        tbwrap.setObjectName("mainToolBar")
        tbwrap.setMovable(False)
        tbwrap.setFloatable(False)
        tbwrap.addWidget(tb)
        self.addToolBar(tbwrap)
        self._top_bar = tbwrap

    def _build_central(self):
        # 主视图分层 (ui/main_view.py): 纯 UI 组合 (标签页/停靠面板/卡片布局),
        # 这里只做业务接线 (信号连接/右键菜单/状态恢复), 不再内联搭界面。
        from .main_view import MainView
        v = self.view = MainView(font_size=self._chart_font(),
                                 on_load=self._load_code)

        # 属性别名: 既有逻辑沿用原属性名访问各控件
        ct = v.chart_tabs
        self.tabs = ct
        for name in ("tab_kline", "tab_pnf", "tab_ind", "tab_mkt",
                     "tab_news", "tab_interp"):
            setattr(self, name, getattr(ct, name))
        self.tab_screener = None   # 综合选股: 懒创建 Tab (菜单/自动显示时挂载)
        self.kline_widget = v.kline_widget
        self.pnf_widget = v.pnf_widget
        self.ind_widget = v.ind_widget
        self.ind_scroll = v.ind_scroll
        self.mkt_scroll = v.mkt_scroll
        self.mkt_widget = v.mkt_widget
        self.ind_header = v.ind_header
        self.ind_hint = v.ind_hint
        self.ind_focus_btn = v.ind_focus_btn
        self.ind_reset_btn = v.ind_reset_btn
        self.ind_save_btn = v.ind_save_btn
        self.ind_panels_btn = v.ind_panels_btn
        self.ind_panels_menu = v.ind_panels_menu
        self.mkt_header = v.mkt_header
        self.mkt_hint = v.mkt_hint
        self.mkt_focus_btn = v.mkt_focus_btn
        self.mkt_reset_btn = v.mkt_reset_btn
        self.mkt_save_btn = v.mkt_save_btn
        self.mkt_panels_btn = v.mkt_panels_btn
        self.mkt_panels_menu = v.mkt_panels_menu
        self.news_widget = v.news_widget
        self.interp_text = v.interp_text
        self.interp_regenerate_btn = v.interp_regenerate_btn
        self.interp_tts_btn = v.interp_tts_btn
        self.interp_chat_btn = v.interp_chat_btn
        self.screener_widget = None  # 综合选股控件: 随 Tab 懒创建
        self.watch_panel = v.dock_watch.panel
        self.right_panel = v.dock_right.panel
        self.dock_watch = v.dock_watch
        self.dock_right = v.dock_right
        self.right_splitter = v.dock_right.right_splitter
        self.summary_scroll = v.dock_right.summary_scroll
        self.summary_grid = v.dock_right.summary_grid
        self.summary_lay = v.summary_layout          # FlowLayout 自动换行
        self.section_list = v.section_list
        self.section_text = v.section_text
        self.tts_btn = v.tts_btn
        # 校准中心: 懒创建 Tab (open_accuracy_center 首次打开才挂载)
        self._ac_win = None
        self._ac_container = None
        # 今日入场点: 懒创建 Tab (恒置于最后, on_scan_entries 触发创建)
        self.entry_tab = None
        # 模拟盘: 常驻固定 Tab (_ensure_paper_tab 建立, 不可关闭)
        self.paper_tab = None

        # ── 业务接线 (自旧 *_build_*_tab 迁移) ──
        # K线: 图层状态恢复 + 右键菜单 + VSA 标签点击解释
        try:
            saved = self.settings.get(S.Chart.KLINE_LAYERS) or {}
            if isinstance(saved, dict):
                self.kline_widget.set_layers_state(saved)
        except Exception:
            pass
        self.kline_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.kline_widget.customContextMenuRequested.connect(self._kline_menu)
        self.kline_widget.labelClicked.connect(self._on_kline_label_clicked)
        # P&F: 右键菜单 + 键盘 [ / ] 格值缩放
        self.pnf_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.pnf_widget.customContextMenuRequested.connect(self._pnf_menu)
        self.pnf_widget.box_scale_requested.connect(self._pnf_box_scale)
        # 技术指标 / 资金透视: 右键菜单
        for w in (self.ind_widget, self.mkt_widget):
            w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ind_widget.customContextMenuRequested.connect(self._ind_menu)
        self.mkt_widget.customContextMenuRequested.connect(self._mkt_menu)

        # 技术指标 / 资金透视: 页头工具栏 (默认视野/复位/保存/面板放大)
        self.ind_widget.default_bars = int(
            self.settings.get(S.Chart.IND_DEFAULT_BARS, 250) or 0)
        self.mkt_widget.default_bars = int(
            self.settings.get(S.Chart.MKT_DEFAULT_BARS, 120) or 0)
        self.ind_reset_btn.clicked.connect(self.ind_widget.reset_view)
        self.mkt_reset_btn.clicked.connect(self.mkt_widget.reset_view)
        self.ind_save_btn.clicked.connect(
            lambda: self._save_ind_png(f"wyckoff_{self._current_code or 'chart'}",
                                       quiet=True))
        self.mkt_save_btn.clicked.connect(
            lambda: self._save_mkt_png(f"wyckoff_{self._current_code or 'chart'}",
                                       quiet=True))
        self.ind_focus_btn.toggled.connect(self._on_ind_focus_toggled)
        self.mkt_focus_btn.toggled.connect(self._on_mkt_focus_toggled)
        self.ind_panels_menu.aboutToShow.connect(self._refresh_ind_panels_menu)
        self.mkt_panels_menu.aboutToShow.connect(self._refresh_mkt_panels_menu)
        self._sync_focus_btns()

        # 连接状态栏消息信号
        self.ind_widget.status_message.connect(self._status)
        self.mkt_widget.status_message.connect(self._status)
        self.ind_widget.crosshair_moved.connect(self._on_ind_crosshair_moved)
        self.mkt_widget.crosshair_moved.connect(self._on_mkt_crosshair_moved)

        # 解读页: 按钮 tooltip/连接 + A-/A+ 缩放 + 占位文案
        self.interp_regenerate_btn.setToolTip(
            "对当前分析重新调用大模型生成 AI 解读\n"
            "(上一轮可能因网络/模型超时而未生成, 可点此重试)")
        self.interp_regenerate_btn.clicked.connect(self._on_interp_regenerate)
        self.interp_tts_btn.setToolTip(
            "朗读本条 AI 解读 (设置→语音播报 可配置引擎/音色/语速)")
        self.interp_tts_btn.clicked.connect(self._on_interp_tts_click)
        self.interp_chat_btn.setToolTip(
            "多轮对话追问当前分析 (AI 解读的进阶形态)\n"
            "系统已注入当前报告 + 该标的历史信号实证, 可连续追问")
        self.interp_chat_btn.clicked.connect(self._on_ai_chat)
        ct.interp_header.layout().addWidget(self._zoom_buttons())
        self._set_interp_placeholder(self._text_font_size)

        # 标签页关闭: 仅「综合选股 / 校准中心」可关 (view 已摘除其余关闭按钮)
        self.tabs.tabCloseRequested.connect(self._on_tab_close)

        # 模拟盘: 常驻固定 Tab (不可关闭)
        self._ensure_paper_tab()

        # 右面板: 节切换 + TTS + 初始空渲染
        self._section_htmls = []
        self.section_list.currentRowChanged.connect(self._on_section_changed)
        self.tts_btn.clicked.connect(self._on_tts_click)
        self._render_summary([])
        self._render_sections([])

        # 自选股面板: 双击强刷 / 单击载入 / 右键菜单 / 底部按钮 / 折叠
        wl = self.watch_list = v.watch_list
        wl.itemDoubleClicked.connect(
            lambda it: self._force_analyze(it.data(Qt.ItemDataRole.UserRole)))
        wl.currentItemChanged.connect(self._on_watch_selected)
        wl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        wl.customContextMenuRequested.connect(self._watch_menu)
        btn_add = v.watch_add_btn
        btn_add.clicked.connect(self.add_current_to_watch)
        btn_del = v.watch_del_btn
        btn_del.clicked.connect(self.remove_watch_item)
        v.dock_watch.btn_collapse.clicked.connect(
            lambda: self.toggle_panel("watch", False))
        v.dock_right.btn_collapse.clicked.connect(
            lambda: self.toggle_panel("right", False))

        self.setCentralWidget(self.tabs)
        v.setup_docks(self)

        # 恢复停靠面板状态 (位置、浮动等)
        # 使用 version 参数: 旧版本保存的 state (无 dock 时) 与新布局不兼容,
        # restoreState 会返回 False, 不会崩溃; 若仍异常则清除旧状态。
        dock_state = self.settings.get(S.UI.DOCK_STATE)
        if dock_state:
            try:
                import base64
                ok = self.restoreState(base64.b64decode(dock_state), version=1)
                if not ok:
                    self.settings.pop(S.UI.DOCK_STATE, None)
            except Exception:
                self.settings.pop(S.UI.DOCK_STATE, None)


    # ── 各标签页 ──
    def _toggle_kline_layer(self, key, on):
        """图层开关: 应用到控件并持久化。"""
        self.kline_widget.set_layer_visible(key, bool(on))
        self.settings[S.Chart.KLINE_LAYERS] = self.kline_widget.layers_state()
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def _kline_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_save = menu.addAction("保存图片...")
        a_fit = menu.addAction("复位视图")
        menu.addSeparator()
        layers_menu = menu.addMenu("图层显示")
        state = self.kline_widget.layers_state()
        from .kline_widget import LAYER_DEFS
        layer_actions = {}
        for key, name in LAYER_DEFS:
            act = layers_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(bool(state.get(key, True)))
            layer_actions[act] = key
        action = menu.exec(self.kline_widget.mapToGlobal(pos))
        if action == a_save:
            self._save_kline_png(f"wyckoff_{self._current_code or 'chart'}")
        elif action == a_fit:
            self.kline_widget.reset_view()
        elif action in layer_actions:
            act = action
            self._toggle_kline_layer(layer_actions[act], act.isChecked())

    def _on_kline_label_clicked(self, label, conf=None):
        self._show_vsa_explain(str(label), conf)

    def _pnf_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_save = menu.addAction("保存图片...")
        a_copy = menu.addAction("复制图片到剪贴板")
        a_csv = menu.addAction("导出列数据 CSV...")
        menu.addSeparator()
        # ── 图层开关 ──
        layer_menu = menu.addMenu("图层")
        _acts = {}
        for name, label in self.pnf_widget.LAYER_LABELS.items():
            act = layer_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.pnf_widget.layer_state().get(name, True))
            _acts[name] = act
        if getattr(self, "_pnf_box_mult", 1.0) != 1.0:
            menu.addSeparator()
            a_bx = menu.addAction("格值复位 ×1.0")
        else:
            a_bx = None
        menu.addSeparator()
        a_fit = menu.addAction("复位视图")
        action = menu.exec(self.pnf_widget.mapToGlobal(pos))
        if action is None:
            return
        if action == a_save:
            self._save_pnf_png(f"wyckoff_{self._current_code or 'chart'}")
        elif action == a_copy:
            self._pnf_copy_image()
        elif action == a_csv:
            self._export_pnf_csv()
        elif action in _acts.values():
            # 找到对应图层名
            for name, act in _acts.items():
                if act is action:
                    self.pnf_widget.set_layer_visible(name, act.isChecked())
                    break
        elif action == a_bx:
            self._pnf_box_reset()
        elif action == a_fit:
            self.pnf_widget.reset_view()

    def _pnf_copy_image(self):
        QApplication.clipboard().setPixmap(self.pnf_widget.grab_pixmap())
        self._status("点数图已复制到剪贴板", theme.C_MUTED)

    def _export_pnf_csv(self):
        base = f"wyckoff_{self._current_code or 'chart'}_pnf"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出点数图列数据", base + ".csv", "CSV (*.csv)")
        if not path:
            return
        try:
            self.pnf_widget.export_csv(path)
            self._status(f"已导出 {path}", theme.C_MUTED)
        except Exception as e:
            log_exc("点数图导出 CSV 失败", e)
            self._status("导出失败", theme.C_DOWN)

    def _pnf_box_scale(self, mult):
        """键盘 [ / ] 请求格值缩放: 从缓存 df 本地重算 pnf 并刷新控件。"""
        from .pnf import build_pnf, build_pnf_data, pnf_history_targets, pnf_targets, pnf_volume
        df = self._last_df
        if df is None or not self._last_pnf or not self._last_pnf.get("cols"):
            self._status("暂无分析数据, 无法调整格值", theme.C_MUTED)
            return
        self._pnf_box_mult = min(
            max(getattr(self, "_pnf_box_mult", 1.0) * float(mult), 0.25), 4.0)
        mode = str(self.settings.get(S.Chart.PNF_BOX_MODE, "pct") or "pct")
        af = float(self.settings.get(S.Chart.PNF_ATR_FACTOR, 0.5) or 0.5)
        try:
            cols, box = build_pnf(df, box_pct=0.015 * self._pnf_box_mult,
                                  box_mode=mode,
                                  atr_factor=af * self._pnf_box_mult)
            vol = pnf_volume(df, cols, box)
            t = pnf_targets(df, cols, box, volumes=vol)
            hist = pnf_history_targets(cols, box)
            title = self._last_pnf.get("title", "")
            if self._pnf_box_mult != 1.0:
                title = f"{title}  |  格值倍率 ×{self._pnf_box_mult:.2f}"
            data = build_pnf_data(cols, box, title, targets=t, history=hist,
                                  df=df, box_mode=mode, atr_factor=af)
        except Exception as e:
            log_exc("点数图格值重算失败", e)
            self._status("格值重算失败", theme.C_DOWN)
            return
        self._last_pnf = data
        self.pnf_widget.set_data(**data, code=self._current_code)
        self._status(f"P&F 格值 ×{self._pnf_box_mult:.2f} (当前格值 {box:.3f})",
                     theme.C_ACCENT)

    def _pnf_box_reset(self):
        from .pnf import build_pnf, build_pnf_data, pnf_history_targets, pnf_targets, pnf_volume
        self._pnf_box_mult = 1.0
        if not self._last_pnf or not self._last_pnf.get("cols"):
            self.pnf_widget.set_data(code=self._current_code)
            return
        df = self._last_df
        if df is None:
            self.pnf_widget.set_data(code=self._current_code)
            return
        mode = str(self.settings.get(S.Chart.PNF_BOX_MODE, "pct") or "pct")
        af = float(self.settings.get(S.Chart.PNF_ATR_FACTOR, 0.5) or 0.5)
        try:
            cols, box = build_pnf(df, box_mode=mode, atr_factor=af)
            vol = pnf_volume(df, cols, box)
            t = pnf_targets(df, cols, box, volumes=vol)
            hist = pnf_history_targets(cols, box)
            data = build_pnf_data(cols, box, self._last_pnf.get("title", ""),
                                  targets=t, history=hist, df=df,
                                  box_mode=mode, atr_factor=af)
            self._last_pnf = data
            self.pnf_widget.set_data(**data, code=self._current_code)
        except Exception as e:
            log_exc("点数图格值复位失败", e)

    def _ind_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_save = menu.addAction("保存图片...")
        a_fit = menu.addAction("复位视图")
        menu.addSeparator()
        a_focus = menu.addMenu("聚焦面板")
        for panel_key, panel_name in (
            ("macd", "MACD"), ("volume", "量能"), ("price", "价格·布林带"),
            ("kdj", "KDJ"), ("rsi", "RSI"), ("obv", "OBV"),
            ("vp", "量价分布"), ("rs", "相对强度 RS")
        ):
            act = a_focus.addAction(panel_name)
            act.setCheckable(True)
            act.setChecked(self.ind_widget.is_focused() == panel_key)
            act.triggered.connect(lambda _=False, k=panel_key: self.ind_widget.focus_panel(k))
        a_grid = a_focus.addAction("⧉ 恢复网格视图")
        a_grid.triggered.connect(self.ind_widget.show_grid)
        menu.addSeparator()
        a_sync = menu.addAction("联动缩放/平移 (所有面板)")
        a_sync.setCheckable(True)
        a_sync.setChecked(getattr(self.ind_widget, "_sync_x", True))
        a_sync.toggled.connect(self.ind_widget._toggle_sync_mode)
        a_copy = menu.addAction("复制面板数据 (Ctrl+C)")
        a_copy.triggered.connect(self.ind_widget._copy_panel_data)
        a_help = menu.addAction("快捷键帮助 (H)")
        a_help.triggered.connect(self.ind_widget._show_shortcuts_help)
        action = menu.exec(self.ind_widget.mapToGlobal(pos))
        if action == a_save:
            self._save_ind_png(f"wyckoff_{self._current_code or 'chart'}")
        elif action == a_fit:
            self.ind_widget.reset_view()

    def _mkt_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_save = menu.addAction("保存图片...")
        a_fit = menu.addAction("复位视图")
        menu.addSeparator()
        a_focus = menu.addMenu("聚焦面板")
        labels = {
            "main_flow": "主力资金流向", "chips": "当前筹码堆积形态",
            "sd": "供需强度", "holders": "股东户数变化",
            "sub_flow": "资金分项"
        }
        for key, label in labels.items():
            if key not in self.mkt_widget.plots:
                continue
            act = a_focus.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.mkt_widget.is_focused() == key)
            act.triggered.connect(lambda _=False, k=key: self.mkt_widget.focus_panel(k))
        a_grid = a_focus.addAction("⧉ 恢复完整布局")
        a_grid.triggered.connect(self.mkt_widget.show_grid)
        menu.addSeparator()
        a_copy = menu.addAction("复制面板数据 (Ctrl+C)")
        a_copy.triggered.connect(self.mkt_widget._copy_panel_data)
        a_help = menu.addAction("快捷键帮助 (H)")
        a_help.triggered.connect(self.mkt_widget._show_shortcuts_help)
        action = menu.exec(self.mkt_widget.mapToGlobal(pos))
        if action == a_save:
            self._save_mkt_png(f"wyckoff_{self._current_code or 'chart'}")
        elif action == a_fit:
            self.mkt_widget.reset_view()

    # ── 技术指标 / 资金透视: 页头工具栏 ──
    def _sync_focus_btns(self):
        """按设置同步两页「聚焦最近」按钮勾选态 (设置变更后调用)。"""
        for btn, key in ((self.ind_focus_btn, S.Chart.IND_DEFAULT_BARS),
                         (self.mkt_focus_btn, S.Chart.MKT_DEFAULT_BARS)):
            btn.blockSignals(True)
            try:
                btn.setChecked(int(self.settings.get(key, 0) or 0) > 0)
            finally:
                btn.blockSignals(False)

    def _apply_chart_defaults(self):
        """把设置里的默认柱数同步到两个图表控件。"""
        self.ind_widget.default_bars = int(
            self.settings.get(S.Chart.IND_DEFAULT_BARS, 250) or 0)
        self.mkt_widget.default_bars = int(
            self.settings.get(S.Chart.MKT_DEFAULT_BARS, 120) or 0)

    def _on_ind_focus_toggled(self, checked):
        n = int(self.settings.get(S.Chart.IND_DEFAULT_BARS, 250) or 0)
        self.ind_widget.default_bars = n if checked else 0
        if self.ind_widget._n > 0:
            self.ind_widget.apply_default_view()
            self._status("技术指标: " + (f"聚焦最近 {n} 根" if checked else "显示全幅"),
                         theme.C_MUTED)

    def _on_mkt_focus_toggled(self, checked):
        n = int(self.settings.get(S.Chart.MKT_DEFAULT_BARS, 120) or 0)
        self.mkt_widget.default_bars = n if checked else 0
        if getattr(self.mkt_widget, "_full_x", None):
            self.mkt_widget.apply_default_view()
            self._status("资金透视: " + (f"日期面板聚焦最近 {n} 根" if checked
                                         else "显示全幅"),
                         theme.C_MUTED)

    def _on_ind_crosshair_moved(self, panel_key, x, y):
        """技术指标十字光标移动: 在状态栏显示当前面板数值。"""
        labels = {
            "macd": "MACD", "volume": "量能", "price": "价格·布林带",
            "kdj": "KDJ", "rsi": "RSI", "obv": "OBV",
            "vp": "量价分布", "rs": "相对强度"
        }
        label = labels.get(panel_key, panel_key)
        self._status(f"技术指标 [{label}]: X={x:.1f}, Y={y:.2f}")

    def _on_mkt_crosshair_moved(self, panel_key, x, y):
        """资金透视十字光标移动: 在状态栏显示当前面板数值。"""
        labels = {
            "main_flow": "主力资金流向", "sub_flow": "资金分项",
            "chips": "筹码分布", "holders": "股东户数", "sd": "供需强度"
        }
        label = labels.get(panel_key, panel_key)
        self._status(f"资金透视 [{label}]: X={x:.1f}, Y={y:.2f}")

    def _refresh_ind_panels_menu(self):
        """面板▾菜单: 放大单个指标面板 / 恢复 4×2 网格。"""
        from .ind_panels import get_panels
        menu = self.ind_panels_menu
        menu.clear()
        focused = self.ind_widget.is_focused()
        if focused:
            act = menu.addAction("⧉ 恢复网格视图")
            act.triggered.connect(self.ind_widget.show_grid)
            menu.addSeparator()
        for panel in get_panels():
            act = menu.addAction(panel.title)
            act.setCheckable(True)
            act.setChecked(focused == panel.key)
            act.triggered.connect(
                lambda _=False, k=panel.key: self.ind_widget.focus_panel(k))

    def _refresh_mkt_panels_menu(self):
        """面板▾菜单: 放大查看 / 单面板复位到全幅。"""
        menu = self.mkt_panels_menu
        menu.clear()
        focused = self.mkt_widget.is_focused()
        if focused:
            act = menu.addAction("⧉ 恢复完整布局")
            act.triggered.connect(self.mkt_widget.show_grid)
            menu.addSeparator()
        labels = {
            "main_flow": "主力资金流向",
            "chips": "当前筹码堆积形态",
            "holders": "股东户数变化",
            "sd": "供需强度",
            "sub_flow": "资金分项",
        }
        for key, label in labels.items():
            if key not in self.mkt_widget.plots:
                continue
            sub = menu.addMenu(f"{label}")
            a_zoom = sub.addAction("放大查看")
            a_zoom.setCheckable(True)
            a_zoom.setChecked(focused == key)
            a_zoom.triggered.connect(
                lambda _=False, k=key: self.mkt_widget.focus_panel(k))
            a_reset = sub.addAction("该面板复位到全幅")
            a_reset.triggered.connect(
                lambda _=False, k=key: self.mkt_widget.reset_plot(k))
        menu.addSeparator()
        a_copy = menu.addAction("复制面板数据 (Ctrl+C)")
        a_copy.triggered.connect(self.mkt_widget._copy_panel_data)
        a_help = menu.addAction("快捷键帮助 (H)")
        a_help.triggered.connect(self.mkt_widget._show_shortcuts_help)

    def _build_screener_tab(self):
        lay = QVBoxLayout(self.tab_screener)
        lay.setContentsMargins(0, 0, 0, 0)
        self.screener_widget = ScreenerWidget(
            parent=self.tab_screener, on_load=self._load_code)
        lay.addWidget(self.screener_widget, 1)

    # ── 右面板 ──
    def _zoom_buttons(self):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        btn_out = QPushButton("A−")
        btn_out.setObjectName("ttsBtn")
        btn_out.setFixedSize(26, 22)
        btn_out.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_out.setToolTip("文字缩小")
        btn_out.clicked.connect(lambda: self._zoom_text(-1))
        btn_in = QPushButton("A+")
        btn_in.setObjectName("ttsBtn")
        btn_in.setFixedSize(26, 22)
        btn_in.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_in.setToolTip("文字放大")
        btn_in.clicked.connect(lambda: self._zoom_text(1))
        h.addWidget(btn_out)
        h.addWidget(btn_in)
        return box

    def _build_statusbar(self):
        sb = self.statusBar()
        self.stock_info = QLabel("")
        sb.addWidget(self.stock_info)
        # 信号头条已移至主工具栏 (刷新按钮与股票信息之间), 状态栏不再放置
        sb.addPermanentWidget(QLabel("  "))
        self.status_label = QLabel("就绪")
        sb.addPermanentWidget(self.status_label)
        sb.addPermanentWidget(QLabel("  "))
        self.source_health_label = QLabel("")
        self.source_health_label.setTextFormat(Qt.TextFormat.RichText)
        sb.addPermanentWidget(self.source_health_label)
        self._update_source_health()

    def _update_source_health(self):
        """把各数据源健康度渲染进状态栏: 源名+成功/失败+成功率, 按成功率着色。"""
        try:
            from wyckoff.datasource import source_health
            h = source_health()
        except Exception:
            h = {}
        if not h:
            self.source_health_label.setText("")
            return
        parts = []
        order = ("新浪", "东方财富", "腾讯")
        names = order + tuple(k for k in h if k not in order)
        for name in names:
            stat = h.get(name)
            if not stat or stat["ok_ratio"] is None:
                continue
            ratio = stat["ok_ratio"]
            if ratio >= 0.9:
                color = theme.C_UP
            elif ratio >= 0.5:
                color = theme.C_AMBER
            else:
                color = theme.C_DOWN
            total = stat["ok"] + stat["fail"]
            parts.append(
                f"<span style='color:{color};'>{_esc(name)} {stat['ok']}/{total}</span>")
        if not parts:
            self.source_health_label.setText("")
            return
        self.source_health_label.setText(" 数据源: " + " · ".join(parts))

    # ── 账户登录 ──
    def _refresh_account_state(self):
        """刷新工具栏账户/同步按钮: 未登录=「登录/禁用」, 已登录=账户名/启用。"""
        try:
            from wyckoff import account
            st = account.status()
            if st.get("logged_in"):
                self._account_btn.setText(str(st.get("current", "登录")))
                self._sync_btn.setEnabled(True)
                self._sync_btn.setToolTip("同步账户私有数据 (云端上行 + 下行)")
                self._pull_btn.setEnabled(True)
                self._pull_btn.setToolTip("从云端下载私有数据到本地")
            else:
                self._account_btn.setText("登录")
                self._sync_btn.setEnabled(False)
                self._sync_btn.setToolTip("请先登录")
                self._pull_btn.setEnabled(False)
                self._pull_btn.setToolTip("请先登录")
        except Exception:
            self._account_btn.setText("登录")
            self._sync_btn.setEnabled(False)
            self._pull_btn.setEnabled(False)

    def _on_account_click(self):
        """点击账户按钮: 未登录弹登录对话框, 已登录弹菜单(退出登录)。"""
        try:
            from wyckoff import account
            if not account.status().get("logged_in"):
                self._open_login_dialog()
                return
            from PyQt6.QtWidgets import QMenu
            menu = QMenu(self)
            act_logout = menu.addAction("退出登录")
            chosen = menu.exec(self._account_btn.mapToGlobal(
                self._account_btn.rect().bottomLeft()))
            if chosen is act_logout:
                _, _ = account.logout()
                self._refresh_account_state()
        except Exception as e:
            self._status(f"账户操作异常: {e}")

    def _open_login_dialog(self):
        """弹出登录对话框: 账户名 + 密码 + 登录/注册 (走 MySQL 云后端)。"""
        from PyQt6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout

        from . import theme
        dlg = QDialog(self)
        dlg.setWindowTitle("账户登录")
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)
        tip = QLabel("登录后可在多台设备间同步账户私有数据 "
                     "(UI 布局 / 自选 / 候选 / 笔记 / 组合 / 模拟盘)。\n"
                     "通过云端数据库 (MySQL) 多设备同步, AI Key 等敏感凭据不会上传。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.C_MUTED};")
        lay.addWidget(tip)

        lab = QLabel("账户名:")
        lay.addWidget(lab)
        ed_user = QLineEdit()
        ed_user.setPlaceholderText("用户名")
        lay.addWidget(ed_user)

        lab = QLabel("密码:")
        lay.addWidget(lab)
        ed_pass = QLineEdit()
        ed_pass.setEchoMode(QLineEdit.EchoMode.Password)
        ed_pass.setPlaceholderText("至少 6 位")
        lay.addWidget(ed_pass)

        err = QLabel("")
        err.setWordWrap(True)
        err.setStyleSheet(f"color:{theme.C_UP};")
        lay.addWidget(err)

        btns = QHBoxLayout()
        btn_login = QPushButton("登录")
        btn_login.setObjectName("primaryBtn")
        btn_register = QPushButton("注册并登录")
        btn_cancel = QPushButton("取消")
        btns.addStretch(1)
        btns.addWidget(btn_login)
        btns.addWidget(btn_register)
        btns.addWidget(btn_cancel)
        lay.addLayout(btns)

        # 走云端后端登录/注册, 成功后执行一次同步 (云端或 Git 回退都由 sync_once 处理)
        def _login():
            from wyckoff import account
            ok, msg = account.login(ed_user.text().strip(), ed_pass.text())
            if ok:
                # 自动初始化/执行账户数据同步 (git 首次需 setup, sync_once 内部回退)
                try:
                    res = psync.setup("") if not psync.status().get("configured") \
                        else psync.sync_once()
                except Exception:
                    pass  # sync init non-fatal, 可在菜单里手动触发
                # 同步后刷新自选股 UI, 确保私有数据(自选/笔记/组合)及时展示
                try:
                    self.reload_watchlist()
                except Exception:
                    pass
                dlg.accept()
            else:
                err.setText(msg)

        def _register():
            from wyckoff import account
            ok, msg = account.register(ed_user.text().strip(), ed_pass.text())
            if ok:
                # 注册成功后自动执行一次数据同步
                try:
                    res = psync.setup("") if not psync.status().get("configured") \
                        else psync.sync_once()
                except Exception:
                    pass  # sync init non-fatal
                dlg.accept()
            else:
                err.setText(msg)

        btn_login.clicked.connect(_login)
        btn_register.clicked.connect(_register)
        btn_cancel.clicked.connect(dlg.reject)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_account_state()

    def _run_account_sync(self):
        """后台执行账户私有数据同步 (走 AutoSyncThread)。"""

        from .threads.auto_sync_thread import AutoSyncThread

        def work():
            from wyckoff import cloud_db
            if cloud_db.enabled():
                # 云端后端: sync_once 已同时覆盖首次同步与增量合并
                return psync.sync_once()
            # Git 回退: 未配置时先 setup, 已配置则双向同步
            if not psync.status().get("configured"):
                return psync.setup("")
            return psync.sync_once()

        self._account_sync_thread = AutoSyncThread(work, self)

        def _finish(res):
            if isinstance(res, dict) and res.get("ok"):
                self._status("账户数据同步完成")
                self.reload_watchlist()
            else:
                self._status(
                    f"账户同步失败: {(res or {}).get('error', '未知错误')}")
            self._refresh_account_state()

        self._account_sync_thread.result.connect(_finish)
        self._account_sync_thread.start()


    def _run_pull(self):
        """后台执行: 从远端仓库拉取私有数据到本地。
        仅执行 git pull + apply_profile, 不推送本地变更。
        """
        from .threads.auto_sync_thread import AutoSyncThread

        def work():
            import wyckoff.profile_sync as psync
            return psync.pull_or_push("pull")

        self._account_sync_thread = AutoSyncThread(work, self)

        def _finish(res):
            if isinstance(res, dict) and res.get("ok"):
                self._status("从远端同步私有数据完成")
                self.reload_watchlist()
            else:
                self._status(
                    f"从远端同步失败: {(res or {}).get('error', '未知错误')}")
            self._refresh_account_state()

        self._account_sync_thread.result.connect(_finish)
        self._account_sync_thread.start()


    # ── 自选股 ──
    def reload_watchlist(self):
        self._reload_watchlist()

    def _watch_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        it = self.watch_list.itemAt(pos)
        menu = QMenu(self)
        a_analyze = menu.addAction("加载分析")
        menu.addSeparator()
        a_up = menu.addAction("上移")
        a_down = menu.addAction("下移")
        menu.addSeparator()
        a_del = menu.addAction("从自选删除")
        action = menu.exec(self.watch_list.mapToGlobal(pos))
        if action is None:
            return
        if action == a_analyze:
            if it is not None:
                self._load_code(it.data(Qt.ItemDataRole.UserRole))
        elif action == a_del:
            if it is not None:
                code = it.data(Qt.ItemDataRole.UserRole)
                if code in self._watchlist:
                    self._watchlist.remove(code)
                    save_watchlist(self._watchlist)
                    self._reload_watchlist()
        elif action == a_up or action == a_down:
            if it is None:
                return
            code = it.data(Qt.ItemDataRole.UserRole)
            i = self._watchlist.index(code) if code in self._watchlist else -1
            if i < 0:
                return
            j = i - 1 if action == a_up else i + 1
            if 0 <= j < len(self._watchlist):
                self._watchlist[i], self._watchlist[j] = self._watchlist[j], self._watchlist[i]
                save_watchlist(self._watchlist)
                self._reload_watchlist()
                self._select_watch(code)

    def _reload_watchlist(self):
        raw = load_watchlist()
        normalized = []
        for c in raw:
            try:
                normalized.append(normalize_symbol(c))
            except ValueError:
                normalized.append(c)
        self._watchlist = normalized
        self._watch_names = {}
        self.watch_list.clear()
        for code in self._watchlist:
            name = ""
            try:
                bare = code[2:] if len(code) == 8 and code[:2] in ("sh", "sz", "bj") else code
                r = local_search_stock(bare, limit=1)
                if r:
                    name = r[0].get("name", "")
            except Exception as e:
                log_exc("_reload_watchlist 解析股票名失败", e)
                name = ""
            self._watch_names[code] = name
            self._add_watch_item(code, name)
        if self._current_code:
            self._select_watch(self._current_code)
        self._refresh_watch_rt()
        self._schedule_accuracy_eval()
        self._schedule_auto_sync()

    def _add_watch_item(self, code, name):
        from PyQt6.QtCore import QSize

        from .watch_card import ROLE_NAME, ROLE_PCT, ROLE_PRICE, ROLE_TAG, ROLE_TAG_COLOR
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, code)
        item.setData(ROLE_NAME, name or "")
        item.setData(ROLE_TAG, "")
        item.setData(ROLE_TAG_COLOR, theme.C_MUTED)
        item.setData(ROLE_PRICE, None)
        item.setData(ROLE_PCT, None)
        item.setSizeHint(QSize(120, 40))
        self.watch_list.addItem(item)
        return item

    def _refresh_watch_rt(self):
        """后台拉取自选股实时行情 + 阶段分类, 更新卡片数据。"""
        codes = list(self._watchlist)
        if not codes:
            return
        self._watch_move_at = time.monotonic()
        th = WatchRTThread(codes, self)
        th.result.connect(self._on_watch_rt)
        self._rt_threads[th] = th
        th.start()

    def _on_watch_rt(self, rt, phases):
        from .watch_card import ROLE_NAME, ROLE_PCT, ROLE_PRICE, ROLE_TAG, ROLE_TAG_COLOR, tag_for
        self._last_rt = rt
        if rt:
            self._watch_rt_seen = True
        for i in range(self.watch_list.count()):
            item = self.watch_list.item(i)
            if item is None:
                continue
            code = item.data(Qt.ItemDataRole.UserRole)
            # 按完整符号精确匹配: 指数 sh000001 与同代码个股 sz000001 是两只不同标的,
            # 不能只按 6 位裸代码查 (会互相顶掉, 出现两只同名卡片)。
            try:
                sym = normalize_symbol(code)
            except ValueError:
                sym = ""
            info = rt.get(sym) or rt.get(code) if sym else rt.get(code)
            if info:
                if info.get("name"):
                    item.setData(ROLE_NAME, info["name"])
                    self._watch_names[code] = info["name"]
                item.setData(ROLE_PRICE, info.get("price"))
                item.setData(ROLE_PCT, info.get("pct"))
            p = phases.get(code)
            if p:
                tag, color = tag_for(p.get("base"))
                conf = p.get("conf")
                if conf == "high":
                    tag = "✓" + tag
                elif conf == "caution":
                    tag = "!" + tag
                item.setData(ROLE_TAG, tag)
                item.setData(ROLE_TAG_COLOR, color)
        self.watch_list.viewport().update()
        self._check_alerts(rt)
        self._check_watch_moves(rt)
        self._snapshot_watch_pct(rt)
        for th in list(self._rt_threads):
            self._rt_threads.pop(th, None)

    def _snapshot_watch_pct(self, rt):
        """记录本轮自选股涨跌幅, 供异动边沿检测使用。"""
        self._watch_pct_prev = {}
        for code, info in (rt or {}).items():
            if isinstance(info, dict):
                try:
                    self._watch_pct_prev[code] = float(info.get("pct"))
                except (TypeError, ValueError):
                    continue

    def _check_watch_moves(self, rt):
        """自选股涨跌幅异动阈值事件: 边沿触发时提示 + 热刷新行情。"""
        try:
            from wyckoff.alerts import watch_move_events
            thr = float(self.settings.get(S.Watch.MOVE_THRESHOLD, DEFAULTS[S.Watch.MOVE_THRESHOLD]))
            events = watch_move_events(self._watch_pct_prev, rt, thr)
            if not events:
                return
            if bool(self.settings.get(S.Watch.MOVE_NOTIFY,
                                      DEFAULTS[S.Watch.MOVE_NOTIFY])):
                items = "、".join(f"{name}({code}) {pct:+.2f}%" for code, name, pct in events[:5])
                self._status(f"⚡ 自选股异动: {items}", theme.C_AMBER)
            # 冷却期外立刻热刷新, 将最新行情快速拉回 (防刷屏)
            if (bool(self.settings.get(S.Watch.MOVE_REFRESH,
                                       DEFAULTS[S.Watch.MOVE_REFRESH]))
                    and time.monotonic() - self._watch_move_at > 5.0):
                self._refresh_watch_rt()
        except Exception as e:
            log_exc("_check_watch_moves 检测异动失败", e)

    # ── 启动体检阶段进度 (D7) ──
    _BOOT_PHASES = ("界面装载", "全市场拼音索引", "自选股首扫")

    def _advance_boot_progress(self):
        """按真实标记推进阶段: 拼音索引完成 + 自选股首扫回执, 无阻塞。"""
        try:
            now = time.monotonic()
            idx = 0
            if self._full_market_index_done:
                idx = 1
            if (not self._watchlist) or self._watch_rt_seen:
                idx = 2
            if idx != self._boot_phase:
                self._boot_times[self._BOOT_PHASES[self._boot_phase]] = \
                    now - self._boot_phase_t0
                self._boot_phase = idx
                self._boot_phase_t0 = now
            if idx >= len(self._BOOT_PHASES) - 1 or now - self._boot_start > 30.0:
                self._finalize_boot_progress()
            else:
                self.boot_label.setText(
                    f"启动 {idx + 1}/{len(self._BOOT_PHASES)} → "
                    f"{self._BOOT_PHASES[idx]}…")
        except Exception as e:
            log_exc("启动阶段进度推进失败", e)
            self._finalize_boot_progress()

    def _finalize_boot_progress(self):
        """全部阶段就绪 → 体检汇总 + 阶段耗时, 写入状态栏与日志。"""
        if getattr(self, "_boot_done", False):
            return
        self._boot_done = True
        try:
            self._boot_timer.stop()
        except Exception:
            pass
        try:
            from wyckoff.startup_check import format_check_summary, run_startup_check
            self._boot_times[self._BOOT_PHASES[self._boot_phase]] = \
                time.monotonic() - self._boot_phase_t0
            report = run_startup_check(
                n_watch=len(getattr(self, "_watchlist", None) or []),
                rt_filled=bool(getattr(self, "_last_rt", None)),
                scan_done=bool(getattr(self, "_watch_rt_seen", False)))
            self._boot_report = report
            total = time.monotonic() - self._boot_start
            times = " · ".join(f"{k[:2]}{v * 1000:.0f}ms"
                               for k, v in self._boot_times.items())
            brief = format_check_summary(report)
            self.boot_label.setText(f"启动 {total * 1000:.0f}ms 就绪 · {brief}")
            log_msg("startup",
                    "启动体检就绪: " + f"{brief} · 各阶段: {times}")
            self.boot_label.setToolTip(
                "\n".join(f"{k}: {v['msg']}" for k, v in report["items"].items()))
        except Exception as e:
            log_exc("启动体检汇总失败", e)
            self.boot_label.setText("已就绪")

    def _check_alerts(self, rt=None):
        """检查自选股价格/信号预警, 触发时弹窗 + TTS 播报。"""
        try:
            from wyckoff.alerts import check_price_alerts
            hits = check_price_alerts(rt or getattr(self, "_last_rt", {}) or {})
            if hits:
                self._notify_alerts(hits)
        except Exception as e:
            log_exc("_check_alerts 检查预警失败", e)

    def _notify_alerts(self, hits):
        """预警触发提醒: 状态栏 + 弹窗 + TTS (若启用)。"""
        if not hits:
            return
        lines = [t for _c, _n, t in hits]
        text = "\n".join(lines)
        self._status("⚠ " + lines[0], theme.C_AMBER)
        QMessageBox.information(self, "自选股预警", text)
        if bool(self.settings.get(S.TTS.ENABLED, False)):
            try:
                from wyckoff.tts import speak
                clean = "、".join(t.replace("预警: ", "") for _c, _n, t in hits[:3])
                speak(clean, self.settings)
            except Exception:
                pass

    # ── 全局键盘精灵: 非输入控件敲字母/数字 → 弹出搜索 ──
    _INPUT_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox,
                    QTextBrowser)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if self._maybe_open_spirit(event):
                return True
        # 信号汇总改用 FlowLayout 自动换行, 无需监听视口 resize 重排
        return super().eventFilter(obj, event)

    def _maybe_open_spirit(self, event):
        if not self.isVisible():
            return False
        focus = QApplication.focusWidget()
        if isinstance(focus, self._INPUT_TYPES):
            return False
        mods = event.modifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.AltModifier
                   | Qt.KeyboardModifier.MetaModifier):
            return False
        key = event.key()
        if not (Qt.Key.Key_A <= key <= Qt.Key.Key_Z
                or Qt.Key.Key_0 <= key <= Qt.Key.Key_9):
            return False
        ch = chr(int(key))
        if self._spirit is not None and self._spirit.isVisible():
            self._spirit.raise_()
            self._spirit.activateWindow()
        else:
            self._open_spirit(ch)
        return True

    def _open_spirit(self, text=""):
        if self._spirit is None:
            self._spirit = CodeSearchDialog(self)
            self._spirit.picked.connect(self._on_spirit_picked)
        self._spirit.open_with(text)

    def _on_spirit_picked(self, code):
        if code:
            self._load_code(code)

    def _select_watch(self, code):
        for i in range(self.watch_list.count()):
            it = self.watch_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == code:
                self.watch_list.setCurrentItem(it)
                return

    def _on_watch_selected(self, cur, _prev):
        if cur is not None:
            code = cur.data(Qt.ItemDataRole.UserRole)
            if code and code != self._current_code:
                key = self._cache_key(code)
                cached = self._analysis_ctrl._analysis_cache.get(key)
                if cached:
                    self._done(cached)
                else:
                    self._load_code(code)

    def _force_analyze(self, code):
        if code:
            self._current_code = code
            self.on_analyze(force_refresh=True)

    def _cache_key(self, code=None):
        code = code or self._current_code
        sk = self.cb_scale.currentText()
        pk = self.cb_period.currentText()
        return (code, SCALE_OPTIONS.get(sk, 240), PERIOD_OPTIONS.get(pk, 700))

    def add_current_to_watch(self):
        code = self._current_code
        if not code:
            QMessageBox.information(self, "加入自选", "请先分析一只股票, 再将其加入自选股。")
            return
        try:
            full = normalize_symbol(code)
        except ValueError:
            full = code
        if full not in self._watchlist:
            self._watchlist.append(full)
            save_watchlist(self._watchlist)
            self._reload_watchlist()
            self._select_watch(full)
        else:
            self._select_watch(full)

    def remove_watch_item(self):
        it = self.watch_list.currentItem()
        if it is None:
            QMessageBox.information(self, "删除自选", "请先在列表中选择要删除的股票。")
            return
        code = it.data(Qt.ItemDataRole.UserRole)
        if code in self._watchlist:
            self._watchlist.remove(code)
            save_watchlist(self._watchlist)
            self._reload_watchlist()

    # ── 分析 ──
    def _remember_kline_view(self):
        """记录当前 K 线视野, 键为「上次渲染的代码 + 周期」, 切股回来可恢复。

        必须用 _last_symbol (而非 _current_code): _load_code 会先把
        _current_code 改成新标的, 而图上仍是旧标的的视野。"""
        code = str(getattr(self, "_last_symbol", "") or "")
        if not code or getattr(self, "kline_widget", None) is None:
            return
        vr = self.kline_widget.view_range()
        if vr:
            self._view_mem[(code, int(getattr(self, "_last_scale", 240)))] \
                = tuple(vr)

    def keyPressEvent(self, ev):
        """PageUp/PageDown: 在自选股列表中切换上一只/下一只标的。"""
        if ev.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) \
                and getattr(self, "_watchlist", None):
            step = -1 if ev.key() == Qt.Key.Key_PageUp else 1
            codes = list(self._watchlist)
            cur = str(self._current_code or "")
            try:
                cur_full = normalize_symbol(cur)
            except ValueError:
                cur_full = cur
            idx = codes.index(cur_full) if cur_full in codes else -1
            ev.accept()
            self._load_code(codes[(idx + step) % len(codes)])
            return
        super().keyPressEvent(ev)

    def on_analyze(self, force_refresh=False):
        code = self._current_code
        if not code:
            self._status("请先在左侧自选股选择股票", theme.C_AMBER)
            return
        self._chart_mgr.remember_kline_view()
        self._analysis_ctrl.start_analysis(code, force_refresh=force_refresh)

    def on_refresh(self):
        self._refresh_watch_rt()

    def on_scan_entries(self):
        """今日可靠入场点: 只做强梯队(Spring/Shakeout/ST/LPS)+已确认+高置信的
        多头入场点。入口改为主窗口 Tab (置于最后, 首显示自动扫一次); 入场规则
        与实测胜率见 wyckoff.entries (确认可交易口径 20根: Spring ~61% /
        Shakeout ~57%)。双击结果行加载该股分析。
        """
        self._ensure_entry_tab()
        self.tabs.setCurrentWidget(self.entry_tab)
        self.entry_tab.panel.start_scan()

    def _auto_show_tabs(self):
        """启动自动显示 (设置→基本): 综合选股 / 校准中心 / 今日入场点。

        三者都是懒创建 Tab, 默认不自动开; 勾选后启动时即创建并切到对应页。
        同时启用多个时按 综合选股→校准中心→今日入场点 顺序逐个打开, 最终落到
        最后启用的那一页 (今日入场点最后打开时会自动扫一次)。
        """
        s = self.settings
        if bool(s.get(S.General.AUTO_SHOW_SCREENER, False)):
            self._switch_to_screener()
        if bool(s.get(S.General.AUTO_SHOW_CALIB, False)):
            self.open_accuracy_center()
        if bool(s.get(S.General.AUTO_SHOW_ENTRIES, False)):
            self.on_scan_entries()

    def _ensure_entry_tab(self):
        """懒创建 今日入场点 Tab (恒置于最后)。"""
        if self.entry_tab is not None and self.tabs.indexOf(self.entry_tab) >= 0:
            return
        from .extra_windows import EntryPointsTab
        self.entry_tab = EntryPointsTab(self, on_load=self._load_code)
        self.tabs.addTab(self.entry_tab, "今日入场点")
        idx = self.tabs.count() - 1
        self.tabs.setCurrentIndex(idx)

    def _ensure_paper_tab(self):
        """建立自动模拟盘 Tab (常驻, 不可关闭)。"""
        if self.paper_tab is not None:
            return
        from .paper_window import PaperWindow
        # PaperWindow 是 QDialog, 直接作为中央固定 Tab 嵌入.
        self.paper_tab = PaperWindow(self, settings=self.settings,
                                     on_load=self._load_code)
        self.tabs.addTab(self.paper_tab, "模拟盘")
        # 固定 Tab: 去掉关闭按钮 (同 K线图 等分析页), 不可被关闭.
        self.tabs.tabBar().setTabButton(
            self.tabs.indexOf(self.paper_tab),
            self.tabs.tabBar().ButtonPosition.RightSide, None)

    def _load_code(self, code):
        self._current_code = code
        self.on_analyze()

    def _remember_last_analyzed(self):
        """记录本次分析的股票与周期/时间段, 供下次启动时自动重载。"""
        if not self._current_code:
            return
        self._state_mgr.remember_last_analyzed(
            self._current_code,
            self.cb_scale.currentText(),
            self.cb_period.currentText())

    def _sync_analyze_btn(self, analyzing):
        """切换"开始分析"按钮的 分析中/空闲 状态 (文案+QSS属性)."""
        b = self.btn_analyze
        b.setText("正在分析中…" if analyzing else "开始分析")
        b.setProperty("analyzing", analyzing)
        b.style().unpolish(b)
        b.style().polish(b)

    def _start_analysis(self, code, force_refresh):
        """保留兼容: 委托给 AnalysisController。"""
        self._analysis_ctrl.start_analysis(code, force_refresh)

    def _done(self, r):
        self._analyzing = False
        self.btn_analyze.setEnabled(True)
        self._sync_analyze_btn(False)
        # 缓存分析结果
        code = r.get("code", "")
        key = self._cache_key(code)
        self._analysis_ctrl._analysis_cache[key] = r

        # 更新图表缓存
        scale_key = self.cb_scale.currentText()
        period_key = self.cb_period.currentText()
        scale = SCALE_OPTIONS.get(scale_key, 240)
        datalen = PERIOD_OPTIONS.get(period_key, 700)

        self._chart_mgr.update_caches(r, scale, datalen)

        # 批量渲染图表
        self._chart_mgr.render_all(r)

        self._current_code = r["code"]
        self._current_name = r.get("name") or ""
        self._remember_last_analyzed()
        self._last_summary = r["summary"]
        self._last_df = r.get("df")
        self._last_vsa = r.get("vsa_signals")
        self._last_segs = r.get("segs")
        self._last_symbol = r["code"]
        self._last_scale = scale
        self._last_datalen = datalen
        self._update_watch_name(r["code"], self._current_name)
        self._render_summary(r["summary"])
        self._render_sections(r["sections"])
        interp = self._interp_lines(r["sections"])
        self._last_interp_lines = interp
        if interp:
            self.interp_text.setHtml(self._interp_html(interp, self._text_font_size))
        else:
            self._set_interp_placeholder(self._text_font_size)
        market = r.get("market")
        if market:
            # 新闻情绪数据
            news_sentiment = market.get("news_sentiment")
            if news_sentiment:
                self.news_widget.set_data(news_sentiment, r["code"], r.get("name"))
            # 产业链地图: 定位当前股所属板块 (fail-soft)
            try:
                sec_info = market.get("sector") or {}
                self.chain_widget.set_symbol(
                    r["code"], r.get("name"), sec_info.get("name"))
            except Exception:
                pass
        self._update_stock_bar(r["summary"])
        self._select_watch(r["code"])
        self._update_source_health()
        self._refresh_accuracy_window()
        self._schedule_auto_refresh()
        self._accuracy_eval_bg()
        self._chart_mgr.push_analysis_ticker(r)
        self._status(f"完成 {datetime_now()}", theme.C_DOWN)

    def _push_analysis_ticker(self, r):
        """保留兼容: 委托给 ChartManager。"""
        self._chart_mgr.push_analysis_ticker(r)

    def _on_analysis_ticker_msgs(self, msgs):
        if msgs:
            try:
                self.status_ticker.add_messages(msgs)
            except Exception as e:
                log_exc("_on_analysis_ticker_msgs 状态栏写消息失败", e)

    def _error(self, msg, tb):
        self._analyzing = False
        self.btn_analyze.setEnabled(True)
        self._sync_analyze_btn(False)
        self._status(f"分析失败: {msg}", theme.C_UP)
        # 设置错误占位
        self._chart_mgr.set_error_placeholder()
        if tb:
            log_msg("分析失败", tb)
        # 打包应用无控制台: 把完整错误渲染到结论面板, 便于用户/排查
        try:
            html = (f'<div style="color:{theme.C_UP};font-weight:bold;">'
                    f'分析失败: {_esc(str(msg))}</div>'
                    f'<div style="color:{theme.C_MUTED};margin-top:6px;">'
                    f'<pre>{_esc(tb or "")}</pre></div>')
            self.section_text.setHtml(html)
            self.section_list.clear()
            self._section_texts = []
            self._section_htmls = []
        except Exception:
            pass

    # ── 右面板渲染 ──
    def _render_summary(self, cards, cols=None):
        """渲染信号汇总卡片 (FlowLayout 自动换行, 无需手动按视口宽度分列)。"""
        # cols 参数保留兼容旧调用, FlowLayout 下忽略
        # 批量更新: 卡片创建期间禁用重绘 (10+ 卡片时减少闪烁)
        self.summary_scroll.setUpdatesEnabled(False)
        try:
            _clear_layout(self.summary_lay)
            if not cards:
                lbl = QLabel("(待分析)")
                lbl.setStyleSheet(f"color:{theme.C_MUTED};font-size:11pt;padding:8px;")
                self.summary_lay.addWidget(lbl)
                return
            for item in cards:
                tone = item.get("tone", "neutral")
                color = theme.TONE_COLOR.get(tone, theme.C_MUTED)
                card = QWidget()
                card.setObjectName("summaryCard")
                card.setFixedHeight(36)
                card.setStyleSheet(
                    f"QWidget#summaryCard {{ background:{theme.C_PANEL};"
                    f"border:1px solid {theme.C_BORDER};"
                    f"border-radius:{theme.RADIUS['sm']}px; }}"
                    f"QWidget#summaryCard:hover {{ border:1px solid {color}; }}")
                ch = QHBoxLayout(card)
                ch.setContentsMargins(0, 0, 10, 0)
                ch.setSpacing(8)
                from PyQt6.QtGui import QColor as _QC
                from PyQt6.QtGui import QPalette as _QPal
                from PyQt6.QtWidgets import QFrame as _QF
                strip = _QF()
                strip.setFixedWidth(4)
                strip.setAutoFillBackground(True)
                _sp = strip.palette()
                _sp.setColor(_QPal.ColorRole.Window, _QC(color))
                strip.setPalette(_sp)
                ch.addWidget(strip)
                lab = QLabel(str(item.get("label", "")))
                lab.setStyleSheet(
                    f"color:{theme.C_MUTED};font-size:{theme.font_pt('body-sm')}pt;")
                lab.setAlignment(Qt.AlignmentFlag.AlignVCenter)
                val = QLabel(str(item.get("value", "")))
                val.setStyleSheet(
                    f"color:{color};font-weight:bold;font-size:{theme.font_pt('body')}pt;")
                val.setAlignment(Qt.AlignmentFlag.AlignVCenter)
                tip = str(item.get("tooltip") or "")
                if tip:
                    val.setToolTip(tip)
                    lab.setToolTip(tip)
                ch.addWidget(lab, 0, Qt.AlignmentFlag.AlignVCenter)
                ch.addWidget(val, 0, Qt.AlignmentFlag.AlignVCenter)
                ch.addStretch(1)
                self.summary_lay.addWidget(card)
        finally:
            self.summary_scroll.setUpdatesEnabled(True)

    def _section_row_widget(self, _row, title):
        """标签行控件: 仅标题。"""
        from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lab = QLabel(title)
        lay.addWidget(lab, 1)
        return w

    def _render_sections(self, sections):
        self._last_sections = sections
        self.section_list.clear()
        self._section_htmls = []
        self._section_texts = []
        titles = []
        for title, lines in (sections or []):
            if title == "AI解读":
                continue
            titles.append(title)
            self._section_htmls.append(section_html(title, lines, self._text_font_size))
            self._section_texts.append("\n".join(lines or []))
        self._section_titles = list(titles)
        if not titles:
            titles = ["结论"]
            self._sections_empty = True
            self._section_htmls = [
                f'<div style="font-size:{self._text_font_size}pt;color:{theme.C_MUTED};">输入A股代码(如 600104 / sh600104 / 000001), '
                '点击"开始分析", 或在左侧自选股上双击加载。</div>']
            self._section_texts = []
        else:
            self._sections_empty = False
        for i, title in enumerate(titles):
            it = QListWidgetItem()
            it.setSizeHint(QSize(140, 34))
            self.section_list.addItem(it)
            self.section_list.setItemWidget(
                it, self._section_row_widget(i, title))
        self.section_list.setCurrentRow(0)
        if self.section_list.count() > 0:
            self.section_text.setHtml(self._section_htmls[0])

    def _zoom_text(self, delta):
        size = max(7, min(18, self._text_font_size + delta))
        if size == self._text_font_size:
            return
        self._text_font_size = size
        self.settings["text_font_size"] = size
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._apply_text_zoom()
        self._status(f"文字大小: {size}pt")

    def _apply_text_zoom(self):
        if getattr(self, "_last_sections", None) is not None:
            self._render_sections(self._last_sections)
        if getattr(self, "_last_interp_lines", None):
            self.interp_text.setHtml(self._interp_html(self._last_interp_lines, self._text_font_size))
        else:
            self._set_interp_placeholder(self._text_font_size)

    def _on_section_changed(self, row):
        if 0 <= row < len(self._section_htmls):
            self.section_text.setHtml(self._section_htmls[row])

    # ── AI 解读重新生成 ──
    def _on_interp_regenerate(self):
        """对当前分析重新生成 AI 解读 (上一轮可能因网络/模型超时失败)。"""
        from wyckoff.interpret import llm_client
        if not self.settings.get(S.AI.INTERPRET_ENABLED, False) \
                or not (self.settings.get(S.AI.API_KEY, "") or "").strip():
            self._set_interp_placeholder(self._text_font_size)
            self._status("AI 解读未启用, 请在 设置→AI 中开启并配置 API Key", theme.C_MUTED)
            return
        if llm_client(self.settings, require_enabled=False) is None:
            self._set_interp_placeholder(self._text_font_size)
            self._status("AI 客户端不可用, 请检查 设置→AI 配置", theme.C_MUTED)
            return
        sections = getattr(self, "_last_sections", None)
        if not sections:
            self._status("请先完成一次分析", theme.C_MUTED)
            return
        from wyckoff.report import build_export_report
        report = build_export_report(
            getattr(self, "_current_code", "") or "",
            getattr(self, "_current_name", "") or "",
            self.cb_scale.currentText(), self.cb_period.currentText(),
            getattr(self, "_last_df", None), sections,
            summary_cards=getattr(self, "_last_summary", None),
            vsa_signals=getattr(self, "_last_vsa", None),
            scale=getattr(self, "_last_scale", 240))
        self.interp_regenerate_btn.setEnabled(False)
        self.interp_regenerate_btn.setText("生成中...")
        self.interp_text.setHtml(
            f'<div style="color:{theme.C_MUTED};">正在重新生成 AI 解读...</div>')

        def work():
            from wyckoff.interpret import interpret_report
            try:
                return interpret_report(report, self.settings)
            except Exception:
                return None

        th = LabelAiThread(work, self)
        th.result.connect(self._on_interp_regenerated)
        self._label_ai_threads[th] = th
        th.start()

    def _on_interp_regenerated(self, out):
        self.interp_regenerate_btn.setEnabled(True)
        self.interp_regenerate_btn.setText("重新生成")
        for th in list(getattr(self, "_label_ai_threads", {}) or {}):
            self._label_ai_threads.pop(th, None)
        if out:
            lines = out.splitlines()
            self._last_interp_lines = lines
            self.interp_text.setHtml(self._interp_html(lines, self._text_font_size))
            self._status("AI 解读已更新", theme.C_DOWN)
        else:
            self._set_interp_placeholder(self._text_font_size)
            self._status("AI 解读生成失败 (网络/模型错误), 请重试", theme.C_UP)

    def _on_ai_chat(self):
        """打开多轮 AI 问股窗口 (注入当前报告 + 该标的历史信号实证)。"""
        sections = getattr(self, "_last_sections", None)
        if not sections:
            self._status("请先完成一次分析", theme.C_MUTED)
            return
        from wyckoff.ai_chat import build_system_context, symbol_signal_stats
        from wyckoff.report import build_export_report
        report = build_export_report(
            getattr(self, "_current_code", "") or "",
            getattr(self, "_current_name", "") or "",
            self.cb_scale.currentText(), self.cb_period.currentText(),
            getattr(self, "_last_df", None), sections,
            summary_cards=getattr(self, "_last_summary", None),
            vsa_signals=getattr(self, "_last_vsa", None),
            scale=getattr(self, "_last_scale", 240))
        stats = symbol_signal_stats(getattr(self, "_current_code", "") or "")
        ctx = build_system_context(report, stats)
        from .ai_chat_window import AiChatDialog
        code = getattr(self, "_current_code", "") or ""
        name = getattr(self, "_current_name", "") or ""
        dlg = AiChatDialog(self, self.settings, ctx,
                           title=f"AI 问股 · {code} {name}".strip())
        dlg.exec()

    # ── 语音朗读 (TTS) ──
    def _tts_parts(self):
        """返回当前选中标签的朗读文本 [(标题, 正文), ...]。

        切换标签后只朗读当前标签下的内容, 而不是每次都从头开始。
        """
        if getattr(self, "_sections_empty", True):
            return []
        titles = getattr(self, "_section_titles", [])
        texts = getattr(self, "_section_texts", [])
        row = self.section_list.currentRow() if self.section_list.count() > 0 else 0
        if 0 <= row < len(texts):
            t = (texts[row] or "").strip()
            if t:
                title = titles[row] if row < len(titles) else ""
                return [(title, t)]
        if not texts:
            cur = self.section_text.toPlainText().strip()
            if cur:
                return [("", cur)]
        return []

    def _tts_text(self):
        if getattr(self, "_sections_empty", True):
            return ""
        parts = [t.strip() for t in getattr(self, "_section_texts", [])
                 if t and t.strip()]
        if not parts:
            cur = self.section_text.toPlainText().strip()
            if cur:
                parts = [cur]
        return "\n\n".join(parts)

    def _cap_tts_text(self, text, force_cap=None):
        """按 tts_max_chars 限制播报字数, 尽量在句末截断 (不把一句话腰斩)。

        force_cap 传入时无视设置值 (用于 AI 解读等本就受模型输出上限约束的文本,
        避免用户误设过小上限导致朗读中途截断)。
        """
        cap = int(force_cap if force_cap is not None
                  else self.settings.get(S.TTS.MAX_CHARS, 3000) or 0)
        if cap <= 0 or len(text) <= cap:
            return text
        cut = text[:cap]
        for sep in ("。", "！", "？", "；", "\n"):
            idx = cut.rfind(sep)
            if idx > cap // 2:
                return cut[:idx + 1]
        return cut

    def _on_tts_click(self):
        if self._tts_playing:
            from wyckoff.tts import stop
            stop()
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报已停止")
            return
        from wyckoff.tts import is_enabled, speak_sequence
        if not is_enabled(self.settings):
            self._status("语音未启用: 请在 设置→语音播报 中启用并配置引擎", theme.C_MUTED)
            return
        parts = self._tts_parts()
        if not parts:
            self._status("暂无可朗读的解读内容", theme.C_MUTED)
            return
        capped = [(t, self._cap_tts_text(x)) for t, x in parts]
        self._tts_playing = True
        self._sync_tts_btn()
        ok = speak_sequence(capped, self.settings,
                            on_done=lambda ok_, err: self._tts_done_sig.emit(ok_, err))
        if not ok:
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报未能启动: 无可用引擎", theme.C_UP)

    def _on_tts_done(self, ok, err):
        self._tts_playing = False
        self._sync_tts_btn()
        if ok:
            self._status("语音播报完成")
        elif err:
            self._status(f"语音播报失败: {err}", theme.C_UP)

    def _interp_tts_text(self):
        return (self.interp_text.toPlainText() or "").strip()

    def _on_interp_tts_click(self):
        if self._tts_playing:
            from wyckoff.tts import stop
            stop()
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报已停止")
            return
        from wyckoff.tts import is_enabled, speak
        if not is_enabled(self.settings):
            self._status("语音未启用: 请在 设置→语音播报 中启用并配置引擎", theme.C_MUTED)
            return
        text = self._interp_tts_text()
        if not text:
            self._status("暂无可朗读的 AI 解读", theme.C_MUTED)
            return
        text = self._cap_tts_text(text, force_cap=6000)
        self._tts_playing = True
        self._sync_tts_btn()
        ok = speak(text, self.settings,
                   on_done=lambda ok_, err: self._tts_done_sig.emit(ok_, err))
        if not ok:
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报未能启动: 无可用引擎", theme.C_UP)

    def _sync_tts_btn(self):
        playing = self._tts_playing
        btns = []
        if hasattr(self, "tts_btn"):
            btns.append(self.tts_btn)
        if hasattr(self, "interp_tts_btn"):
            btns.append(self.interp_tts_btn)
        for b in btns:
            b.setText("■ 停止" if playing else "▶ 语音朗读")
            b.setProperty("playing", playing)
            b.style().unpolish(b)
            b.style().polish(b)

    @staticmethod
    def _interp_lines(sections):
        for title, lines in (sections or []):
            if title == "AI解读":
                return [ln for ln in lines if (ln or "").strip()]
        return None

    def _interp_html(self, lines, font_size=11):
        parts = ["<div>"]
        for ln in lines:
            stripped = ln.strip()
            if not stripped:
                continue
            if stripped.startswith(("解读:", "结论:")):
                parts.append(f'<p style="font-size:{font_size}pt;color:{theme.C_AMBER};font-weight:bold;">{_esc(stripped)}</p>')
            else:
                parts.append(f'<p style="font-size:{font_size}pt;">{_esc(stripped)}</p>')
        parts.append("</div>")
        return "".join(parts)

    def _set_interp_placeholder(self, font_size=11):
        """解读页占位文案: 按 AI 配置状态区分原因, 避免误导用户。"""
        enabled = bool(self.settings.get(S.AI.INTERPRET_ENABLED, False))
        key = (self.settings.get(S.AI.API_KEY, "") or "").strip()
        if not enabled or not key:
            msg = ('(未启用 AI 解读: 请在 <b>设置→AI</b> 勾选"启用 AI 报告解读"并填入 API Key。'
                   '离线分析不包含此节)')
        else:
            msg = ('(AI 解读已启用, 但本次分析未生成结果: 可能因网络/模型超时或报告过长失败。'
                   '请点击上方 <b>重新生成</b> 按钮重试, 或先完成一次分析。)')
        self.interp_text.setHtml(
            f'<div style="font-size:{font_size}pt;color:{theme.C_MUTED};">{msg}</div>')

    # ── VSA 信号解释 (K线标签点击 / 汇总卡片) ──
    def _signal_stats_html(self, label, conf=None):
        """信号统计块: 本信号置信度 + 该类型历史胜率 (5/20 根)。

        胜率来自信号准确度库 (load_win_rates), 键为 (kind, type);
        置信度为当前所点击事件 bar 的置信度 (VSA 标签无置信度, 为 None)。
        样本 < 10 视为样本不足, 不展示百分数。
        """
        from wyckoff.signal_accuracy import load_win_rates
        from wyckoff.vsa_explain import VSA_EXPLAIN
        kind = "vsa" if label in VSA_EXPLAIN else "event"
        bits = []
        if kind == "event" and isinstance(conf, (int, float)):
            c = int(round(float(conf)))
            color = theme.C_UP if c >= 70 else (theme.C_AMBER if c >= 50 else theme.C_DOWN)
            bits.append(f'信号置信度: <b style="color:{color};">{c}/100</b>')
        rates5 = load_win_rates(5)
        rates20 = load_win_rates(20)
        r5 = rates5.get((kind, str(label)))
        r20 = rates20.get((kind, str(label)))
        if r5 is not None and r5["n"] >= 10:
            bits.append(f'历史胜率(5根): <b>{r5["win"] * 100:.1f}%</b> (样本{r5["n"]})')
        if r20 is not None and r20["n"] >= 10:
            bits.append(f'历史胜率(20根): <b>{r20["win"] * 100:.1f}%</b> (样本{r20["n"]})')
        if not bits:
            bits.append('该信号暂无足够的历史评估样本, 胜率数据待积累')
        return '&nbsp;·&nbsp;'.join(bits)

    def _show_vsa_explain(self, label, conf=None):
        ex = explain(label)
        cn = VSA_CN.get(label) or EVENT_CN.get(label) or label
        from PyQt6.QtWidgets import QDialogButtonBox, QGroupBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"信号 {label} · {cn}")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)

        if not ex:
            txt = f"<b>{label} · {cn}</b><br>暂无结构化解释, 由下方 AI 解读补充。"
        else:
            lines = [
                f"<b>{label} · {cn}</b>",
                f"<br><b>含义</b>: {ex.get('meaning', '')}",
                f"<br><b>方向</b>: {ex.get('direction', '')}",
                f"<br><b>关注点</b>: {ex.get('watch', '')}",
                f"<br><b>建议</b>: {ex.get('advice', '')}",
                f"<br><b>流程角色</b>: {ex.get('role', '')}",
                f"<br><b>失效条件</b>: {ex.get('fail', '')}",
            ]
            if ex.get("direction") and ("偏空" in str(ex.get("direction"))
                                        or "空" in str(ex.get("direction"))):
                lines.append(
                    f"<br><i style='color:{theme.C_MUTED};'>{LONG_ONLY_NOTE}</i>")
            txt = "".join(lines)

        stats_lab = QLabel(self._signal_stats_html(label, conf))
        stats_lab.setWordWrap(True)
        stats_lab.setStyleSheet(f"color:{theme.C_MUTED};"
                                f"font-size:{theme.font_pt('mini')}pt;"
                                f"border:1px solid {theme.C_BORDER};"
                                f"border-radius:{theme.RADIUS['sm']}px;padding:6px;")
        lay.addWidget(stats_lab)

        static = QTextBrowser()
        static.setHtml(txt)
        static.setMaximumHeight(180)
        lay.addWidget(static)

        # ── AI 解读区 (对话框底部) ──
        ai_box = QGroupBox("AI 解读")
        ai_lay = QVBoxLayout(ai_box)
        self._label_ai = label
        self._label_ai_text = QTextEdit()
        self._label_ai_text.setReadOnly(True)
        self._label_ai_text.setPlaceholderText(
            "点击『生成 AI 解读』, 由大模型基于该信号与当前 K 线语境解释。\n"
            "(需在 设置→AI 中启用 AI 解读并配置 API Key)")
        ai_lay.addWidget(self._label_ai_text, 1)
        ai_h = QHBoxLayout()
        btn_ai = QPushButton("生成 AI 解读")
        btn_ai.clicked.connect(
            lambda _=False, b=btn_ai: self._gen_label_ai(label, b))
        ai_h.addWidget(btn_ai)
        btn_ai_tts = QPushButton("▶ 语音朗读")
        btn_ai_tts.setToolTip("朗读下方 AI 解读内容")
        btn_ai_tts.clicked.connect(self._on_label_ai_tts)
        ai_h.addWidget(btn_ai_tts)
        ai_h.addStretch(1)
        ai_lay.addLayout(ai_h)
        lay.addWidget(ai_box, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    def _on_label_ai_tts(self):
        """朗读信号解释对话框内的 AI 解读文本。"""
        if self._tts_playing:
            from wyckoff.tts import stop
            stop()
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报已停止")
            return
        from wyckoff.tts import is_enabled, speak
        if not is_enabled(self.settings):
            self._status("语音未启用: 请在 设置→语音播报 中启用并配置引擎", theme.C_MUTED)
            return
        text = (self._label_ai_text.toPlainText() or "").strip()
        if not text or text.startswith("正在请求"):
            self._status("暂无可朗读的 AI 解读, 请先生成", theme.C_MUTED)
            return
        text = self._cap_tts_text(text, force_cap=6000)
        self._tts_playing = True
        self._sync_tts_btn()
        ok = speak(text, self.settings,
                   on_done=lambda ok_, err: self._tts_done_sig.emit(ok_, err))
        if not ok:
            self._tts_playing = False
            self._sync_tts_btn()
            self._status("语音播报未能启动: 无可用引擎", theme.C_UP)

    def _gen_label_ai(self, label, btn=None):
        """在信号解释对话框内生成该标签的 AI 解读。"""
        from wyckoff.interpret import interpret_tag
        if btn is not None:
            btn.setEnabled(False)
        self._label_ai_text.setPlainText("正在请求 AI 解读...")

        def work():
            try:
                ctx = self._label_ai_context()
                # 用户显式点击 → 只要有 Key 即可调用
                out = interpret_tag(label, self.settings, context=ctx,
                                    require_enabled=False)
            except Exception:
                out = None
            return out

        th = LabelAiThread(work, self)
        th.result.connect(lambda out: self._on_label_ai_done(btn, out))
        self._label_ai_threads[th] = th
        th.start()

    def _label_ai_context(self):
        """当前 K 线语境文本: 股票 + 最近几根 K 线 + 该信号近期出现情况。"""
        df = getattr(self, "_last_df", None)
        parts = []
        code = self._current_code or ""
        name = self._current_name or ""
        if code:
            parts.append(f"股票: {name} ({code})")
        if df is not None and not df.empty:
            tail = df.tail(20)
            try:
                rows = []
                for _, r in tail.iterrows():
                    rows.append(
                        f"{str(r['day'])[:10]} O{r['open']:.2f} H{r['high']:.2f} "
                        f"L{r['low']:.2f} C{r['close']:.2f} V{float(r['volume']):.0f}")
                parts.append("最近20根K线:\n" + "\n".join(rows))
            except Exception:
                pass
        if df is not None:
            phase = getattr(self, "_last_summary", None)
            if phase:
                smap = {c.get("label"): c for c in phase}
                if "阶段" in smap:
                    parts.append(f"当前阶段: {smap['阶段'].get('value', '')}")
        return "\n".join(parts)

    def _on_label_ai_done(self, btn, out):
        if btn is not None:
            try:
                btn.setEnabled(True)
            except RuntimeError:
                pass  # 按钮已被销毁
        for th in list(getattr(self, "_label_ai_threads", {}) or {}):
            self._label_ai_threads.pop(th, None)
        if out:
            self._label_ai_text.setPlainText(out)
        else:
            self._label_ai_text.setPlainText(
                "AI 解读不可用: 未启用 AI 解读 或未配置 API Key / 模型调用失败。\n"
                "请在 设置→AI 中启用『AI 解读』并填入 DeepSeek/OpenAI 兼容 API Key。")

    # ── 信息栏 / 建议 ──
    def _update_stock_bar(self, summary):
        code = self._current_code
        if not code:
            self.stock_info.setText("")
            for w in (self.tb_name, self.tb_code, self.tb_price,
                      self.tb_pct, self.tb_advice):
                w.setText("")
            return
        smap = {c["label"]: c for c in (summary or [])}
        phase = smap.get("阶段", {}).get("value", "")
        advice, tone = self._build_advice(summary)
        mark = "▲" if tone == "buy" else "▼" if tone == "sell" else "●"
        ac = theme.C_UP if tone == "buy" else theme.C_DOWN if tone == "sell" else theme.C_MUTED
        self.stock_info.setText(
            f"{self._current_name}  {code}   "
            f"<span style='color:{theme.C_AMBER};'>{_esc(phase)}</span>   "
            f"<span style='color:{ac};'>{mark} {_esc(advice)}</span>")
        self.stock_info.setTextFormat(Qt.TextFormat.RichText)

        name = self._current_name or ""
        price, pct = self._last_price_pct()
        self.tb_name.setText(name)
        self.tb_code.setText(code)
        self.tb_price.setText(f"{price:.2f}" if price is not None else "-")
        self.tb_price.setStyleSheet(f"color:{theme.C_TEXT};")
        if pct is not None:
            pc = theme.C_UP if pct >= 0 else theme.C_DOWN
            self.tb_pct.setText(f"{pct:+.2f}%" if pct else "0.00%")
            self.tb_pct.setStyleSheet(f"color:{pc};")
        else:
            self.tb_pct.setText("-")
            self.tb_pct.setStyleSheet(f"color:{theme.C_MUTED};")
        self.tb_advice.setText(f"{mark} {advice}")
        self.tb_advice.setStyleSheet(f"color:{ac};")

    def _last_price_pct(self):
        """从缓存 df 取最后收盘价与涨跌幅, 返回 (price, pct), 失败为 (None, None)。"""
        try:
            df = self._last_df
            if df is None or len(df) < 2 or "close" not in df:
                return None, None
            closes = df["close"].dropna()
            if len(closes) < 2:
                return None, None
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            return last, (last / prev - 1) * 100 if prev else None
        except Exception:
            return None, None

    def _build_advice(self, summary):
        if not summary:
            return "", "neutral"
        smap = {c["label"]: c for c in summary}
        phase_val = smap.get("阶段", {}).get("value", "")
        phase_tone = smap.get("阶段", {}).get("tone", "neutral")
        base_phase = phase_val.replace("高置信 ", "").replace(" (需谨慎)", "").split(" ")[0]
        xichou = smap.get("吸筹", {}).get("value", "")
        zhudi = smap.get("筑底", {}).get("value", "")
        has_spring = any(k in xichou + zhudi for k in ("Spring", "弹簧"))
        has_sc = "SC" in zhudi or "卖出高潮" in zhudi
        has_st = "ST" in zhudi
        tupo = smap.get("突破", {}).get("value", "")
        has_joc = "JOC" in tupo
        has_sos = "SOS" in tupo
        jingshi = smap.get("警示", {}).get("value", "")
        has_utad = "UTAD" in jingshi or "上冲派发" in jingshi
        has_bc = "BC" in jingshi or "买入高潮" in jingshi
        dir_tone = smap.get("方向", {}).get("tone", "neutral")
        if phase_tone == "bullish":
            if has_joc:
                return "JOC突破→回踩不破可买入", "buy"
            if has_sos:
                return "SOS强势→回踩不破可买入", "buy"
            if has_spring:
                return "Spring吸筹→放量收回即买点", "buy"
            if has_st:
                return "ST二次测试→确认吸筹有效", "buy"
            if has_sc:
                return "SC卖出高潮→关注突破确认", "buy"
            if has_utad:
                return f"{base_phase}遇到UTAD→警惕假突破,减仓观望", "sell"
            if has_bc:
                return f"{base_phase}出现BC→警惕顶部,分批止盈", "sell"
            return f"{base_phase}→持有/回调关注", "buy"
        elif phase_tone == "bearish":
            if has_utad:
                return "UTAD派发→减仓/空仓观望", "sell"
            if has_bc:
                return "BC买入高潮→警惕反转,离场", "sell"
            if has_spring:
                return f"{base_phase}出现Spring→可能是假弹簧,等二次确认", "neutral"
            if has_joc or has_sos:
                return f"{base_phase}中出现突破→可能是陷阱,等待回踩确认", "neutral"
            if has_st:
                return f"{base_phase}中ST仅视为反抽→非底部买点,等待底部结构完成", "neutral"
            if dir_tone == "bullish":
                return f"{base_phase}但短期偏多→反弹减仓机会", "sell"
            return f"{base_phase}→减仓/反弹离场", "sell"
        else:
            if has_spring:
                return "Spring弹簧→关注放量突破上沿", "buy"
            if has_joc:
                return "JOC突破→区间上沿突破,确认后入场", "buy"
            if has_sos:
                return "SOS强势→关注能否有效突破区间", "buy"
            if has_utad:
                return "UTAD派发→关注区间下沿支撑", "sell"
            if has_bc:
                return "BC高潮→警惕向下突破区间", "sell"
            if has_st:
                return "ST二次测试→关注能否守住下沿", "neutral"
            if dir_tone == "bullish":
                return "偏多震荡→关注回踩买点", "buy"
            elif dir_tone == "bearish":
                return "偏空震荡→等待企稳信号", "sell"
            return "区间整理→观望等待方向选择", "neutral"

    def _update_watch_name(self, code, name):
        from .watch_card import ROLE_NAME
        if name and code in self._watch_names and not self._watch_names[code]:
            self._watch_names[code] = name
        for i in range(self.watch_list.count()):
            it = self.watch_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == code:
                it.setData(ROLE_NAME, name or "")
                it.setData(Qt.ItemDataRole.DisplayRole, None)
                self.watch_list.viewport().update()
                return

    # ── 自动刷新 / 设置 ──
    def _schedule_auto_refresh(self):
        self._cancel_auto_refresh()
        if not bool(self.settings.get(S.Auto.AUTO_REFRESH, False)):
            return
        try:
            interval = max(10, int(self.settings.get(S.Auto.REFRESH_INTERVAL, 30)))
        except (TypeError, ValueError):
            interval = 30
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._on_auto_tick)
        self._refresh_timer.start(interval * 1000)
        self._schedule_accuracy_eval()

    def _schedule_accuracy_eval(self):
        """后台定期评估到期的准确度/信号记录 (不依赖分析动作触发)。"""
        self._cancel_accuracy_eval()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._accuracy_eval_bg)
        timer.start(60 * 60 * 1000)  # 每小时一次, 与 run_pending_eval 的 min_interval 对齐
        self._acc_eval_timer = timer
        self._schedule_auto_scan()

    def _cancel_accuracy_eval(self):
        t = getattr(self, "_acc_eval_timer", None)
        if t is not None:
            t.stop()
        self._acc_eval_timer = None

    def _schedule_auto_scan(self):
        """后台定期重算自选股威科夫信号 + 更新信号准确度 (设置 auto_scan 时启用)。"""
        self._cancel_auto_scan()
        if not bool(self.settings.get(S.Auto.AUTO_SCAN, False)):
            return
        try:
            interval = max(30, int(self.settings.get(S.Auto.SCAN_INTERVAL, 3600)))
        except (TypeError, ValueError):
            interval = 3600
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._auto_scan_watchlist)
        timer.start(interval * 1000)
        self._scan_timer = timer

    def _cancel_auto_scan(self):
        t = getattr(self, "_scan_timer", None)
        if t is not None:
            t.stop()
        self._scan_timer = None

    def _auto_scan_watchlist(self):
        """后台扫描自选股信号 (静默, 不弹窗), 完成后重新调度。"""
        self._cancel_auto_scan()
        codes = list(self._watchlist)
        if codes:
            try:
                th = WatchScanThread(codes, self)
                th.result.connect(self._on_watch_scan)
                self._scan_threads[th] = th
                th.start()
            except Exception as e:
                log_exc("启动定时扫描线程失败", e)
        else:
            self._schedule_auto_scan()

    def _startup_ticker_scan(self):
        """启动后一次性扫描自选股, 立即填充状态栏头条 (避免等 auto_scan 首轮到点)。"""
        try:
            if not getattr(self, "_watchlist", None):
                return
            if self._scan_threads:
                return  # 已在扫描
            self.status_ticker.set_messages(
                [(f"正在扫描 {len(self._watchlist)} 只自选股...", theme.C_MUTED, "")])
            self._auto_scan_watchlist()
        except Exception as e:
            log_exc("_startup_ticker_scan 失败", e)

    def _on_watch_scan(self, payload):
        # 兼容 3-tuple (旧) / 4-tuple (新: 第4位是后台线程已构建好的 msgs)
        if isinstance(payload, tuple) and len(payload) == 4:
            _ok, sig_by_code, rich, msgs = payload
        elif isinstance(payload, tuple) and len(payload) == 3:
            _ok, sig_by_code, rich = payload
            msgs = None  # 旧线程没构建, 这里不主动重算以免卡UI
        elif isinstance(payload, dict):
            _ok, sig_by_code, rich, msgs = payload, payload, {}, None
        else:
            _ok, sig_by_code, rich, msgs = payload, {}, {}, None
        for th in list(getattr(self, "_scan_threads", {}) or {}):
            self._scan_threads.pop(th, None)
        try:
            from wyckoff.alerts import check_signal_alerts
            hits = check_signal_alerts(sig_by_code or {})
            if hits:
                self._notify_alerts(hits)
        except Exception as e:
            log_exc("_on_watch_scan 检查信号预警失败", e)
        # 高实测命中事件/VSA → 状态栏中间滚动头条
        # msgs 已由 WatchScanThread 在后台线程构建完成, UI 线程只做一次 set_messages (<1ms)
        try:
            final_msgs = msgs or []
            if not final_msgs:
                n_scanned = len(rich or {})
                if n_scanned:
                    final_msgs = [(f"自选股 {n_scanned} 只扫描完成: 暂无高实测命中信号",
                                   theme.C_MUTED, "")]
            self.status_ticker.set_messages(final_msgs)
        except Exception as e:
            log_exc("_on_watch_scan 写状态栏头条失败", e)
        # 顺便评估到期记录, 并继续下一轮
        self._accuracy_eval_bg()
        self._schedule_auto_scan()

    def _cancel_auto_refresh(self):
        t = getattr(self, "_refresh_timer", None)
        if t is not None:
            t.stop()
        self._refresh_timer = None

    def _on_auto_tick(self):
        if not bool(self.settings.get(S.Auto.AUTO_REFRESH, False)):
            return
        self._refresh_watch_rt()
        self._schedule_auto_refresh()

    # ── 校准数据自动同步 (变更后去抖) ──
    def _schedule_auto_sync(self):
        """周期检查校准数据是否有变更待同步 (auto_sync 开启时调度)。"""
        self._cancel_auto_sync()
        if not bool(self.settings.get(S.Auto.AUTO_SYNC, False)):
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._auto_sync_check)
        timer.start(15 * 1000)
        self._auto_sync_timer = timer

    def _cancel_auto_sync(self):
        t = getattr(self, "_auto_sync_timer", None)
        if t is not None:
            t.stop()
        self._auto_sync_timer = None

    def _auto_sync_check(self):
        """满足条件则后台启动同步: 有待同步变更 + 去抖窗口已过 + 已配置仓库。"""
        self._cancel_auto_sync()
        try:
            if not bool(self.settings.get(S.Auto.AUTO_SYNC, False)):
                return
            from sync import auto as sync_auto

            if not sync_auto.pending():
                self._schedule_auto_sync()
                return
            th = getattr(self, "_auto_sync_th", None)
            if th is not None and th.isRunning():
                self._schedule_auto_sync()
                return
            try:
                debounce = max(15, int(self.settings.get(S.Auto.SYNC_DEBOUNCE, 60)))
            except (TypeError, ValueError):
                debounce = 60
            if time.time() - sync_auto.last_change_ts() < debounce:
                self._schedule_auto_sync()
                return
            if not str(self.settings.get(S.Runtime.CALIB_REPO_URL) or "").strip():
                # 未配置仓库: 清掉待同步标记, 避免每 15s 空转 (配置后下一次变更会再触发)
                sync_auto.reset()
                self._schedule_auto_sync()
                return
        except Exception as e:
            log_exc("自动同步调度失败", e)
            self._schedule_auto_sync()
            return

        def _work():
            from sync import service as sync_service

            r = sync_service.sync()
            try:
                r["remote_info"] = sync_service.status()
            except Exception:
                pass
            return r

        th = AutoSyncThread(_work, self)
        th.result.connect(lambda res: self._on_auto_sync_done(th, res))
        self._auto_sync_th = th
        th.start()

    def _on_auto_sync_done(self, th, result):
        if getattr(self, "_auto_sync_th", None) is th:
            self._auto_sync_th = None
        try:
            from sync.auto import reset as sync_auto_reset

            sync_auto_reset()
        except Exception:
            pass
        try:
            if isinstance(result, dict) and result.get("ok"):
                n_new = (int(result.get("signals_new", 0) or 0)
                         + int(result.get("feedback_new", 0) or 0))
                n_upd = (int(result.get("signals_upd", 0) or 0)
                         + int(result.get("feedback_upd", 0) or 0))
                msg = f"自动同步完成: 新增 {n_new}, 更新 {n_upd}"
                if result.get("retrained"):
                    msg += ", 已重训模型"
                self.status_ticker.set_messages([(msg, theme.C_MUTED, "")])
                # 校准中心已打开则刷新其模型/同步状态
                win = getattr(self, "_ac_win", None)
                if win is not None and win.isVisible():
                    win.render_all()
            elif isinstance(result, dict) and result.get("error"):
                self.status_ticker.set_messages(
                    [(f"自动同步失败: {result['error']}", theme.C_AMBER, "")])
        except Exception as e:
            log_exc("自动同步结果展示失败", e)
        self._schedule_auto_sync()

    # ── 图表导出 ──
    def _export_current_fig(self):
        idx = self.tabs.currentIndex()
        if idx == 0:
            self._save_kline_png(f"wyckoff_{self._current_code or 'chart'}")
            return
        if idx == 1:
            self._save_pnf_png(f"wyckoff_{self._current_code or 'chart'}")
            return
        elif idx == 2:
            self._save_ind_png(f"wyckoff_{self._current_code or 'chart'}")
            return
        elif idx == 3:
            self._save_mkt_png(f"wyckoff_{self._current_code or 'chart'}")
            return

    def _export_all_figs(self):
        code = self._current_code or "chart"
        n = 0
        self._save_kline_png(f"wyckoff_{code}_kline", quiet=True)
        n += 1
        self._save_ind_png(f"wyckoff_{code}_ind", quiet=True)
        n += 1
        self._save_mkt_png(f"wyckoff_{code}_mkt", quiet=True)
        n += 1
        self._save_pnf_png(f"wyckoff_{code}_pnf", quiet=True)
        n += 1
        QMessageBox.information(self, "导出图表", f"已导出 {n} 张图表到数据目录。")

    def _stamp_pixmap(self, pm, title=""):
        """给导出的图表加时间戳水印 (左下角 时间 + 股票)。"""
        from datetime import datetime

        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QFont
        painter = QPainter(pm)
        font = QFont(self.font())
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QColor(120, 130, 150, 200))
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{title}  {ts}" if title else ts
        painter.drawText(QPointF(6, pm.height() - 8), text)
        painter.end()
        return pm

    def _save_kline_png(self, base, quiet=False):
        from wyckoff.paths import DATA_DIR
        pm = self.kline_widget.grab_pixmap()
        self._stamp_pixmap(pm, title=self._current_name or self._current_code or "")
        path = os.path.join(DATA_DIR, f"{base}.png")
        pm.save(path)
        if not quiet:
            QMessageBox.information(self, "导出图表", f"已保存:\n{path}")
        else:
            self._status(f"已保存 {path}", theme.C_DOWN)

    def _save_pnf_png(self, base, quiet=False):
        from wyckoff.paths import DATA_DIR
        pm = self.pnf_widget.grab_pixmap()
        self._stamp_pixmap(pm, title=self._current_name or self._current_code or "")
        path = os.path.join(DATA_DIR, f"{base}.png")
        pm.save(path)
        if not quiet:
            QMessageBox.information(self, "导出图表", f"已保存:\n{path}")
        else:
            self._status(f"已保存 {path}", theme.C_DOWN)

    def _save_ind_png(self, base, quiet=False):
        from wyckoff.paths import DATA_DIR
        pm = self.ind_widget.grab_pixmap()
        self._stamp_pixmap(pm, title=self._current_name or self._current_code or "")
        path = os.path.join(DATA_DIR, f"{base}.png")
        pm.save(path)
        if not quiet:
            QMessageBox.information(self, "导出图表", f"已保存:\n{path}")
        else:
            self._status(f"已保存 {path}", theme.C_DOWN)

    def _save_mkt_png(self, base, quiet=False):
        from wyckoff.paths import DATA_DIR
        pm = self.mkt_widget.grab_pixmap()
        self._stamp_pixmap(pm, title=self._current_name or self._current_code or "")
        path = os.path.join(DATA_DIR, f"{base}.png")
        pm.save(path)
        if not quiet:
            QMessageBox.information(self, "导出图表", f"已保存:\n{path}")
        else:
            self._status(f"已保存 {path}", theme.C_DOWN)

    # ── 校准中心 ──
    def open_accuracy_center(self, select=0):
        """切换到主窗口内嵌的校准中心 Tab (select 为其内部页索引)。"""
        from .main_view import _LazyCalibContainer
        win = getattr(self, "_ac_win", None)
        if win is None:  # 兜底: 重建场景下重新挂载
            win = _LazyCalibContainer(self.settings, self)
            self._ac_win = win
            self._ac_container = win
        idx = self.tabs.indexOf(win)
        if idx < 0:
            anchor = self.tabs.indexOf(self.tab_screener) if self.tab_screener else -1
            # 无综合选股时追加到末尾, 避免 insertTab(0) 把校准中心插到第一页
            pos = anchor + 1 if anchor >= 0 else self.tabs.count()
            idx = self.tabs.insertTab(pos, win, "校准中心")
        # 批量改数据/切页期间挂起懒渲染, end_update 统一渲总览+当前页
        win.begin_update()
        try:
            self.tabs.setCurrentIndex(idx)
            win.refresh_sync_url()
            win.refresh_feedback(self._last_segs, self._last_symbol,
                                 self._last_datalen, self._last_scale,
                                 self._last_df)
            win.tabs.setCurrentIndex(max(0, min(5, int(select))))
        finally:
            win.end_update()

    def _refresh_accuracy_window(self):
        win = getattr(self, "_ac_win", None)
        if win is not None and win.isVisible():
            win.refresh_feedback(self._last_segs, self._last_symbol,
                                 self._last_datalen, self._last_scale, self._last_df)
            win.render_all()

    def _accuracy_eval_bg(self):
        import threading

        def worker():
            try:
                from wyckoff.accuracy import run_auto_accuracy_eval
                from wyckoff.signal_accuracy import expire_stale_signals, run_auto_signal_eval
                n = run_auto_accuracy_eval()
                ns = run_auto_signal_eval()
                nd = expire_stale_signals()
                if (n or ns or nd):
                    # Qt 控件只能在主线程访问: 通过信号把刷新调度回主线程
                    self._accuracy_done_sig.emit()
            except Exception as e:
                log_exc("准确度后台评估失败", e)
        threading.Thread(target=worker, daemon=True).start()

    def _on_accuracy_done(self):
        """主线程刷新准确度窗口 + 校准提醒 (由 _accuracy_done_sig 触发)。"""
        try:
            win = getattr(self, "_ac_win", None)
            if win is not None:
                win.render_all()
        except Exception as e:
            log_exc("刷新准确度窗口失败", e)
        try:
            self._calibration_remind()
        except Exception as e:
            log_exc("校准提醒失败", e)

    def _calibration_remind(self):
        try:
            from wyckoff.accuracy import accuracy_stats, load_accuracy
            from wyckoff.calibration import calibration_status
            from wyckoff.signal_accuracy import load_signals, signal_stats
            from wyckoff.storage import load_feedback
            acc = accuracy_stats(load_accuracy())
            sig = signal_stats(load_signals())
            due, msg = calibration_status(acc["evaluated"],
                                          sig["summary"]["evaluated"],
                                          len(load_feedback()))
            if due:
                self._status(f"校准提醒: {msg}", theme.C_AMBER)
        except Exception as e:
            log_exc("_calibration_remind 失败", e)

    # ── 候选池 / 行业 ──
    # ── 单例弹窗工厂 (此前 10 个 open_* 各自复制同一模板) ──
    def _show_win(self, attr, factory, refresh=True):
        """单例打开器: attr 为窗口缓存属性名; 已存在则 (可选)refresh+置顶。
        返回窗口实例。"""
        win = getattr(self, attr, None)
        if win is None:
            win = factory()
            setattr(self, attr, win)
        elif refresh and hasattr(win, "refresh"):
            win.refresh()
        win.show()
        win.raise_()
        return win

    def open_candidates(self):
        from .extra_windows import CandidatesWindow
        self._show_win("_cand_win",
                       lambda: CandidatesWindow(self, on_load=self._load_code))

    def open_sector(self):
        from .extra_windows import SectorWindow
        self._show_win("_sector_win",
                       lambda: SectorWindow(self, on_load=self._load_code))

    def _switch_to_screener(self):
        idx = self.tabs.indexOf(self.tab_screener) if self.tab_screener else -1
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
            return
        self.tab_screener = QWidget()
        self._build_screener_tab()
        eidx = self.tabs.indexOf(self.entry_tab) if self.entry_tab is not None else -1
        if eidx >= 0:
            # "今日入场点"恒为最后一个 Tab, 综合选股插到它前面
            idx = self.tabs.insertTab(eidx, self.tab_screener, "综合选股")
        else:
            self.tabs.addTab(self.tab_screener, "综合选股")
            idx = self.tabs.count() - 1
        self.tabs.setCurrentIndex(idx)

    def _on_tab_close(self, index):
        widget = self.tabs.widget(index)
        if widget is self.tab_screener:
            self.tabs.removeTab(index)
            self.tab_screener.deleteLater()
            self.tab_screener = None
            self.screener_widget = None  # 已销毁, 防主题切换时访问野指针
        elif self._ac_win is not None and widget is self._ac_win:
            # 关闭校准中心 Tab: 销毁并置空, 下次 Ctrl+Shift+A 重新挂载
            self.tabs.removeTab(index)
            widget.deleteLater()
            self._ac_win = None
            self._ac_container = None
        elif widget is self.entry_tab:
            self.tabs.removeTab(index)
            widget.deleteLater()
            self.entry_tab = None

    # ── 批量扫描 ──
    def scan_watchlist(self):
        from wyckoff.storage import load_watchlist
        codes = load_watchlist()
        if not codes:
            self._status("自选股为空, 先添加股票", theme.C_UP)
            return
        self._open_scan(codes, "自选股信号扫描")

    def scan_market(self):
        self._status("正在获取全市场活跃股列表 ...", theme.C_AMBER)
        th = ScanMarketThread(self)
        th.result.connect(self._on_scan_market_codes)
        th.finished.connect(
            lambda: self._scan_threads.pop(th, None)
            if th in self._scan_threads else None)
        self._scan_threads[th] = th
        th.start()

    def _on_scan_market_codes(self, codes, title):
        if not codes:
            self._status("全市场扫描失败", theme.C_UP)
            return
        self._open_scan(codes, title)

    def _open_scan(self, codes, title):
        win = ScanWindow(codes, title, self, on_load=self._load_code)
        win.show()
        win.raise_()

    def open_scan_center(self):
        """打开扫描中心 (13 类专项扫描)。"""
        from .extra_windows import ScanCenterWindow
        self._show_win("_scan_center_win",
                       lambda: ScanCenterWindow(self, on_load=self._load_code),
                       refresh=False)

    # ── 国家队工具 ──
    def open_nteam(self):
        self._show_win("_nteam_win",
                       lambda: NteamWindow(self, on_load=self._load_code))

    def open_holdings(self):
        code = self._current_code or ""
        if not code:
            QMessageBox.information(
                self, "提示",
                "请先分析一只股票 (工具栏输入代码/拼音, 或双击自选股),\n"
                "再打开国家队持仓透视。")
            return
        name = ""
        try:
            from wyckoff.datasource import fetch_name
            name = fetch_name(code) or ""
        except Exception:
            pass
        win = getattr(self, "_holdings_win", None)
        if win is None or not win.isVisible():
            win = HoldingsWindow(code, name, self)
            self._holdings_win = win
        win.show()
        win.raise_()

    def open_portfolio(self):
        """打开我的持仓 (个人持仓簿, 盈亏/止损跟踪)。"""
        self._show_win("_portfolio_win",
                       lambda: PortfolioWindow(self, on_load=self._load_code))

    def open_alerts(self):
        """打开自选股预警管理窗口。"""
        self._show_win("_alerts_win", lambda: AlertsWindow(self))

    def open_notes(self):
        """打开当前股票的自选备注。"""
        code = self._current_code or ""
        if not code:
            QMessageBox.information(
                self, "提示",
                "请先分析一只股票 (工具栏输入代码/拼音, 或双击自选股),\n"
                "再为它添加备注。")
            return
        try:
            code = normalize_symbol(code)[2:]
        except Exception:
            pass
        name = self._current_name or ""
        win = NotesWindow(code, name, self)
        win.show()
        win.raise_()

    def open_compare(self):
        """多股票对比: 默认对比自选股, 空自选股时提示先加自选。"""
        codes = list(self._watchlist)
        if not codes:
            QMessageBox.information(
                self, "提示",
                "请先在自选股中添加要对比的股票 (菜单: 文件→添加当前股票到自选股)")
            return
        win = getattr(self, "_compare_win", None)
        if win is None or not win.isVisible():
            win = CompareWindow(codes, self, on_load=self._load_code)
            self._compare_win = win
        else:
            win._codes = codes
            win.refresh()
        win.show()
        win.raise_()

    def open_etf_monitor(self):
        self._show_win("_etf_win", lambda: EtfMonitorWindow(self))

    # ── 指数分析 ──
    def _open_index_dialog(self):
        from wyckoff.indices import INDEX_CATALOG, search_index
        dlg = QDialog(self)
        dlg.setWindowTitle("A股主要指数")
        dlg.resize(420, 520)
        lay = QVBoxLayout(dlg)
        ed = QLineEdit()
        ed.setPlaceholderText("输入名称/代码/拼音搜索指数, 双击加载分析")
        lay.addWidget(ed)
        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        lay.addWidget(lst, 1)

        def refill(kw):
            lst.clear()
            items = INDEX_CATALOG if not kw else search_index(kw)
            for it in items:
                item = QListWidgetItem(f"{it['name']}  {it['symbol']}  ({it['category']})")
                item.setData(Qt.ItemDataRole.UserRole, it["symbol"])
                lst.addItem(item)

        refill("")
        ed.textChanged.connect(lambda t: refill(t.strip()))
        lst.itemDoubleClicked.connect(lambda it: self._load_code(
            it.data(Qt.ItemDataRole.UserRole)))
        lst.itemDoubleClicked.connect(dlg.accept)
        lay.addWidget(lst)
        dlg.exec()

    # ── 导出报告 ──
    def export_report(self):
        code = self._current_code
        if not code:
            self._status("请先完成一次分析再导出", theme.C_UP)
            return
        import os
        from datetime import datetime

        from wyckoff.paths import DATA_DIR
        from wyckoff.utils import normalize_symbol
        out_dir = QFileDialog.getExistingDirectory(
            self, "选择导出目录", DATA_DIR)
        if not out_dir:
            return
        try:
            symbol = normalize_symbol(code)
        except ValueError:
            symbol = code
        base = f"wx_report_{symbol}"
        txt_path = os.path.join(out_dir, f"{base}.txt")
        png_path = os.path.join(out_dir, f"{base}.png")
        pnf_path = os.path.join(out_dir, f"{base}_pnf.png")
        ind_path = os.path.join(out_dir, f"{base}_ind.png")
        mkt_path = os.path.join(out_dir, f"{base}_mkt.png")
        # 完整报告 (量化速览+信号汇总+全部分节+近期K线明细), 而非当前单个标签,
        # 便于直接喂给 DeepSeek 等大模型做精准解读
        from wyckoff.report import build_export_report
        text = build_export_report(
            symbol, getattr(self, "_current_name", "") or "",
            self.cb_scale.currentText(), self.cb_period.currentText(),
            getattr(self, "_last_df", None),
            getattr(self, "_last_sections", None) or [],
            summary_cards=getattr(self, "_last_summary", None),
            vsa_signals=getattr(self, "_last_vsa", None),
            scale=getattr(self, "_last_scale", 240))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n\n")
            f.write(text)
        try:
            self.kline_widget.grab_pixmap().save(png_path)
            self.pnf_widget.grab_pixmap().save(pnf_path)
            self.ind_widget.grab_pixmap().save(ind_path)
            self.mkt_widget.grab_pixmap().save(mkt_path)
        except Exception:
            import traceback
            traceback.print_exc()
        self._status(f"报告已导出: {txt_path}", theme.C_DOWN)

    # ── 清除缓存 / 帮助 ──
    def _clear_market_cache(self):
        from wyckoff import datasource, sqldb
        stats = sqldb.cache_stats()
        sqldb.clear_cache()
        with datasource._KLINE_LOCK:
            datasource._KLINE_CACHE.clear()
            datasource._FACTOR_CACHE.clear()
        with _ANALYSIS_LOCK:
            _ANALYSIS_CACHE.clear()
        QMessageBox.information(
            self, "清除缓存",
            f"已清除本地行情缓存:\n"
            f"K线 {stats['kline_rows']} 条 · 复权因子 {stats['qfq_rows']} 条\n"
            f"释放磁盘占用 {stats['db_bytes'] / 1024:.0f} KB")

    def show_help(self):
        from PyQt6.QtGui import (
            QKeySequence,
            QShortcut,
            QTextCharFormat,
            QTextCursor,
            QTextDocument,
        )
        from PyQt6.QtWidgets import QTextEdit
        dlg = QDialog(self)
        dlg.setWindowTitle("使用说明")
        dlg.resize(880, 720)
        # 允许在任务栏/标题栏最大化
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        lay = QVBoxLayout(dlg)

        # 搜索行
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 6)
        edit = QLineEdit()
        edit.setPlaceholderText("搜索关键词 (回车跳转下一处, Ctrl+F 聚焦本框) ...")
        edit.setClearButtonEnabled(True)
        cnt = QLabel("")
        cnt.setMinimumWidth(90)
        btn_next = QPushButton("下一处")
        srow.addWidget(edit, 1)
        srow.addWidget(cnt)
        srow.addWidget(btn_next)
        lay.addLayout(srow)

        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(self._help_html())
        lay.addWidget(tb, 1)

        hb = QHBoxLayout()
        hb.addStretch(1)
        btn = QPushButton("关闭")
        btn.setDefault(True)
        btn.clicked.connect(dlg.accept)
        hb.addWidget(btn)
        lay.addLayout(hb)

        _fmt = QTextCharFormat()
        # 琥珀底+深字: 两种主题下对比度都足够
        _fmt.setBackground(QColor(theme.C_AMBER))
        _fmt.setForeground(QColor("#1a2238"))

        def _move_to(hit):
            if not hit.isNull():
                tb.setTextCursor(hit)
                tb.ensureCursorVisible()

        def _apply(text):
            text = str(text).strip()
            if not text:
                tb.setExtraSelections([])
                cnt.setText("")
                return
            doc = tb.document()
            flags = QTextDocument.FindFlag.FindCaseSensitively
            selections = []
            cur = QTextCursor(doc)
            n = 0
            while True:
                hit = doc.find(text, cur, flags)
                if hit.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = hit
                sel.format = _fmt
                selections.append(sel)
                n += 1
                cur = hit
            tb.setExtraSelections(selections)
            cnt.setText(f"{n} 处" if n else "无匹配")

        def _jump_forward():
            text = edit.text().strip()
            if not text:
                return
            doc = tb.document()
            flags = QTextDocument.FindFlag.FindCaseSensitively
            cur = tb.textCursor()
            cur.setPosition(cur.selectionEnd())
            hit = doc.find(text, cur, flags)
            if hit.isNull():
                hit = doc.find(text, QTextCursor(doc), flags)
            _move_to(hit)

        edit.textChanged.connect(_apply)
        edit.returnPressed.connect(_jump_forward)
        btn_next.clicked.connect(_jump_forward)
        QShortcut(QKeySequence("Ctrl+F"), dlg).activated.connect(
            lambda: (edit.setFocus(), edit.selectAll()))
        edit.setFocus()
        dlg.exec()

    def _help_html(self):
        import os

        from wyckoff.paths import HELP_FILE
        try:
            if os.path.exists(HELP_FILE):
                with open(HELP_FILE, encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return f"""<html><body style="background:{theme.C_BG};color:{theme.C_TEXT};
font-family:'Noto Sans CJK SC',serif;font-size:13px;padding:14px;line-height:1.7;">
<h3 style="margin:0 0 8px;">威科夫分析工具 · 快速上手</h3>
<p><b>① 分析股票</b><br/>
在工具栏输入代码/名称/拼音 (如 600104、sh600104、上汽) 回车即分析;
或在左侧自选股双击加载; 或点击工具栏"开始分析"。</p>
<p><b>② 自选股</b><br/>
左栏显示实时行情与阶段高亮 (底/升=红, 区=琥珀, 顶/跌=绿)。
"扫描自选股/全市场扫描/板块扫描"批量找信号, 双击结果加载分析。</p>
<p><b>③ K线图交互</b><br/>
滚轮缩放 · 左键拖拽平移 · 双击复位 · +/-缩放 · ←→平移 · Home复位。</p>
<p><b>④ 结论区</b><br/>
右侧分 信号汇总 + 分析结论, 左侧竖标签切换段落。</p>
<p><b>⑤ 报告</b><br/>
"文件 → 导出分析报告"保存 txt + K线PNG + 点数图PNG 到数据目录。</p>
<p><b>⑥ 工具</b><br/>
"国家队持仓透视"识别当前股票十大股东中的汇金/证金/社保;
"ETF 三因子份额监测"用交易所份额变化推断汇金宽基ETF信号
(概率性信号, 非官方数据)。</p>
<p style="color:{theme.C_MUTED};">威科夫技术分析 · 不构成投资建议</p>
</body></html>"""

    # ── 设置 ──
    def _chart_font(self):
        """图表字号 (pt), 来自设置 (设置→界面 图表字号), 未设置时默认 11。"""
        try:
            return int(self.settings.get(S.UI.CHART_FONT_SIZE, 11) or 11)
        except (TypeError, ValueError):
            return 11

    def _apply_chart_font(self):
        """按当前 chart_font_size 刷新四个图表控件的绘制字号并重渲染。"""
        size = self._chart_font()
        for w, data, code in (
                (self.kline_widget, self._chart_mgr.last_kline or {}, None),
                (self.pnf_widget, self._chart_mgr.last_pnf or {}, self._current_code),
                (self.ind_widget, self._chart_mgr.last_ind or {}, None),
                (self.mkt_widget, self._chart_mgr.last_mkt or {}, None)):
            try:
                w._font_size = size
                if data:
                    w.set_data(**data, **({"code": code} if code else {}))
            except Exception:
                pass

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.settings()
            save_settings(self.settings)
            self.cb_scale.setCurrentText(self.settings.get(S.General.DEFAULT_SCALE, "日线"))
            self.cb_period.setCurrentText(self.settings.get(S.General.DEFAULT_PERIOD, "近3年"))
            self._apply_theme()
            self._apply_fonts()
            self._apply_chart_defaults()
            self._apply_chart_font()
            self._sync_focus_btns()
            self._schedule_auto_sync()
            self._status("设置已保存", theme.C_DOWN)

    def _apply_fonts(self):
        """保存设置后即时刷新界面字体/字号、自选股栏字号与结论面板字号。"""
        theme.set_ui_font(
            family=str(self.settings.get(S.UI.FONT_FAMILY, "") or ""),
            size=int(self.settings.get(S.UI.FONT_SIZE, 12) or 12),
            watch=int(self.settings.get(S.UI.WATCH_FONT_SIZE, 12) or 12))
        self.setStyleSheet(theme.QSS)
        # 结论面板文字字号 (与 A-/A+ 按钮共用 text_font_size)
        try:
            new_ts = int(self.settings.get(S.UI.TEXT_FONT_SIZE, 11) or 11)
        except (TypeError, ValueError):
            new_ts = 11
        if new_ts != self._text_font_size:
            self._text_font_size = new_ts
            self._apply_text_zoom()

    def _theme_toggle_text(self):
        """主题切换菜单文案: 显示"要切换到"的目标主题 (当前深色 → 提示切浅色)。"""
        return ("切换到浅色主题" if theme.active_theme() == "dark"
                else "切换到深色护眼主题")

    def toggle_theme(self):
        new = "light" if theme.active_theme() == "dark" else "dark"
        self.settings[S.General.THEME] = new
        save_settings(self.settings)
        self._apply_theme()
        self._status(f"已切换为{'深色护眼' if new == 'dark' else '浅色'}主题", theme.C_MUTED)

    def _apply_theme(self):
        theme.set_theme(str(self.settings.get(S.General.THEME, "light") or "light"))
        self.setStyleSheet(theme.QSS)
        # 菜单项文案随当前主题翻转
        if getattr(self, "act_toggle_theme", None) is not None:
            self.act_toggle_theme.setText(self._theme_toggle_text())
        # 综合选股页内联样式随主题重刷 (构造期烧入的配色会残留)
        try:
            sw = getattr(self, "screener_widget", None)
            if sw is not None:
                sw.apply_theme()
        except Exception:
            pass
        # 校准中心 Tab 随主题重刷 (深色护眼/浅色)
        try:
            ac = getattr(self, "_ac_win", None)
            if ac is not None:
                ac.apply_theme()
        except Exception:
            pass
        # 产业链地图节点配色随主题重刷
        try:
            cw = getattr(self, "chain_widget", None)
            if cw is not None:
                cw.apply_theme()
        except Exception:
            pass
        # 模拟盘 Tab 随主题重刷 (构造期烧入的内联配色/资金曲线背景残留)
        try:
            pt = getattr(self, "paper_tab", None)
            if pt is not None:
                pt.apply_theme()
        except Exception:
            pass
        # 批量更新: 4图表重渲染 + 主题刷新合并为一次重绘
        self.setUpdatesEnabled(False)
        try:
            # 先刷新四个图表控件的背景/坐标轴配色
            for w in (self.kline_widget, self.pnf_widget,
                      self.ind_widget, self.mkt_widget):
                try:
                    w.apply_theme()
                except Exception:
                    pass
            try:
                self.ind_scroll.apply_theme()
            except Exception:
                pass
            if getattr(self, "mkt_scroll", None) is not None:
                try:
                    self.mkt_scroll.apply_theme()
                except Exception:
                    pass
            # 再以当前数据重渲染, 刷新数据项配色
            try:
                self.kline_widget.set_data(**self._chart_mgr.last_kline or {})
            except Exception:
                pass
            try:
                self.pnf_widget.set_data(**self._chart_mgr.last_pnf,
                                        **({"code": self._current_code}
                                           if self._current_code else {}))
            except Exception:
                pass
            try:
                self.ind_widget.set_data(**self._chart_mgr.last_ind or {})
            except Exception:
                pass
            try:
                self.mkt_widget.set_data(**self._chart_mgr.last_mkt or {})
            except Exception:
                pass
        finally:
            self.setUpdatesEnabled(True)
        if getattr(self, "_last_sections", None) is not None:
            self._render_sections(self._last_sections)
        if getattr(self, "_last_summary", None) is not None:
            self._render_summary(self._last_summary)
        if getattr(self, "_last_interp_lines", None):
            self.interp_text.setHtml(self._interp_html(self._last_interp_lines, self._text_font_size))
        else:
            self._set_interp_placeholder(self._text_font_size)

    def _show_about(self):
        QMessageBox.about(self, "关于 Wyckoff 客户端",
                          "Wyckoff 威科夫分析方法客户端 (PyQt6)\n\n"
                          "分析引擎: wyckoff 分析包 (含 VSA 信号 / 阶段判断 / "
                          "P&F 点数图 / 资金透视 / 回测 / AI 证伪等)\n"
                          "K线图 / P&F / 技术指标 / 资金透视: pyqtgraph\n\n"
                          "作者: 阮俊\n"
                          "版权 © 2026 阮俊 · 保留所有权利")

    def _status(self, text, color=None):
        self.status_label.setText(text)
        if color:
            self.status_label.setStyleSheet(f"color:{color};")

    def closeEvent(self, e):
        self._closing = True
        self._cancel_auto_refresh()
        self._cancel_accuracy_eval()
        self._cancel_auto_scan()
        self._cancel_auto_sync()
        try:
            if hasattr(self, "status_ticker"):
                self.status_ticker.clear()
        except Exception:
            pass
        # 记录面板折叠状态与停靠面板布局 (必须在 hide 之前, 否则 isVisible() 恒为 False)
        try:
            self.settings[S.UI.LEFT_PANEL_VISIBLE] = self.dock_watch.isVisible()
            self.settings[S.UI.RIGHT_PANEL_VISIBLE] = self.dock_right.isVisible()
            import base64
            self.settings[S.UI.DOCK_STATE] = base64.b64encode(
                self.saveState(version=1)).decode("ascii")
            try:
                save_settings(self.settings)
            except Exception as e:
                log_exc("closeEvent 保存设置失败", e)
        except Exception as e:
            log_exc("closeEvent 收集窗口状态失败", e)
        # 隐藏窗口: 让用户感觉"已关闭", 线程收尾在后台进行, 避免关窗卡顿。
        try:
            self.hide()
        except Exception:
            pass
        # 等待所有后台线程结束, 否则 QThread 在运行中被销毁 → SIGABRT
        # (尤其是键盘精灵选中股票后分析线程还在跑时直接关窗)。
        # 窗口已隐藏, 这里的短暂阻塞不再被用户感知。
        th = getattr(self, "_thread", None)
        if th is not None and th.isRunning():
            th.wait(5000)
        t = getattr(self, "_startup_scan_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        b = getattr(self, "_boot_timer", None)
        if b is not None:
            try:
                b.stop()
            except Exception:
                pass
        for t in list(getattr(self, "_analysis_threads", {}) or {}):
            if t is not None and t.isRunning():
                t.wait(5000)
        for t in list(getattr(self, "_rt_threads", {}) or {}):
            if t is not None and t.isRunning():
                t.wait(8000)
        st = getattr(self, "_scan_threads", None)
        for t in list(st.values() if isinstance(st, dict) else (st or [])):
            if t is not None and hasattr(t, "isRunning") and t.isRunning():
                t.wait(8000)
        st = getattr(self, "_auto_sync_th", None)
        if st is not None and st.isRunning():
            st.wait(8000)
        for t in list(getattr(self, "_label_ai_threads", {}) or {}):
            if t is not None and t.isRunning():
                t.wait(5000)
        super().closeEvent(e)


# ──────────────────────────────────────────── 小工具 ────────────────────────────────────────────

def _combo(items, current=None):
    from PyQt6.QtWidgets import QComboBox
    cb = QComboBox()
    cb.addItems(items)
    if current in items:
        cb.setCurrentText(current)
    cb.setMinimumWidth(88)
    return cb


def _button(text, primary=False):
    b = QPushButton(text)
    if primary:
        b.setObjectName("primaryBtn")
    return b


def _clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


def datetime_now():
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def _app_icon():
    """应用图标: 开发态取 ui/wyckoff.png; 打包态取 _MEIPASS 根 (见 wyckoff.spec datas)。"""
    import os
    import sys

    from PyQt6.QtGui import QIcon

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wyckoff.png")
    frozen = os.path.join(getattr(sys, "_MEIPASS", ""), "wyckoff.png")
    for p in (frozen if getattr(sys, "frozen", False) else "", here):
        if p and os.path.isfile(p):
            return QIcon(p)
    return QIcon()


def main():
    import sys

    from PyQt6.QtCore import qInstallMessageHandler
    from PyQt6.QtWidgets import QApplication

    def _qt_msg_handler(msg_type, context, message):
        # Deepin 会话全局设置 QT_SCALE_FACTOR_ROUNDING_POLICY=PassThrough,
        # Qt 创建 QApplication 时内部读取该变量并调用静态 setter, 此时实例
        # 已存在 → 触发 "must be called before creating ..." 警告。纯噪音,
        # 过滤之; 其余消息照常输出到 stderr。
        if "setHighDpiScaleFactorRoundingPolicy" in message:
            return
        sys.stderr.write(message + "\n")

    qInstallMessageHandler(_qt_msg_handler)
    app = QApplication(sys.argv)
    app.setApplicationName("WyckoffClient")
    icon = _app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)  # 全部窗口 + 任务栏图标统一继承
    # Windows 原生样式 (windowsvista/windows11) 不遵循 QSS 绘制滚动条, 深色护眼
    # 下会露出白色系统滚动条; 强制 Fusion 样式以完整响应 QSS 换肤。
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
