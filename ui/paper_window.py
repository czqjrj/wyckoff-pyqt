"""模拟盘面板 (自动筛选→自动下单→自动卖出→收益统计)。

复用 wyckoff.paper 引擎 + wyckoff_strategies_manager 双策略 (策略4·纪律 +
价值吸筹) + extra_windows 的表格/线程模式:
  - 手动执行周期 (run_cycle) 与 定时自动执行周期 (30/15 分钟), 后台线程避免卡 UI。
  - 右侧策略概览按策略管理器两大策略并行统计 (纪律 / 价值吸筹)。
  - 四个数据页签: 持仓 / 已平仓 / 候选 / 订单, 顶部账户概览 + 收益统计。
"""

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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


def _cond_kind_cn(kind, trigger=None):
    """条件单类型中文标签 (与引擎触发语义对齐)。

    buy_price 引擎为 trigger="above" (现价上破触发价时买入) → 价升买入;
    sell_price 默认 trigger="below" (现价回落触发价时卖出) → 价跌卖出。
    """
    if kind == "buy_price":
        return "价升买入" if trigger != "below" else "价跌买入"
    if kind == "sell_price":
        return "价跌卖出" if trigger != "below" else "价升卖出"
    return {
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
_CN_CAND = ("code", "name", "strategy", "type", "conf", "last", "auto_px")
_CN_CAND_HEAD = ("代码", "名称", "策略", "事件", "置信", "现价", "买入触发价")
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
        from wyckoff._log import log_exc
        from wyckoff.paper import run_cycle
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
        from wyckoff._log import log_exc
        from wyckoff.paper import apply_paper_params, load_state, run_scan
        apply_paper_params(dict(self._settings or {}))
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
        from wyckoff._log import log_exc
        from wyckoff.datasource import fetch_realtime
        from wyckoff.paper import _LOCK, float_ret, load_state, save_state
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
        self.btn_close_sel = _ghost_btn("手动平仓所选")
        self.btn_close_sel.setToolTip(
            "在 [持仓] 页签选中一行, 按当前现价立即平仓该持仓")
        self.btn_close_sel.clicked.connect(self._close_selected)
        hb.addWidget(self.btn_close_sel)
        self.btn_watch = _ghost_btn("所选候选加入自选")
        self.btn_watch.setToolTip(
            "在 [候选] 页签选中一行, 把该股加入主界面自选股监控")
        self.btn_watch.clicked.connect(self._add_candidate_to_watch)
        hb.addWidget(self.btn_watch)
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
        self.pb_scan.setRange(0, 1)  # 待机静止 (避免 busy 自转动画)
        self.pb_scan.setValue(0)
        self.pb_scan.setFormat("待机")
        self.pb_scan.setToolTip("扫描进度 (待机=空闲, 扫描中为流动动画)")
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
        self._update_scan_info()

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
        self.equity_chart.getAxis("bottom").setLabel("日期")
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
            "条件单由系统自动生成: 扫描入场(buy_price) + 持仓保护(止盈/止损)\n"
            "买入成交后自动为持仓生成 止盈/止损; 也可手动添加价格/百分比条件单"
        )
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet(f"font-size:14px;color:{theme.C_MUTED};"
                           "min-height:40px;")
        lay.addWidget(info)

        form = QHBoxLayout()
        form.setSpacing(6)
        self.cd_symbol = QLineEdit()
        self.cd_symbol.setPlaceholderText("代码 (如 600036)")
        self.cd_symbol.setFixedWidth(110)
        self.cd_kind = QComboBox()
        for kind, label in (("buy_price", "价升买入"),
                            ("sell_price", "价跌卖出"),
                            ("take_profit", "止盈"),
                            ("stop_loss", "止损"),
                            ("trailing", "追踪止损")):
            self.cd_kind.addItem(label, kind)
        self.cd_cd_trigger = QComboBox()
        self.cd_cd_trigger.addItem("上破≥", "above")
        self.cd_cd_trigger.addItem("回落≤", "below")
        self.cd_price = QLineEdit()
        self.cd_price.setPlaceholderText("触发价/百分比 (如 12.5 或 0.1)")
        self.cd_price.setFixedWidth(140)
        self.cd_qty = QSpinBox()
        self.cd_qty.setRange(0, 10_000_000)
        self.cd_qty.setValue(0)
        self.cd_qty.setToolTip("数量 (0=全仓/按资金预算)")
        btn_add = _ghost_btn("添加条件单")
        btn_add.clicked.connect(self._add_manual_condition)
        btn_cancel = _ghost_btn("取消所选")
        btn_cancel.clicked.connect(self._cancel_selected_cond)
        btn_cancel.setToolTip("在下方选中一行, 取消其条件单 (仅 active)")
        for w in (self.cd_symbol, self.cd_kind, self.cd_cd_trigger,
                  self.cd_price, self.cd_qty, btn_add, btn_cancel):
            form.addWidget(w)
        form.addStretch(1)
        lay.addLayout(form)
        lay.addWidget(self.t_cond, 1)
        return page

    def _add_manual_condition(self):
        from PyQt6.QtWidgets import QMessageBox
        symbol = self.cd_symbol.text().strip()
        if not symbol:
            QMessageBox.warning(self, "添加条件单", "请输入代码")
            return
        kind = self.cd_kind.currentData()
        trigger = self.cd_cd_trigger.currentData()
        raw = self.cd_price.text().strip()
        try:
            from wyckoff.paper import place_condition
            if kind in ("buy_price", "sell_price"):
                price = float(raw)
                cond, msg = place_condition(
                    kind, symbol, price=price, trigger=trigger,
                    qty=self.cd_qty.value(), name="", reason="手动添加")
            else:
                pct = float(raw)
                cond, msg = place_condition(
                    kind, symbol, pct=pct, trigger=trigger,
                    qty=self.cd_qty.value(), name="", reason="手动添加")
        except ValueError:
            QMessageBox.warning(self, "添加条件单", "价格/百分比需为数字")
            return
        if cond is None:
            QMessageBox.warning(self, "添加条件单", msg)
            return
        self.summary.setText(self.summary.text() + f"\n{msg}: {symbol}")
        self.refresh()

    def _cancel_selected_cond(self):
        from PyQt6.QtWidgets import QMessageBox
        sel = self.t_cond.selectedItems()
        if not sel:
            QMessageBox.information(self, "取消条件单",
                                    "请先在条件单表选中一行")
            return
        sym_col = _CN_COND.index("symbol")
        item = self.t_cond.item(sel[0].row(), sym_col)
        symbol = item.text() if item else ""
        from wyckoff.paper import cancel_condition, load_state
        st = load_state()
        # 取消该代码下同日期的 active 条件 (匹配类型)
        cancelled = False
        for c in st.get("conditions", []):
            if c.get("status") == "active" and c.get("symbol") == symbol:
                if cancel_condition(st, c["cid"], save=False):
                    cancelled = True
        if cancelled:
            from wyckoff.paper import save_state
            save_state(st)
            self.refresh()
        else:
            QMessageBox.information(self, "取消条件单",
                                    f"{symbol} 无 active 条件单")

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
        self.sp_stop.setValue(float(self._settings.get(S.Paper.STOP_LOSS, 0.03)))

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

        self.ck_trailing = QCheckBox("追踪止损")
        self.ck_trailing.setChecked(
            bool(self._settings.get(S.Paper.TRAILING_STOP, False)))
        self.ck_trailing.setToolTip(
            "从持仓期内最高价回撤触发平仓, 而非固定百分比止损; "
            "可避免强势股被结构位 -3% 噪音洗出")

        self.sp_trail_atr = QDoubleSpinBox()
        self.sp_trail_atr.setRange(0, 10.0)
        self.sp_trail_atr.setSingleStep(1.0)
        self.sp_trail_atr.setDecimals(1)
        self.sp_trail_atr.setValue(
            float(self._settings.get(S.Paper.TRAIL_ATR_MULT, 0.0)))
        self.sp_trail_atr.setSuffix(" ATR")
        self.sp_trail_atr.setToolTip(
            "止损下沿再扣 ATR 缓冲 (剔除日内噪音), 0 表示不缓冲")

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
        grid.addWidget(self.ck_trailing, 1, 0, 1, 3)
        grid.addWidget(QLabel("ATR缓冲"), 1, 3)
        grid.addWidget(self.sp_trail_atr, 1, 4)
        for w in fields:
            w[1].setToolTip({
                self.sp_maxpos: "同时持有的最大股票数 (1~5)",
                self.sp_conf: "策略4·纪律只对置信度≥该值的强多头事件开仓",
                self.sp_hold: "持有 K 根后到期强制平仓",
                self.sp_stop: "止损幅度: 勾选用作追踪回撤触发, 否则为固定结构位止损",
                self.sp_tp: "止盈幅度",
                self.sp_cost: "单边成本 (佣金+印花税+滑点)",
                self.sp_cash: "模拟盘初始资金 (更改后需重置账户)",
            }[w[1]])
        hint = QLabel("回测最优参考: 止损 -3% / 止盈 +15% / 同持上限 3")
        hint.setStyleSheet(f"color: {theme.C_MUTED};")
        grid.addWidget(hint, 2, 0, 1, len(fields) * 2 + 1)

        btn = _ghost_btn("保存到设置")
        btn.clicked.connect(self._save_config)
        grid.addWidget(btn, 3, len(fields) * 2)
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
        self._settings[S.Paper.TRAILING_STOP] = self.ck_trailing.isChecked()
        self._settings[S.Paper.TRAIL_ATR_MULT] = self.sp_trail_atr.value()

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
        self.pb_scan.setFormat("%p%")

    def _on_scan_done(self, st):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("扫描")
        # 收尾: 无论成功/失败/空池, 进度条落定为静态完成态 (退出 busy 自转)
        self.pb_scan.setRange(0, 1)
        self.pb_scan.setValue(1)
        self.pb_scan.setFormat("完成")
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
        auto_str = self.auto_on.currentText()
        _t = ("扫描状态: 完成\n"
              f"今日次数: {self._scan_count}\n"
              f"上次扫描: {last_time[:16] if last_time else '--'}\n"
              f"下次扫描: {next_time[:16] if next_time else '--'}\n"
              f"定时执行: {auto_str}\n"
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
            from wyckoff.paper import equity, load_state, stats
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

    def _close_selected(self):
        from PyQt6.QtWidgets import QMessageBox
        sel = self.t_pos.selectedItems()
        if not sel:
            QMessageBox.information(self, "手动平仓",
                                    "请先在 [持仓] 页签选中要平仓的行")
            return
        symbol = sel[0].text()
        from wyckoff.paper import force_close_position, load_state
        st = load_state()
        pos = next((p for p in st["positions"] if p["symbol"] == symbol), None)
        if pos is None:
            QMessageBox.warning(self, "手动平仓", f"未找到持仓 {symbol}")
            return
        last = pos.get("last", pos["buy_px"])
        ret = (last / pos["buy_px"] - 1) * 100
        ok = QMessageBox.question(
            self, "手动平仓",
            f"按现价 {last:.3f} 平仓 {symbol} {pos.get('name','')} "
            f"({ret:+.2f}%)？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        force_close_position(load_state(), symbol, "手动平仓")
        self.refresh()

    def _add_candidate_to_watch(self):
        from PyQt6.QtWidgets import QMessageBox
        sel = self.t_cand.selectedItems()
        if not sel:
            QMessageBox.information(self, "加入自选",
                                    "请先在 [候选] 页签选中要加入自选的股票")
            return
        code = sel[0].text()
        from wyckoff.storage import load_watchlist, save_watchlist
        watch = list(load_watchlist())
        if code not in watch:
            watch.append(code)
            save_watchlist(watch)
        from wyckoff.paper import load_state
        st = load_state()
        name = next((c.get("name", "") for c in st["candidates"]
                     if c["code"] == code), "")
        self.summary.setText(
            self.summary.text() + f"\n已加入自选: {code} {name}")

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
        usage = self._usage(st)
        usage_warn = ""
        if s["n_positions"] and usage > cfg["max_capital_usage"] * 100:
            usage_warn = (f" (超上限 {cfg['max_capital_usage']*100:.0f}%)")
        self.summary.setText(
            f"总资产 {s['equity']:,.0f}  持仓 {s['n_positions']} 只 · "
            f"已平仓 {s['n_closed']} 笔 · 累计 {s['total_return']*100:+.2f}% · "
            f"胜率 {win} · 盈亏比 {s['pl_ratio'] or '-'} · "
            f"最大回撤 {dd} · 资金利用率 {usage:.0f}%{usage_warn}\n"
            f"策略: 同持≤{cfg['max_pos']} · conf≥{cfg['min_conf']} · "
            f"持{cfg['hold_bars']}K · 止损-{cfg['stop_loss']*100:.0f}% · "
            f"止盈+{cfg['take_profit']*100:.0f}% · 成本{cfg['cost']*100:.1f}%"
            + (" · 追踪止损" if cfg.get("trailing_stop") else ""))
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
                "auto_px": float(c.get("auto_cond_price", 0) or 0),
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
                    icon, _ = "✓ 正确", theme.C_UP
                elif correct is False:
                    icon, _ = "✗ 错误", theme.C_DOWN
                else:
                    icon, _ = "─ 评估中", None
            elif status == "cancelled":
                icon, _ = "✗ 已取消", theme.C_MUTED
            else:
                icon, _ = "○ 进行中", None
            crows.append({
                "created_ts": c.get("created_ts", ""),
                "symbol": c.get("symbol", ""), "name": c.get("name", ""),
                "kind": _cond_kind_cn(c.get("kind", ""), c.get("trigger")),
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
        dates = ["初始"]
        for h in hist:
            xs.append(len(xs))
            eqs.append(float(h.get("equity", INIT_CASH)))
            dates.append(str(h.get("ts", ""))[:10])
        xs.append(len(xs))
        eqs.append(float(equity(st, {})))
        dates.append("当前")

        ch = self.equity_chart
        ch.clear()
        ch.plot(xs, eqs,
                pen=pg.mkPen(theme.C_ACCENT, width=2),
                symbol="o", symbolSize=4, symbolBrush=pg.mkBrush(theme.C_ACCENT),
                symbolPen=pg.mkPen(theme.C_ACCENT))
        ch.plot(xs, [float(INIT_CASH)] * len(xs),
                pen=pg.mkPen(theme.C_MUTED, width=1,
                             style=Qt.PenStyle.DashLine))
        # 沪深300 基准对照 (归一化到初始资金, 供 alpha/beta 粗略对比; 拉取失败跳过)
        try:
            bench = self._fetch_benchmark(n_points=len(xs))
            if bench is not None:
                ch.plot(xs, bench,
                        pen=pg.mkPen(theme.C_BENCH, width=1, style=Qt.PenStyle.DotLine),
                        symbol=None)
        except Exception:
            pass
        if len(xs) >= 2:
            ced = theme.C_DOWN if eqs[-1] < eqs[0] else theme.C_UP
            cur = ch.plot([xs[-2], xs[-1]], [eqs[-2], eqs[-1]],
                          pen=pg.mkPen(ced, width=2))
            cur.setZValue(10)
        # 日期刻度 (每隔 N 根显示一个日期避免重叠)
        axis = ch.getAxis("bottom")
        max_labels = min(15, len(xs))
        step = max(1, len(xs) // max_labels)
        date_ticks = [(xs[i], dates[i]) for i in range(0, len(xs), step)]
        axis.setTicks([date_ticks])

    def _fetch_benchmark(self, n_points=100):
        """拉取沪深300 收盘序列, 归一化到初始资金作为基准对照。失败返回 None。"""
        try:
            from wyckoff.datasource import fetch_kline
            df = fetch_kline("sh000300", datalen=400, scale=240)
            if df is None or len(df) < 2:
                return None
            closes = [float(x) for x in df["close"].tail(n_points).tolist()]
            if not closes or closes[0] <= 0:
                return None
            base = float(closes[0])
            # 基准点序列对齐到资金曲线 x 轴起点 (初始资金, 历史点=0% 基准)
            from wyckoff.paper import INIT_CASH
            seg = [float(INIT_CASH)] + [INIT_CASH * (c / base) for c in closes]
            return seg[-(n_points + 1):] if n_points > 0 else seg
        except Exception:
            return None

    @staticmethod
    def _usage(st):
        mv = sum(p["qty"] * p.get("last", p["buy_px"])
                 for p in st["positions"])
        eq = st["cash"] + mv
        return 100.0 * mv / eq if eq > 0 else 0.0
