"""模拟盘面板 (自动筛选→自动下单→自动卖出→收益统计)。

复用 wyckoff.paper 引擎 + extra_windows 的表格/线程模式:
  - 手动/定时执行"一个周期" (run_cycle), 后台线程执行避免卡 UI。
  - 四个标签: 持仓 / 已平仓 / 候选 / 订单, 顶部账户概览 + 收益统计。
"""
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout,
    QCheckBox, QTableWidgetItem, QWidget,
)

from .extra_windows import _accent_header, _table, _ghost_btn


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


class _PaperCycleThread(QThread):
    """后台执行 run_cycle (连行情筛选+下单+卖出)。"""
    done = pyqtSignal(object)

    def __init__(self, parent=None, min_conf=90):
        super().__init__(parent)
        self._min_conf = min_conf

    def run(self):
        from wyckoff.paper import run_cycle
        try:
            st = run_cycle(min_conf=self._min_conf)
        except Exception as e:
            from wyckoff.logger import log_exc
            log_exc("模拟盘周期执行失败", e)
            st = {"error": str(e)}
        self.done.emit(st)


class PaperWindow(QDialog):
    """模拟盘: 账户概览 + 四页签 (持仓/已平仓/候选/订单) + 周期调度。"""

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
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
        self.auto_on = QCheckBox("每 30 分钟自动执行周期")
        self.timer = QTimer(self)
        self.timer.setInterval(30 * 60 * 1000)
        self.auto_on.toggled.connect(lambda on: self.timer.start() if on else self.timer.stop())
        self.timer.timeout.connect(self.refresh)
        hb.addWidget(self.btn_cycle)
        hb.addWidget(self.btn_refresh)
        hb.addWidget(self.btn_reset)
        hb.addSpacing(20)
        hb.addWidget(self.auto_on)
        hb.addStretch(1)
        root.addLayout(hb)

        # 五个页签: 持仓/已平仓/候选/订单 + 资金曲线
        tabs = QTabWidget()
        self.t_pos = _table()
        self.t_closed = _table()
        self.t_cand = _table()
        self.t_ord = _table()
        self.tabs = tabs
        tabs.addTab(self.t_pos, "持仓")
        tabs.addTab(self.t_closed, "已平仓")
        tabs.addTab(self.t_cand, "候选")
        tabs.addTab(self.t_ord, "订单")
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

    # ── 动作 ──
    def _run_cycle(self):
        self.btn_cycle.setEnabled(False)
        self.btn_cycle.setText("周期执行中 ...")
        self._thread = _PaperCycleThread(self, min_conf=90)
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
            from wyckoff.paper import save_state, _new_state
            save_state(_new_state())
            self.refresh()

    # ── 渲染 ──
    def refresh(self):
        from wyckoff.paper import load_state, stats
        st = load_state()
        s = stats(st)

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
            f"最大回撤 {dd} · 资金利用率 {self._usage(st):.0f}%")

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

        self._render_equity(st)

    def _render_equity(self, st):
        """资金曲线: [初始资金] + equity_hist 各平仓时点 + 当前总资产。"""
        import pyqtgraph as pg
        from .extra_windows import theme
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