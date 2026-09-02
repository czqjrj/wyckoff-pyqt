"""模拟盘面板 (自动筛选→自动下单→自动卖出→收益统计)。

复用 wyckoff.paper 引擎 + wyckoff_strategies_manager 双策略 (策略4·纪律 +
价值吸筹) + extra_windows 的表格/线程模式:
  - 手动执行周期 (run_cycle) 与 定时自动执行周期 (30/15 分钟), 后台线程避免卡 UI。
  - 右侧策略概览按策略管理器两大策略并行统计 (纪律 / 价值吸筹)。
  - 四个数据页签: 持仓 / 已平仓 / 候选 / 订单, 顶部账户概览 + 收益统计。
"""
import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .extra_windows import (
    _accent_header,
    _flabel,
    _ghost_btn,
    _table,
    retheme_children,
    theme,
)


# ── 表格渲染 ───────────────────────────────────────────────
_F2 = {"buy_px", "sell_px", "last", "price", "conf"}
_PCT_COLS = {"ret", "last_ret"}


def _cell_color(num):
    """涨红跌绿着色 (A股习惯: 涨=红 C_UP, 跌=绿 C_DOWN)。"""
    if num is None:
        return None
    if num < -1e-9:
        return theme.C_DOWN
    if num > 1e-9:
        return theme.C_UP
    return None


def _fill_paper(table, cols, heads, rows, color_cols=()):
    """表格批量填充: 自定义列头 + color_cols 内数值按涨红跌绿着色。"""
    table.setUpdatesEnabled(False)
    try:
        table.clear()
        table.setColumnCount(len(cols))
        table.setRowCount(len(rows))
        table.setHorizontalHeaderLabels([heads[c] for c in cols])
        for ri, r in enumerate(rows):
            for ci, c in enumerate(cols):
                val = r.get(c, "")
                if isinstance(val, float):
                    txt = (f"{val:+.2f}%" if c in _PCT_COLS
                           else f"{val:.2f}" if c in _F2 else f"{val:+.2f}")
                else:
                    txt = str(val) if val not in (None, "") else "-"
                it = QTableWidgetItem(txt)
                try:
                    num = float(val)
                    it.setData(Qt.ItemDataRole.UserRole, num)
                except (TypeError, ValueError):
                    num = None
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(ri, ci, it)
                if c in color_cols and num is not None:
                    color = _cell_color(num)
                    if color:
                        it.setForeground(QColor(color))
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
    finally:
        table.setUpdatesEnabled(True)


def _cond_kind_cn(kind):
    return {
        "buy_price": "价跌买入",
        "sell_price": "价升卖出",
        "take_profit": "止盈",
        "stop_loss": "止损",
        "trailing": "追踪止损",
    }.get(kind, kind)


# ── 列定义 ────────────────────────────────────────────────
_CN_POS = ("symbol", "name", "strategy", "type", "conf", "qty", "buy_px",
           "last", "last_ret", "entry_bars")
_CN_POS_HEAD = ("代码", "名称", "策略", "事件", "置信", "数量", "成本", "现价",
                "浮盈亏", "已持K")
_CN_CLOSED = ("symbol", "name", "strategy", "type", "reason", "buy_px",
              "sell_px", "ret", "bars", "close_ts")
_CN_CLOSED_HEAD = ("代码", "名称", "策略", "事件", "平仓原因", "买入价",
                   "卖出价", "收益", "持有K", "平仓时间")
_CN_CAND = ("code", "name", "strategy", "type", "conf", "last")
_CN_CAND_HEAD = ("代码", "名称", "策略", "事件", "置信", "现价")
_CN_ORD = ("ts", "symbol", "name", "strategy", "qty", "price", "type", "conf",
           "side", "date")
_CN_ORD_HEAD = ("时间", "代码", "名称", "策略", "数量", "价格", "事件", "置信",
                "方向", "日期")
_CN_COND = ("created_ts", "symbol", "name", "kind", "trigger", "cond_price",
            "pct", "qty", "status", "matched_price", "reason", "correct_icon")
_CN_COND_HEAD = ("创建时间", "代码", "名称", "类型", "触发", "触发价", "百分比",
                 "数量", "状态", "成交价", "说明", "正确")

# 策略管理器信号来源 → 界面中文标签 (双策略并行)
_STRAT_CN = {
    "paper_discipline_bull": "策略4·纪律",
    "screener_value_accumulation": "价值吸筹",
}
_STRAT_ORDER = ("paper_discipline_bull", "screener_value_accumulation")


def _strat_cn(s):
    return _STRAT_CN.get(s or "", s or "-")


# ── 扫描模式 (多策略并行) ─────────────────────────────────
_SCAN_MODES = (
    ("混合扫描(纪律+价值吸筹)", ""),   # 综合: 走 run_scan 默认, 双策略并行
    ("纪律扫描(强多头+硬门禁)", "discipline"),
    ("价值吸筹扫描(底部整固)", "value_accumulation"),
)


# ── 后台线程 ──────────────────────────────────────────────
class _CycleThread(QThread):
    """后台执行 run_cycle (连行情筛选+下单+卖出)。"""
    done = pyqtSignal(object)

    def __init__(self, parent=None, settings=None, mode=None, candidates=None):
        super().__init__(parent)
        self._settings = settings
        self._mode = mode
        self._candidates = candidates

    def run(self):
        from wyckoff.paper import run_cycle
        from wyckoff._log import log_exc
        settings = dict(self._settings or {})
        if self._mode == "value_accumulation":
            # 纯价值吸筹: 降低强制 conf 门槛, 让价值吸筹逻辑优先
            from wyckoff.settings_keys import S
            settings[S.Paper.MIN_CONF] = min(
                int(settings.get(S.Paper.MIN_CONF, 90)), 50)
        try:
            st = run_cycle(settings=settings, candidates=self._candidates)
        except Exception as e:
            log_exc("模拟盘周期执行失败", e)
            st = {"error": str(e)}
        self.done.emit(st)


class _ScanThread(QThread):
    """后台执行 run_scan (多策略并行, 不阻塞 UI)。"""
    done = pyqtSignal(object)
    progress = pyqtSignal(int)

    def __init__(self, parent=None, mode="", n_codes=6000, settings=None):
        super().__init__(parent)
        self._mode = mode
        self._n_codes = n_codes
        self._settings = settings or {}

    def run(self):
        from wyckoff.paper import load_state, run_scan
        from wyckoff._log import log_exc
        _prev = [None]

        def _cb(done, total, code):
            # UI 刷新节流: 只在整百分比变化时发一次信号 (6000 只→~100 次)
            if total > 0:
                pct = int(round(100.0 * done / total))
                if pct != _prev[0]:
                    _prev[0] = pct
                    self.progress.emit(min(100, max(0, pct)))

        try:
            # 必须在完整状态副本上执行, 否则 run_scan 内部 save_state 会覆盖
            # 掉持仓/已平仓等持久化字段。
            st = load_state()
            result = run_scan(st, scan_type=self._mode or "discipline",
                              n_codes=self._n_codes, progress=_cb)
            st["last_scan_result"] = result
        except Exception as e:
            log_exc("模拟盘扫描失败", e)
            st = {"error": str(e)}
        self.done.emit(st)


class _QuoteThread(QThread):
    """批量拉实时行情, 只更新持仓/候选现价 (不跑周期/不下单)。"""
    done = pyqtSignal(int)

    def __init__(self, parent=None, codes=None):
        super().__init__(parent)
        self._codes = sorted(set(codes or []))

    def run(self):
        from wyckoff.datasource import fetch_realtime
        from wyckoff.paper import _LOCK, float_ret, load_state, save_state
        from wyckoff._log import log_exc
        try:
            rt = fetch_realtime(self._codes) or {}
        except Exception as e:
            log_exc("模拟盘现价刷新失败", e)
            rt = {}
        if not rt:
            self.done.emit(0)
            return
        touched = 0
        try:
            with _LOCK:
                st = load_state()
                for p in st.get("positions", []):
                    q = rt.get(p["symbol"]) or rt.get(p["symbol"][-6:])
                    price = q.get("price") if q else None
                    if not price or price <= 0:
                        continue
                    p["last"] = round(float(price), 3)
                    p["last_ret"] = round(float_ret(float(p["buy_px"]), float(price)), 4)
                    touched += 1
                for c in st.get("candidates", []):
                    q = rt.get(c["code"]) or rt.get(c["code"][-6:])
                    price = q.get("price") if q else None
                    if not price or price <= 0:
                        continue
                    if abs(round(float(price), 3) - float(c.get("last", 0) or 0)) > 1e-9:
                        c["last"] = round(float(price), 3)
                        touched += 1
                if touched:
                    save_state(st)
        except Exception as e:
            log_exc("模拟盘现价写入失败", e)
        self.done.emit(touched)


# ── 主窗口 ────────────────────────────────────────────────
class PaperWindow(QDialog):
    """模拟盘: 账户概览 + 策略概览(双策略) + 多页签 + 周期调度。"""

    def __init__(self, parent=None, settings=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self._settings = settings or {}
        self._thread = None
        self._scan_thread = None
        self._quote_thread = None
        self.setWindowTitle("模拟盘 · 自动威科夫策略")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1200, 720)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(_accent_header("模拟盘 · 双策略并行 "
                                      "(策略4·纪律 + 价值吸筹) → "
                                      "筛选→买入→卖出→统计"))

        # 顶部: 账户概览 + 操作行 + 策略概览 (水平分栏)
        top = QWidget()
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(12)

        # 左: 账户概览
        left_box = QGroupBox("账户概览")
        lv = QVBoxLayout(left_box)
        self.summary = QLabel("加载中 ...")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight:bold;")
        lv.addWidget(self.summary)
        self.scan_info = QLabel("扫描就绪")
        self.scan_info.setStyleSheet(f"font-size:12px;color:{theme.C_MUTED};")
        lv.addWidget(self.scan_info)
        top_lay.addWidget(left_box, 2)

        # 右: 策略概览 (双策略并行)
        right_box = QGroupBox("策略概览 (双策略)")
        rv = QGridLayout(right_box)
        self._strat_blocks = {}
        rv.addWidget(_flabel("策略"), 0, 0)
        rv.addWidget(_flabel("信号"), 0, 1)
        rv.addWidget(_flabel("持仓"), 0, 2)
        rv.addWidget(_flabel("胜率"), 0, 3)
        rv.addWidget(_flabel("累计"), 0, 4)
        for row, key in enumerate(_STRAT_ORDER, start=1):
            name = _STRAT_CN[key]
            lab_name = QLabel(name)
            lab_name.setStyleSheet("font-weight:bold;")
            rv.addWidget(lab_name, row, 0)
            blocks = []
            for col in (1, 2, 3, 4):
                b = QLabel("--")
                b.setAlignment(Qt.AlignmentFlag.AlignCenter)
                rv.addWidget(b, row, col)
                blocks.append(b)
            self._strat_blocks[key] = blocks
        rv.setColumnStretch(4, 1)
        top_lay.addWidget(right_box, 3)

        root.addWidget(top)

        # 操作行: 执行周期 / 自动执行 / 扫描
        hb = QHBoxLayout()
        hb.setSpacing(8)
        self.btn_cycle = _ghost_btn("执行一个周期 (筛选+下单+卖出)")
        self.btn_cycle.clicked.connect(self._run_cycle)
        hb.addWidget(self.btn_cycle)

        # 自动执行模式 (互斥)
        self.auto_on = QComboBox()
        self.auto_on.addItems(["自动执行: 关闭",
                               "每 15 分钟自动执行周期",
                               "每 30 分钟自动执行周期"])
        self.auto_on.setCurrentIndex(0)
        self.auto_on.setToolTip("定时自动执行一个完整周期 (筛选+下单+卖出)")
        hb.addWidget(self.auto_on)

        self.btn_refresh = _ghost_btn("刷新面板")
        self.btn_refresh.clicked.connect(self.refresh)
        hb.addWidget(self.btn_refresh)
        self.btn_reset = _ghost_btn("重置账户")
        self.btn_reset.clicked.connect(self._reset_account)
        hb.addWidget(self.btn_reset)
        self.btn_export = _ghost_btn("导出报告")
        self.btn_export.clicked.connect(self._export_report)
        hb.addWidget(self.btn_export)
        hb.addStretch(1)
        root.addLayout(hb)

        # 扫描行: 扫描模式 + 扫描范围 + 扫描按钮
        hb_scan = QHBoxLayout()
        hb_scan.setSpacing(8)
        hb_scan.addWidget(_flabel("策略+扫描"))
        self.cb_scan_mode = QComboBox()
        for label, _mode in _SCAN_MODES:
            self.cb_scan_mode.addItem(label)
        self.cb_scan_mode.setToolTip(
            "混合: 双策略并行 (纪律优先, 价值吸筹兜底)\n"
            "纪律: 仅策略4·纪律 (强多头+硬门禁)\n"
            "价值吸筹: 仅综合选股·价值吸筹 (底部整固+20根吸筹)")
        hb_scan.addWidget(self.cb_scan_mode)

        hb_scan.addWidget(_flabel("扫描数"))
        self.sp_scan_n = QSpinBox()
        self.sp_scan_n.setRange(10, 6000)
        self.sp_scan_n.setSingleStep(100)
        self.sp_scan_n.setValue(6000)
        self.sp_scan_n.setSuffix(" 只")
        self.sp_scan_n.setToolTip(
            "扫描数量 (上限 6000 = 全A 名单 ~5900 只)\n"
            "全市场扫描走本地全A 名单 (去 ST/退市/新股), "
            "名单不可用才降级东财成交额 Top 兜底")
        hb_scan.addWidget(self.sp_scan_n)

        self.btn_scan = _ghost_btn("扫描")
        self.btn_scan.clicked.connect(self._on_scan_now)
        hb_scan.addWidget(self.btn_scan)
        self.pb_scan = QProgressBar()
        self.pb_scan.setFixedWidth(170)
        self.pb_scan.setRange(0, 0)  # 待机状态 (busy 模式显示)
        self.pb_scan.setFormat("%p%")
        self.pb_scan.setToolTip("扫描进度")
        hb_scan.addWidget(self.pb_scan)
        self.lbl_scan_result = QLabel("")
        self.lbl_scan_result.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:12px;")
        hb_scan.addWidget(self.lbl_scan_result)
        hb_scan.addStretch(1)
        root.addLayout(hb_scan)

        # 策略参数配置
        root.addWidget(self._build_config_group())

        # 页签: 持仓/已平仓/候选/订单 + 条件单 + 资金曲线
        tabs = QTabWidget()
        self.tabs = tabs
        self.t_pos = _table()
        self.t_closed = _table()
        self.t_cand = _table()
        self.t_ord = _table()
        self.t_cond = _table()
        tabs.addTab(self.t_pos, "持仓")
        tabs.addTab(self.t_closed, "已平仓")
        tabs.addTab(self.t_cand, "候选")
        tabs.addTab(self.t_ord, "订单")
        tabs.addTab(self._build_cond_tab(), "条件单")
        tabs.addTab(self._build_equity_tab(), "资金曲线")
        root.addWidget(tabs, 1)

        # 定时器: 自动执行周期 (默认关闭)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_auto_timer)
        self.auto_on.currentIndexChanged.connect(self._apply_auto_interval)

        # 定时器: 行情热刷新 (现价随实时行情变动, 每 10s, 不跑周期)
        self.qt_timer = QTimer(self)
        self.qt_timer.timeout.connect(self._on_quote_timer)
        self.qt_timer.start(10 * 1000)

        self.refresh()

    # ── 自动执行定时器 ─────────────────────────────────────
    def _apply_auto_interval(self):
        """根据下拉框切换自动执行间隔 (0=关闭, 15/30 分钟)。"""
        self.timer.stop()
        idx = self.auto_on.currentIndex()
        if idx == 1:      # 15 分钟
            self.timer.setInterval(15 * 60 * 1000)
            self.timer.start()
        elif idx == 2:    # 30 分钟
            self.timer.setInterval(30 * 60 * 1000)
            self.timer.start()
        # idx == 0: 关闭, 不启动

    def _on_auto_timer(self):
        """定时回调: 自动执行一个完整周期 (筛选+下单+卖出)。"""
        if self._thread and self._thread.isRunning():
            return
        mode = self._current_mode()
        self._run_cycle_thread(mode)

    # ── 行情热刷新 ─────────────────────────────────────────
    def _quote_codes(self):
        from wyckoff.paper import load_state
        st = load_state()
        codes = {p["symbol"] for p in st.get("positions", [])}
        codes |= {o.get("symbol") for o in st.get("pending", [])}
        codes |= {c.get("code") for c in st.get("candidates", [])}
        return [c for c in codes if c]

    def _on_quote_timer(self):
        """每 10s 拉实时行情更新现价 (页面隐藏/无标的时跳过)。"""
        if not self.isVisible():
            return
        if self._quote_thread and self._quote_thread.isRunning():
            return
        codes = self._quote_codes()
        if not codes:
            return
        self._quote_thread = _QuoteThread(self, codes=codes)
        self._quote_thread.done.connect(self._on_quote_done)
        self._quote_thread.start()

    def _on_quote_done(self, touched):
        # 现价/浮盈亏或候选价有变动 → 重绘面板
        if touched > 0:
            self.refresh()

    # ── 资金曲线 ───────────────────────────────────────────
    def _build_equity_tab(self):
        import pyqtgraph as pg

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.equity_chart = pg.PlotWidget()
        self.equity_chart.setBackground(theme.C_BG)
        self.equity_chart.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        self.equity_chart.showGrid(x=False, y=True, alpha=0.3)
        self.equity_chart.getAxis("bottom").setPen(pg.mkPen(theme.C_MUTED))
        self.equity_chart.getAxis("left").setPen(pg.mkPen(theme.C_MUTED))
        self.equity_chart.getAxis("bottom").setLabel("平仓/周期序号")
        self.equity_chart.getAxis("left").setLabel("总资产")
        self.equity_chart.setMouseEnabled(x=True, y=False)
        lay.addWidget(self.equity_chart)
        return page

    def _build_cond_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        info = QLabel(
            "条件单由系统自动根据强多头/价值吸筹事件生成\n"
            "无需手动添加, 系统将根据交易机会实时创建条件单"
        )
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet(f"font-size:14px;color:{theme.C_MUTED};"
                           "min-height:80px;")
        lay.addWidget(info, 1)
        return page

    # ── 策略参数配置 ───────────────────────────────────────
    def _build_config_group(self):
        from wyckoff.settings_keys import S

        group = QGroupBox("策略参数 (执行周期时生效)")
        grid = QGridLayout(group)
        grid.setSpacing(6)

        self.sp_maxpos = QSpinBox()
        self.sp_maxpos.setRange(1, 5)
        self.sp_maxpos.setValue(int(self._settings.get(S.Paper.MAX_POS, 3)))

        self.sp_conf = QSpinBox()
        self.sp_conf.setRange(50, 100)
        self.sp_conf.setValue(int(self._settings.get(S.Paper.MIN_CONF, 90)))

        self.sp_hold = QSpinBox()
        self.sp_hold.setRange(1, 120)
        self.sp_hold.setValue(int(self._settings.get(S.Paper.HOLD_BARS, 20)))
        self.sp_hold.setSuffix(" 根")

        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setRange(0.01, 0.30)
        self.sp_stop.setSingleStep(0.005)
        self.sp_stop.setDecimals(3)
        self.sp_stop.setValue(float(self._settings.get(S.Paper.STOP_LOSS, 0.05)))

        self.sp_tp = QDoubleSpinBox()
        self.sp_tp.setRange(0.05, 1.00)
        self.sp_tp.setSingleStep(0.05)
        self.sp_tp.setDecimals(3)
        self.sp_tp.setValue(float(self._settings.get(S.Paper.TAKE_PROFIT, 0.15)))

        self.sp_cost = QDoubleSpinBox()
        self.sp_cost.setRange(0, 0.02)
        self.sp_cost.setSingleStep(0.0005)
        self.sp_cost.setDecimals(4)
        self.sp_cost.setValue(float(self._settings.get(S.Paper.COST, 0.004)))

        self.sp_cash = QDoubleSpinBox()
        self.sp_cash.setRange(1000, 1e9)
        self.sp_cash.setDecimals(0)
        self.sp_cash.setValue(
            float(self._settings.get(S.Paper.INIT_CASH, 1_000_000)))
        self.sp_cash.setSuffix(" 元")

        fields = (
            ("同持上限", self.sp_maxpos),
            ("置信度≥", self.sp_conf),
            ("持有期", self.sp_hold),
            ("止损", self.sp_stop),
            ("止盈", self.sp_tp),
            ("单边成本", self.sp_cost),
            ("初始资金", self.sp_cash),
        )
        for i, (label, w) in enumerate(fields):
            grid.addWidget(QLabel(label), 0, i * 2)
            grid.addWidget(w, 0, i * 2 + 1)
        for w in fields:
            w[1].setToolTip({
                self.sp_maxpos: "同时持有的最大股票数 (1~5)",
                self.sp_conf: "策略4·纪律只对置信度≥该值的强多头事件开仓",
                self.sp_hold: "持有 K 根后到期强制平仓",
                self.sp_stop: "结构位止损幅度",
                self.sp_tp: "止盈幅度",
                self.sp_cost: "单边成本 (佣金+印花税+滑点)",
                self.sp_cash: "模拟盘初始资金 (更改后需重置账户)",
            }[w[1]])

        btn = _ghost_btn("保存到设置")
        btn.clicked.connect(self._save_config)
        grid.addWidget(btn, 0, len(fields) * 2)
        return group

    def _collect_config(self):
        from wyckoff.settings_keys import S
        self._settings[S.Paper.MAX_POS] = self.sp_maxpos.value()
        self._settings[S.Paper.MIN_CONF] = self.sp_conf.value()
        self._settings[S.Paper.HOLD_BARS] = self.sp_hold.value()
        self._settings[S.Paper.STOP_LOSS] = self.sp_stop.value()
        self._settings[S.Paper.TAKE_PROFIT] = self.sp_tp.value()
        self._settings[S.Paper.COST] = self.sp_cost.value()
        self._settings[S.Paper.INIT_CASH] = self.sp_cash.value()

    def _save_config(self):
        self._collect_config()
        try:
            from wyckoff.storage import save_settings
            save_settings(self._settings)
        except Exception:
            pass
        from wyckoff.paper import apply_paper_params
        apply_paper_params(self._settings)
        self.refresh()

    def _current_mode(self):
        """当前扫描/周期执行模式 (多策略并行)。"""
        return _SCAN_MODES[self.cb_scan_mode.currentIndex()][1]

    # ── 主题 ──────────────────────────────────────────────
    def apply_theme(self):
        retheme_children(self)
        try:
            self._refresh_equity_theme()
        except Exception:
            pass
        try:
            self.refresh()
        except Exception:
            pass

    def _refresh_equity_theme(self):
        import pyqtgraph as pg

        ch = getattr(self, "equity_chart", None)
        if ch is None:
            return
        ch.setBackground(theme.C_BG)
        ch.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        ch.getAxis("bottom").setPen(pg.mkPen(theme.C_MUTED))
        ch.getAxis("left").setPen(pg.mkPen(theme.C_MUTED))

    # ── 扫描 ──────────────────────────────────────────────
    def _on_scan_now(self):
        """策略+扫描: 按所选模式后台扫描, 双策略并行。"""
        if self._scan_thread and self._scan_thread.isRunning():
            return
        mode = self._current_mode()
        n = self.sp_scan_n.value()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("扫描中 ...")
        self.lbl_scan_result.setText("")
        self.pb_scan.setRange(0, 0)  # 进度未知 → busy 模式
        self.pb_scan.setValue(0)
        self._scan_thread = _ScanThread(self, mode=mode, n_codes=n)
        self._scan_thread.done.connect(self._on_scan_done)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.start()

    def _on_scan_progress(self, pct):
        self.pb_scan.setRange(0, 100)
        self.pb_scan.setValue(pct)

    def _on_scan_done(self, st):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("扫描")
        # 收尾: 无论成功/失败/空池, 进度条落定为完成态
        self.pb_scan.setRange(0, 1)
        self.pb_scan.setValue(1)
        ok = False
        if isinstance(st, dict):
            if st.get("error"):
                self.lbl_scan_result.setText(
                    f"扫描失败: {st['error']}")
            else:
                self.lbl_scan_result.setText(
                    st.get("last_scan_result", "扫描完成"))
                ok = True
        self._update_scan_info(st)
        self.refresh()
        # 扫描成功 → 自动追加执行一个周期 (复用扫描候选, 免二次全市场选股)
        if ok and st.get("candidates"):
            mode = self._current_mode()
            self._run_cycle_thread(mode,
                                   candidates=st["candidates"])

    def _update_scan_info(self, st=None):
        from wyckoff.paper import load_state
        st = st or load_state()
        self._scan_count = st.get("scan_count", 0)
        last_time = st.get("last_scan_time", "")
        next_time = st.get("next_scan_time", "")
        result_str = st.get("last_scan_result", "")
        _t = ("扫描状态: 完成\n"
              f"今日次数: {self._scan_count}\n"
              f"上次扫描: {last_time[:16] if last_time else '--'}\n"
              f"下次扫描: {next_time[:16] if next_time else '--'}\n"
              f"结果: {result_str[:30] if result_str else ''}")
        self.scan_info.setText(_t)

    # ── 动作 ──────────────────────────────────────────────
    def _run_cycle(self):
        if self._thread and self._thread.isRunning():
            return
        self._collect_config()
        mode = self._current_mode()
        self._run_cycle_thread(mode)

    def _run_cycle_thread(self, mode, candidates=None):
        # 防重入: 已有周期在跑 (如 15/30 分钟定时器) 则跳过本次自动追加
        if self._thread and self._thread.isRunning():
            return
        self.btn_cycle.setEnabled(False)
        self.btn_cycle.setText("周期执行中 ...")
        self._thread = _CycleThread(self, settings=self._settings, mode=mode,
                                    candidates=candidates)
        self._thread.done.connect(self._on_cycle_done)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_thread_finished(self):
        self.btn_cycle.setEnabled(True)
        self.btn_cycle.setText("执行一个周期 (筛选+下单+卖出)")

    def _on_cycle_done(self, st):
        if isinstance(st, dict) and st.get("error"):
            self.summary.setText(f"周期执行失败: {st['error']}")
            return
        # 展示周期结果 (自动买入后的可见性)
        try:
            from wyckoff.paper import load_state
            cur = load_state()
            np_ = len(cur.get("positions", []))
            skip = cur.get("meta", {}).get("last_risk_skip")
            note = f"周期执行完成: 持仓 {np_} 只"
            if skip:
                note += f" · 风控拦截 {skip.get('code')}: {skip.get('reason')}"
            self.lbl_scan_result.setText(note)
        except Exception:
            pass
        self.refresh()

    def _reset_account(self):
        from PyQt6.QtWidgets import QMessageBox
        ok = QMessageBox.question(
            self, "重置模拟盘", "清空账户全部持仓/历史/统计并回到初始资金？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok == QMessageBox.StandardButton.Yes:
            from wyckoff.paper import _new_state, save_state
            save_state(_new_state())
            self.refresh()

    def _export_report(self):
        from PyQt6.QtWidgets import QFileDialog
        try:
            from wyckoff.paper import stats, load_state, equity, INIT_CASH
        except Exception:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出模拟盘报告", "paper_report.json",
            "JSON 报告 (*.json)")
        if not path:
            return
        import json
        from datetime import datetime
        st = load_state()
        s = stats(st)
        report = {
            "generated_at": datetime.now().isoformat(),
            "account": {"cash": st["cash"], "n_positions": len(st["positions"]),
                        "n_closed": len(st["closed"]),
                        "equity": equity(st, {})},
            "strategy_performance": self._strategy_summary(st),
            "stats": s,
            "positions": st["positions"],
            "closed": st["closed"][-200:],
            "candidates": st["candidates"],
            "orders": st["orders"][-200:],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        self.summary.setText(self.summary.text() + "\n报告已导出")

    # ── 渲染 ──────────────────────────────────────────────
    def _strategy_summary(self, st):
        """按策略管理器双策略并行统计。"""
        by_strat = {}
        for key in _STRAT_ORDER:
            by_strat[key] = {"signals": 0, "positions": 0, "wins": 0,
                             "trades": 0, "rets": []}
        for c in st["candidates"]:
            k = c.get("strategy", "")
            if k in by_strat:
                by_strat[k]["signals"] += 1
        for p in st["positions"]:
            k = p.get("strategy", "")
            if k in by_strat:
                by_strat[k]["positions"] += 1
        for c in st["closed"]:
            k = c.get("strategy", "")
            if k in by_strat:
                by_strat[k]["trades"] += 1
                if c.get("ret", 0) > 0:
                    by_strat[k]["wins"] += 1
                by_strat[k]["rets"].append(c.get("ret", 0))
        out = {}
        for key in _STRAT_ORDER:
            d = by_strat[key]
            n = d["trades"]
            cum = (sum(d["rets"]) * 100 if d["rets"]
                   else None)
            wr = (d["wins"] / n * 100 if n else None)
            out[key] = {
                "name": _STRAT_CN[key],
                "signals": d["signals"],
                "positions": d["positions"],
                "trades": n,
                "win_rate": wr,
                "cum": cum,
            }
        return out

    def _refresh_strategy_blocks(self, st):
        """刷新右侧双策略概览。"""
        summ = self._strategy_summary(st)
        for key in _STRAT_ORDER:
            blocks = self._strat_blocks.get(key)
            if not blocks:
                continue
            d = summ[key]
            blocks[0].setText(str(d["signals"]))
            blocks[1].setText(str(d["positions"]))
            blocks[2].setText(
                f"{d['win_rate']:.0f}%" if d["win_rate"] is not None else "--")
            blocks[3].setText(
                f"{d['cum']:+.1f}%" if d["cum"] is not None else "--")

    def refresh(self):
        from wyckoff.paper import apply_paper_params, load_state, stats
        st = load_state()
        s = stats(st)
        cfg = apply_paper_params(self._settings)

        win = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "-"
        dd = (f"{s['max_drawdown']*100:+.2f}%"
              if s["max_drawdown"] is not None else "-")
        self.summary.setText(
            f"总资产 {s['equity']:,.0f}  持仓 {s['n_positions']} 只 · "
            f"已平仓 {s['n_closed']} 笔 · 累计 {s['total_return']*100:+.2f}% · "
            f"胜率 {win} · 盈亏比 {s['pl_ratio'] or '-'} · "
            f"最大回撤 {dd} · 资金利用率 {self._usage(st):.0f}%\n"
            f"策略: 同持≤{cfg['max_pos']} · conf≥{cfg['min_conf']} · "
            f"持{cfg['hold_bars']}K · 止损-{cfg['stop_loss']*100:.0f}% · "
            f"止盈+{cfg['take_profit']*100:.0f}% · 成本{cfg['cost']*100:.1f}%")
        self._refresh_strategy_blocks(st)

        # 持仓
        rows = []
        for p in st["positions"]:
            rows.append({
                "symbol": p["symbol"], "name": p.get("name", ""),
                "strategy": _strat_cn(p.get("strategy", "")),
                "type": p.get("type", ""), "conf": float(p.get("conf", 50)),
                "qty": f"{p['qty']:,}", "buy_px": float(p["buy_px"]),
                "last": float(p.get("last", p["buy_px"])),
                "last_ret": float(p.get("last_ret", 0) * 100),
                "entry_bars": p.get("entry_bars", 0),
            })
        _fill_paper(self.t_pos, _CN_POS, dict(zip(_CN_POS, _CN_POS_HEAD)),
                    rows, color_cols=("last_ret",))
        self.t_pos.setSortingEnabled(False)

        # 已平仓
        rows = []
        for c in st["closed"][-200:]:
            rows.append({
                "symbol": c["symbol"], "name": c.get("name", ""),
                "strategy": _strat_cn(c.get("strategy", "")),
                "type": c.get("type", ""), "reason": c.get("reason", ""),
                "buy_px": float(c["buy_px"]), "sell_px": float(c["sell_px"]),
                "ret": float(c["ret"] * 100), "bars": c.get("bars", 0),
                "close_ts": c.get("close_ts", ""),
            })
        _fill_paper(self.t_closed, _CN_CLOSED,
                    dict(zip(_CN_CLOSED, _CN_CLOSED_HEAD)), rows,
                    color_cols=("ret",))
        self.t_closed.setSortingEnabled(False)

        # 候选 (按策略优先级排序, 双策略并行展示)
        cand_sorted = sorted(
            st["candidates"],
            key=lambda c: (_STRAT_ORDER.index(c.get("strategy", ""))
                           if c.get("strategy", "") in _STRAT_ORDER else 99,
                           -int(c.get("conf", 0) or 0)))
        rows = []
        for c in cand_sorted:
            rows.append({
                "code": c["code"], "name": c.get("name", ""),
                "strategy": _strat_cn(c.get("strategy", "")),
                "type": c.get("type", ""), "conf": float(c.get("conf", 0)),
                "last": float(c.get("last", 0) or 0),
            })
        _fill_paper(self.t_cand, _CN_CAND, dict(zip(_CN_CAND, _CN_CAND_HEAD)),
                    rows)
        self.t_cand.setSortingEnabled(False)

        # 订单
        rows = []
        for o in st["orders"][-200:]:
            rows.append({
                "ts": o.get("ts", ""), "symbol": o["symbol"],
                "name": o.get("name", ""),
                "strategy": _strat_cn(o.get("strategy", "")),
                "qty": f"{o.get('qty', 0):,}",
                "price": float(o.get("price", 0) or 0),
                "type": o.get("type", ""), "conf": float(o.get("conf", 0)),
                "side": o.get("side", ""), "date": o.get("date", ""),
            })
        _fill_paper(self.t_ord, _CN_ORD, dict(zip(_CN_ORD, _CN_ORD_HEAD)),
                    rows)
        self.t_ord.setSortingEnabled(False)

        # 条件单
        crows = []
        for c in st.get("conditions", []):
            status = c.get("status", "")
            correct = c.get("correct")
            if status == "done":
                if correct is True:
                    icon, ic = "✓ 正确", theme.C_UP
                elif correct is False:
                    icon, ic = "✗ 错误", theme.C_DOWN
                else:
                    icon, ic = "─ 评估中", None
            elif status == "cancelled":
                icon, ic = "✗ 已取消", theme.C_MUTED
            else:
                icon, ic = "○ 进行中", None
            crows.append({
                "created_ts": c.get("created_ts", ""),
                "symbol": c.get("symbol", ""), "name": c.get("name", ""),
                "kind": _cond_kind_cn(c.get("kind", "")),
                "trigger": ("上破≥" if c.get("trigger") == "above"
                            else "回落≤" if c.get("trigger") == "below" else ""),
                "cond_price": c.get("price") if c.get("price") is not None else "",
                "pct": c.get("pct") if c.get("pct") is not None else "",
                "qty": c.get("qty", 0), "status": status,
                "matched_price": c.get("matched_price") or "",
                "reason": c.get("reason", ""),
                "correct_icon": icon,
            })
        _fill_paper(self.t_cond, list(_CN_COND),
                    dict(zip(_CN_COND, _CN_COND_HEAD)), crows)
        self.t_cond.setSortingEnabled(False)

        self._render_equity(st)

    def _render_equity(self, st):
        import pyqtgraph as pg

        from wyckoff.paper import INIT_CASH, equity
        hist = st.get("equity_hist") or []
        xs = [0]
        eqs = [float(INIT_CASH)]
        for h in hist:
            xs.append(len(xs))
            eqs.append(float(h.get("equity", INIT_CASH)))
        xs.append(len(xs))
        eqs.append(float(equity(st, {})))

        ch = self.equity_chart
        ch.clear()
        ch.plot(xs, eqs,
                pen=pg.mkPen(theme.C_ACCENT, width=2),
                symbol="o", symbolSize=4, symbolBrush=pg.mkBrush(theme.C_ACCENT),
                symbolPen=pg.mkPen(theme.C_ACCENT))
        ch.plot(xs, [float(INIT_CASH)] * len(xs),
                pen=pg.mkPen(theme.C_MUTED, width=1,
                             style=Qt.PenStyle.DashLine))
        if len(xs) >= 2:
            ced = theme.C_DOWN if eqs[-1] < eqs[0] else theme.C_UP
            cur = ch.plot([xs[-2], xs[-1]], [eqs[-2], eqs[-1]],
                          pen=pg.mkPen(ced, width=2))
            cur.setZValue(10)

    @staticmethod
    def _usage(st):
        mv = sum(p["qty"] * p.get("last", p["buy_px"])
                 for p in st["positions"])
        eq = st["cash"] + mv
        return 100.0 * mv / eq if eq > 0 else 0.0
