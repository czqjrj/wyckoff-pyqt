"""模拟盘面板 (自动筛选→自动下单→自动卖出→收益统计)。

复用 wyckoff.paper 引擎 + extra_windows 的表格/线程模式:
  - 手动/定时执行"一个周期" (run_cycle), 后台线程执行避免卡 UI。
  - 四个标签: 持仓 / 已平仓 / 候选 / 订单, 顶部账户概览 + 收益统计。
"""
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .extra_windows import _accent_header, _ghost_btn, _table


def _fill_paper(table, cols, heads, rows, color_cols=()):
    """表格批量填充: 自定义列头 + color_cols 内数值按涨红跌绿着色。"""
    from PyQt6.QtWidgets import QHeaderView
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
                    if c in _PCT_COLS:
                        txt = f"{val:+.2f}%"
                    else:
                        txt = f"{val:.2f}" if c in _F2 else f"{val:+.2f}"
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
                    color = theme_c(num)
                    if color:
                        it.setForeground(QColor(color))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    finally:
        table.setUpdatesEnabled(True)


_F2 = {"buy_px", "sell_px", "last", "price", "conf"}
_PCT_COLS = {"ret", "last_ret"}


def theme_c(num):
    from .extra_windows import theme
    if num < -1e-9:
        return theme.C_DOWN
    if num > 1e-9:
        return theme.C_UP
    return None


def _cond_kind_cn(kind):
    return {
        "buy_price": "价跌买入",
        "sell_price": "价升卖出",
        "take_profit": "止盈",
        "stop_loss": "止损",
        "trailing": "追踪止损",
    }.get(kind, kind)

_CN_POS = ("symbol", "name", "type", "conf", "qty", "buy_px", "last",
           "last_ret", "entry_bars")
_CN_POS_HEAD = ("代码", "名称", "事件", "置信", "数量", "成本", "现价",
                "浮盈亏", "已持K")
_CN_CLOSED = ("symbol", "name", "type", "reason", "buy_px", "sell_px",
              "ret", "bars", "close_ts")
_CN_CLOSED_HEAD = ("代码", "名称", "事件", "平仓原因", "买入价", "卖出价",
                   "收益", "持有K", "平仓时间")
_CN_CAND = ("code", "name", "type", "conf", "last")
_CN_CAND_HEAD = ("代码", "名称", "事件", "置信", "现价")
_CN_ORD = ("ts", "symbol", "name", "qty", "price", "type", "conf", "side", "date")
_CN_ORD_HEAD = ("时间", "代码", "名称", "数量", "价格", "事件", "置信",
                "方向", "日期")
_CN_COND = ("created_ts", "symbol", "name", "kind", "trigger", "cond_price",
            "pct", "qty", "status", "matched_price", "reason")
_CN_COND_HEAD = ("创建时间", "代码", "名称", "类型", "触发", "触发价", "百分比",
                 "数量", "状态", "成交价", "说明")


class _PaperCycleThread(QThread):
    """后台执行 run_cycle (连行情筛选+下单+卖出)。"""
    done = pyqtSignal(object)

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self._settings = settings

    def run(self):
        from wyckoff.paper import run_cycle
        try:
            st = run_cycle(settings=self._settings)
        except Exception as e:
            from wyckoff.logger import log_exc
            log_exc("模拟盘周期执行失败", e)
            st = {"error": str(e)}
        self.done.emit(st)


class PaperWindow(QDialog):
    """模拟盘: 账户概览 + 四页签 (持仓/已平仓/候选/订单) + 周期调度。"""

    def __init__(self, parent=None, settings=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self._settings = settings or {}
        self.setWindowTitle("模拟盘 · 自动威科夫策略")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1120, 640)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("模拟盘 · 自动威科夫策略 "
                                      "(筛选→买入→卖出→统计)"))

        # 账户概览
        self.summary = QLabel("加载中 ...")
        self.summary.setStyleSheet("font-weight:bold;")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        # 操作行
        hb = QHBoxLayout()
        self.btn_cycle = _ghost_btn("执行一个周期 (筛选+下单+卖出)")
        self.btn_cycle.clicked.connect(self._run_cycle)
        self.btn_refresh = _ghost_btn("刷新面板")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reset = _ghost_btn("重置账户")
        self.btn_reset.clicked.connect(self._reset_account)
        # 导出报告按钮
        self.btn_export = _ghost_btn("导出报告")
        # 自动执行模式设置
        hb_mode = QHBoxLayout()
        # 30分钟模式 (默认)
        self.auto_on_30 = QCheckBox("每 30 分钟自动执行周期")
        self.auto_on_30.setChecked(True)  # 默认开启
        self.auto_on_30.setToolTip("间隔30分钟执行一个周期")
        # 15分钟模式
        self.auto_on_15 = QCheckBox("每 15 分钟自动执行周期")
        self.auto_on_15.setChecked(False)  # 默认关闭
        self.auto_on_15.setToolTip("间隔15分钟执行一个周期，单次周期较少")
        # 互斥: 只能任选一个
        self.auto_on_15.toggled.connect(lambda checked: self.auto_on_30.setChecked(not checked) if checked else None)
        self.auto_on_30.toggled.connect(lambda checked: self.auto_on_15.setChecked(not checked) if checked else None)
        hb_mode.addWidget(self.auto_on_30)
        hb_mode.addWidget(self.auto_on_15)
        hb.addStretch(1)
        root.addLayout(hb)
        root.addLayout(hb_mode)
        # 定时器: 每30分钟(默认)或15分钟自动执行周期
        self.timer = QTimer(self)
        # 默认间隔: 30分钟 (auto_on_30 被勾选)
        if self.auto_on_30.isChecked():
            self.timer.setInterval(30 * 60 * 1000)
        else:
            self.timer.setInterval(15 * 60 * 1000)
        # 复选框切换时自动更新间隔
        self.auto_on_15.toggled.connect(lambda: self.timer.setInterval(15 * 60 * 1000 if not self.auto_on_30.isChecked() else 30 * 60 * 1000))
        self.auto_on_30.toggled.connect(lambda: self.timer.setInterval(30 * 60 * 1000 if self.auto_on_30.isChecked() else 15 * 60 * 1000))
        # 默认启动定时器 (30分钟模式)
        self.timer.start()
        self.timer.timeout.connect(self.refresh)

        # 策略参数配置 (放在模拟盘窗口内)
        root.addWidget(self._build_config_group())

        # 页签: 持仓/已平仓/候选/订单 + 条件单 + 资金曲线
        tabs = QTabWidget()
        self.t_pos = _table()
        self.t_closed = _table()
        self.t_cand = _table()
        self.t_ord = _table()
        self.t_cond = _table()
        self.tabs = tabs
        tabs.addTab(self.t_pos, "持仓")
        tabs.addTab(self.t_closed, "已平仓")
        tabs.addTab(self.t_cand, "候选")
        tabs.addTab(self.t_ord, "订单")
        tabs.addTab(self._build_cond_tab(), "条件单")
        tabs.addTab(self._build_equity_tab(), "资金曲线")
        root.addWidget(tabs, 1)

        self.refresh()

    def _build_equity_tab(self):
        """资金曲线页签: 总资产净值折线 (起点=初始资金, 含当前时点)。"""
        import pyqtgraph as pg

        from .extra_windows import theme
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
        """条件单页签: 显示自动生成的条件单 (无手动添加入口)。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        info = QLabel(
            "条件单由系统自动根据强多头事件生成\n"
            "无需手动添加, 系统将根据交易机会实时创建条件单"
        )
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet("font-size: 14px; color: #666; min-height: 80px;")
        lay.addWidget(info, 1)
        return page

    # ── 策略参数配置 ──
    def _build_config_group(self):
        from wyckoff.settings_keys import S
        group = QGroupBox("策略参数 (在模拟盘内配置, 执行周期时生效)")
        grid = QGridLayout(group)
        grid.setSpacing(6)

        self.sp_maxpos = QSpinBox()
        self.sp_maxpos.setRange(1, 5)
        self.sp_maxpos.setValue(int(self._settings.get(S.Paper.MAX_POS, 3)))
        self.sp_maxpos.setToolTip("同时持有的最大股票数 (1~5)。超过同持上限的候选会被跳过。")

        self.sp_conf = QSpinBox()
        self.sp_conf.setRange(50, 100)
        self.sp_conf.setValue(int(self._settings.get(S.Paper.MIN_CONF, 90)))
        self.sp_conf.setToolTip("只对置信度 >= 该值的强多头事件开仓。")

        self.sp_hold = QSpinBox()
        self.sp_hold.setRange(1, 120)
        self.sp_hold.setValue(int(self._settings.get(S.Paper.HOLD_BARS, 20)))
        self.sp_hold.setSuffix(" 根")
        self.sp_hold.setToolTip("持有 K 根后到期强制平仓 (默认 20 根 ≈ 1 个月)。")

        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setRange(0.01, 0.30)
        self.sp_stop.setSingleStep(0.005)
        self.sp_stop.setDecimals(3)
        self.sp_stop.setValue(float(self._settings.get(S.Paper.STOP_LOSS, 0.05)))
        self.sp_stop.setSuffix(" %")
        self.sp_stop.setToolTip("结构位止损幅度 (默认 5%)。")

        self.sp_tp = QDoubleSpinBox()
        self.sp_tp.setRange(0.05, 1.00)
        self.sp_tp.setSingleStep(0.05)
        self.sp_tp.setDecimals(3)
        self.sp_tp.setValue(float(self._settings.get(S.Paper.TAKE_PROFIT, 0.15)))
        self.sp_tp.setSuffix(" %")
        self.sp_tp.setToolTip("止盈幅度 (默认 15%)。")

        self.sp_cost = QDoubleSpinBox()
        self.sp_cost.setRange(0, 0.02)
        self.sp_cost.setSingleStep(0.0005)
        self.sp_cost.setDecimals(4)
        self.sp_cost.setValue(float(self._settings.get(S.Paper.COST, 0.004)))
        self.sp_cost.setSuffix(" %")
        self.sp_cost.setToolTip("单边成本 (佣金+印花税+滑点, 默认 0.4%)。")

        self.sp_cash = QDoubleSpinBox()
        self.sp_cash.setRange(1000, 1e9)
        self.sp_cash.setDecimals(0)
        self.sp_cash.setValue(float(self._settings.get(S.Paper.INIT_CASH, 1_000_000)))
        self.sp_cash.setSuffix(" 元")
        self.sp_cash.setToolTip("模拟盘初始资金。更改后需重置账户才应用到新账户。")

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

        btn = QPushButton("保存到设置")
        btn.setToolTip("把当前参数保存到界面设置并立即生效 (下次执行周期按新参数)。")
        btn.clicked.connect(self._save_config)
        grid.addWidget(btn, 0, len(fields) * 2)

        # 平仓原因按钮保持原有"重置账户"在操作行
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
        # 立即应用, 并刷新顶部参数摘要
        from wyckoff.paper import apply_paper_params
        apply_paper_params(self._settings)
        self.refresh()

    # ── 动作 ──
    def _run_cycle(self):
        self._collect_config()   # 立即采用面板上显示的参数, 无需先点"保存到设置"
        self.btn_cycle.setEnabled(False)
        self.btn_cycle.setText("周期执行中 ...")
        self._thread = _PaperCycleThread(self, settings=self._settings)
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

    # ── 渲染 ──
    def refresh(self):
        from wyckoff.paper import apply_paper_params, load_state, stats
        st = load_state()
        s = stats(st)
        cfg = apply_paper_params(self._settings)

        if s["win_rate"] is not None:
            win = f"{s['win_rate']*100:.1f}%"
        else:
            win = "-"
        dd = (f"{s['max_drawdown']*100:+.2f}%" if s["max_drawdown"] is not None
              else "-")
        self.summary.setText(
            f"总资产 {s['cash']:,.0f}  持仓 {s['n_positions']} 只 · "
            f"已平仓 {s['n_closed']} 笔 · 累计 {s['total_return']*100:+.2f}% · "
            f"胜率 {win} · 盈亏比 {s['pl_ratio'] or '-'} · "
            f"最大回撤 {dd} · 资金利用率 {self._usage(st):.0f}%\n"
            f"策略: 同持≤{cfg['max_pos']} · conf≥{cfg['min_conf']} · "
            f"持{cfg['hold_bars']}K · 止损-{cfg['stop_loss']*100:.0f}% · "
            f"止盈+{cfg['take_profit']*100:.0f}% · 成本{cfg['cost']*100:.1f}%")

        # 持仓
        rows = []
        for p in st["positions"]:
            rows.append({
                "symbol": p["symbol"], "name": p.get("name", ""),
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
                "type": c.get("type", ""), "reason": c.get("reason", ""),
                "buy_px": float(c["buy_px"]), "sell_px": float(c["sell_px"]),
                "ret": float(c["ret"] * 100), "bars": c.get("bars", 0),
                "close_ts": c.get("close_ts", ""),
            })
        _fill_paper(self.t_closed, _CN_CLOSED,
                    dict(zip(_CN_CLOSED, _CN_CLOSED_HEAD)), rows,
                    color_cols=("ret",))
        self.t_closed.setSortingEnabled(False)

        # 候选
        rows = []
        for c in st["candidates"]:
            rows.append({
                "code": c["code"], "name": c.get("name", ""),
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
                "name": o.get("name", ""), "qty": f"{o.get('qty', 0):,}",
                "price": float(o.get("price", 0) or 0),
                "type": o.get("type", ""), "conf": float(o.get("conf", 0)),
                "side": o.get("side", ""), "date": o.get("date", ""),
            })
        _fill_paper(self.t_ord, _CN_ORD, dict(zip(_CN_ORD, _CN_ORD_HEAD)),
                    rows)
        self.t_ord.setSortingEnabled(False)

        # 条件单 (自动生成中)
        crows = []
        for c in st.get("conditions", []):
            kind = c.get("kind", "")
            status = c.get("status", "")
            correct = c.get("correct")

            # 判断图标和颜色
            if status == "done":
                if correct is True:
                    icon = "✓ 正确"
                    icon_color = theme.C_UP  # 绿色
                elif correct is False:
                    icon = "✗ 错误"
                    icon_color = theme.C_DOWN  # 红色
                else:
                    icon = "─ 评估中"
                    icon_color = None
            elif status == "cancelled":
                icon = "✗ 已取消"
                icon_color = theme.C_MUTED
            else:  # active
                icon = "○ 进行中"
                icon_color = None

            crows.append({
                "created_ts": c.get("created_ts", ""),
                "symbol": c.get("symbol", ""), "name": c.get("name", ""),
                "kind": _cond_kind_cn(kind),
                "trigger": "上破≥" if c.get("trigger") == "above" else "回落≤" if c.get("trigger") == "below" else "",
                "cond_price": c.get("price") if c.get("price") is not None else "",
                "pct": c.get("pct") if c.get("pct") is not None else "",
                "qty": c.get("qty", 0), "status": status,
                "matched_price": c.get("matched_price") or "",
                "reason": c.get("reason", ""),
                "correct_icon": icon,
                "correct_color": icon_color,
            })

        # 计算列宽: 前8列固定, 后3列 (matched_price, reason, correct_icon) 根据内容自动
        n_cols = max(1, len(crows)) if crows else 0
        n_cols = min(n_cols, 13)  # 最大13列

        cond_cols = ("created_ts", "symbol", "name", "kind", "trigger",
                     "cond_price", "pct", "qty", "status", "matched_price",
                     "reason", "correct_icon")
        cond_heads = ("创建时间", "代码", "名称", "类型", "触发",
                      "触发价", "百分比", "数量", "状态", "成交价",
                      "说明", "正确")

        # 只有当有数据时才设置列数
        if crows:
            self.t_cond.setColumnCount(n_cols)
            self.t_cond.setHorizontalHeaderLabels([cond_heads[i % len(cond_heads)] for i in range(n_cols)])

        _fill_paper(self.t_cond, cond_cols[:n_cols],
                   dict(zip(cond_cols[:n_cols], cond_heads[:n_cols])), crows)
        self.t_cond.setSortingEnabled(False)

        # 为 correct_icon 列设置专用渲染 (显示 ✓ 或 ✗ 并着色)
        if n_cols > 11 and crows:
            # 获取 correct_icon 所在的列索引
            correct_col_idx = 11  # 第12列 (0-indexed)
            if correct_col_idx < self.t_cond.columnCount():
                # 重新设置该列的表头
                self.t_cond.setHorizontalHeaderItem(correct_col_idx, QTableWidgetItem("结果"))
                # 隐藏或调整该列的大小
                self.t_cond.horizontalHeader().setSectionSizeFromContent(correct_col_idx)

        self._render_equity(st)

    def _render_equity(self, st):
        """资金曲线: [初始资金] + equity_hist 各平仓时点 + 当前总资产。"""
        import pyqtgraph as pg

        from wyckoff.paper import INIT_CASH, equity

        from .extra_windows import theme
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
        # 初始资金参考线 (虚线)
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
        mv = sum(p["qty"] * p.get("last", p["buy_px"]) for p in st["positions"])
        eq = st["cash"] + mv
        return 100.0 * mv / eq if eq > 0 else 0.0
