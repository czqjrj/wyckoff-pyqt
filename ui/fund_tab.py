"""基本面分析 主窗口 Tab (PyQt6)。

汇总当前标的的基本面证据, 一次拉取渲染为纯文本报告:
  - 实时估值快照 (fetch_fundamental: 现价/PE/PB/市值/换手/EPS/营收/净利同比)
  - 主力资金流摘要 (fetch_main_flow: 近1/5/10/20日主力净额 + 最新单日分解)
  - 财报趋势 + A/B/C/D 分层 (fundamental_trend.build_report: as-of 财报, 无前视)
  - 所属板块 (fetch_sector)

网络抓取在后台 QThread 里执行, 完成前不阻塞 UI; 全部数据源 fail-soft。
"""
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wyckoff.utils import normalize_symbol

from . import theme

_TIER_NAME = {"A": "优", "B": "中性", "C": "弱", "D": "排雷", "": "无数据"}
_TIER_DESC = {
    "A": "净利同比≥+15% 且营收非负增长",
    "B": "有数据但不够 A/C (温和增长)",
    "C": "净利负增长 或 营收同比<-10%",
    "D": "报告期亏损 / 净利断崖≤-30% / 负增长+高估值 / 双杀",
    "": "无历史财报 (门禁 fail-open 放行, 权重 1.0)",
}
_ROW_OPTIONS = ("最近 12 份", "最近 24 份", "最近 36 份", "全部")

_SEP = "═" * 56


def _fmt_pct(v, signed=False):
    if v is None:
        return "-"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _fmt_yi(v):
    if v is None:
        return "-"
    return f"{v:+.2f}亿"


class _FundWorker(QThread):
    """后台抓取基本面+资金流+财报趋势。网络/解析异常全部 fail-soft。"""

    result = pyqtSignal(object)  # {"symbol":..., "text":..., "error":...}

    def __init__(self, symbol, code6, show_rows, fetch=True, parent=None):
        super().__init__(parent)
        self._symbol = symbol
        self._code6 = code6
        self._show_rows = show_rows
        self._fetch = fetch

    def run(self):
        parts = []
        parts.append(f"基本面分析 · {self._code6} ({self._symbol})")
        fund = flow = sector = None
        try:
            from wyckoff.fundamental import fetch_fundamental
            fund = fetch_fundamental(self._symbol)
        except Exception:
            fund = None
        try:
            from wyckoff.fundamental import fetch_main_flow
            flow = fetch_main_flow(self._symbol, 120)
        except Exception:
            flow = None
        try:
            from wyckoff.fundamental import fetch_sector
            sector = fetch_sector(self._symbol)
        except Exception:
            sector = None

        rep = None
        try:
            from wyckoff.fundamental_trend import build_report
            rep = build_report(self._symbol, show_rows=self._show_rows,
                               fetch=self._fetch, live=True)
        except Exception as e:
            rep = {"error": f"财报趋势加载失败: {e}"}

        # ── 第一段: 实时估值 ──
        parts.append(_SEP)
        parts.append("◆ 实时估值 (快照)")
        if fund:
            name = fund.get("name") or self._code6
            parts[0] = f"基本面分析 · {name} ({self._symbol})"
            price = fund.get("price")
            pe = fund.get("pe_ttm")
            if price is not None:
                parts.append(f"现价 {price:.2f}")
            if pe is not None:
                if pe > 0:
                    parts.append(f"PE(TTM) {pe:.2f}")
                else:
                    parts.append(f"PE(TTM) {pe:.2f} (亏损)")
            if fund.get("pb") is not None:
                parts.append(f"PB {fund['pb']:.2f}")
            if fund.get("mcap_yi") is not None:
                parts.append(f"总市值 {fund['mcap_yi']:.0f} 亿")
            if fund.get("float_mcap_yi") is not None:
                parts.append(f"流通市值 {fund['float_mcap_yi']:.0f} 亿")
            if fund.get("turnover"):
                parts.append(f"换手率 {fund['turnover']:.2f}%")
            if fund.get("eps") is not None:
                parts.append(f"EPS(TTM) {fund['eps']:.3f}")
            if fund.get("revenue") is not None and fund["revenue"] > 0:
                parts.append(f"营收(最新) {fund['revenue'] / 1e8:.0f} 亿")
            if fund.get("net_growth") is not None:
                g = fund["net_growth"] * 100
                parts.append(f"净利同比 {g:+.1f}%")
            if sector:
                parts.append(f"所属板块 {sector}")
        else:
            parts.append("实时估值获取失败 (网络不可达, 不影响下属报告)")
        parts.append("")

        # ── 第二段: 主力资金流 ──
        parts.append(_SEP)
        parts.append("◆ 主力资金流 (近120日)")
        if flow is not None and len(flow):
            sums = {n: float(flow.tail(n)["main"].sum()) / 1e8
                    for n in (1, 5, 10, 20)}
            parts.append(f"近1日主力 {_fmt_yi(sums[1])} · 近5日 {_fmt_yi(sums[5])} "
                         f"· 近10日 {_fmt_yi(sums[10])} · 近20日 {_fmt_yi(sums[20])}")
            last = flow.iloc[-1]
            parts.append(f"最新 {last['day'].date()}: 主力 {_fmt_yi(last['main'])} "
                         f"| 超大单 {_fmt_yi(last['super'])} | 大单 {_fmt_yi(last['large'])} "
                         f"| 中单 {_fmt_yi(last['mid'])} | 小单 {_fmt_yi(last['small'])}")
        else:
            parts.append("资金流数据缺失 (接口失败)")
        parts.append("")

        # ── 第三段: 财报趋势 + 分层 ──
        parts.append(_SEP)
        parts.append("◆ 财报按公告日 as-of (无前视)")
        if rep is not None and (rep.get("error") or not rep.get("rows")):
            parts.append(rep.get("error") or "无财报数据 (缓存缺失且回补失败)")
        else:
            tier = rep.get("tier") or ""
            tier_name = _TIER_NAME.get(tier, tier)
            tier_desc = _TIER_DESC.get(tier, "")
            weight = rep.get("weight")
            gate_txt = ("拦截D层" if rep.get("tier") == "D" else "放行") if rep.get("tier") \
                else "放行 (无数据 fail-open)"
            parts.append(f"当前分层: {rep.get('tier') or '-'} ({tier_name}) — {tier_desc}")
            if weight is not None:
                parts.append(f"分层仓位权重: {weight} · 基本面硬门禁: {gate_txt}")
            rows = rep.get("rows") or []
            parts.append(f"财报趋势 (最近 {len(rows)} 份, 共 {rep.get('n_reports', 0)} 份)")
            parts.append("")
            parts.append("报告期     | 公告日     | 营收同比   | 净利同比   | ROE     | 毛利率  | EPS")
            parts.append("---------- | ---------- | ---------- | ---------- | ------- | ------- | ------")
            for r in rows:
                eps = "-" if r.get("eps") is None else f"{r['eps']:.3f}"
                parts.append(f"{str(r.get('d') or ''):>10} | {str(r.get('n') or ''):>10} "
                             f"| {_fmt_pct(r.get('rev'), True):>10} | {_fmt_pct(r.get('np'), True):>10} "
                             f"| {_fmt_pct(r.get('roe')):>7} | {_fmt_pct(r.get('gm')):>7} | {eps}")
            verdict = rep.get("verdict") or {}
            parts.append("")
            for label, key in (("收入", "rev"), ("利润", "np"), ("质量", "quality"),
                               ("结论", "conclusion")):
                if verdict.get(key):
                    parts.append(f"- {label}: {verdict[key]}")

        err = ""
        if rep and rep.get("error"):
            if fund is None and flow is None:
                err = rep["error"]
                parts.append("")
                parts.append(f"[抓取失败] {err}")
            else:
                err = "财报趋势失败, 其余数据正常"

        self.result.emit({"symbol": self._symbol, "text": "\n".join(parts),
                          "error": err})


class FundamentalsTab(QWidget):
    """基本面分析 Tab: 展示当前标的的估值/资金流/财报分层报告。

    用法 (由 MainWindow 懒创建):
        tab = FundamentalsTab(main_window)
        main_window.tabs.addTab(tab, "基本面分析")
        每有新的分析完成:  tab.attach_stock(code)   (自动刷新)
    """

    def __init__(self, parent=None, on_load=None, show_rows=12, fetch=True):
        super().__init__(parent)
        self._stock = None
        self._shown_stock = None
        self._show_rows = show_rows
        self._fetch = fetch
        self._th = None
        self._pending_code = None
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 工具条
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.lb_stock = QLabel("未选择标的")
        self.lb_stock.setStyleSheet(
            f"color:{theme.C_ACCENT};font-weight:bold;font-size:{theme.font_pt('body')};")
        bar.addWidget(self.lb_stock)
        bar.addStretch(1)

        self.ed_code = QLineEdit()
        self.ed_code.setPlaceholderText("输代码回车: 600104 / sh600104")
        self.ed_code.setFixedWidth(170)
        self.ed_code.returnPressed.connect(self._on_manual_code)
        bar.addWidget(self.ed_code)

        self.btn_load = QPushButton("载入当前股票")
        self.btn_load.setToolTip("跟随主窗口自选股/分析中的标的")
        self.btn_load.clicked.connect(self._on_load_current)
        bar.addWidget(self.btn_load)

        self.cb_rows = QComboBox()
        self.cb_rows.addItems(_ROW_OPTIONS)
        self.cb_rows.setCurrentIndex({12: 0, 24: 1, 36: 2, 0: 3}.get(self._show_rows, 0))
        self.cb_rows.currentIndexChanged.connect(self._on_rows_changed)
        bar.addWidget(self.cb_rows)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        bar.addWidget(self.btn_refresh)
        root.addLayout(bar)

        # 报告视图
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setAcceptRichText(False)
        self.view.setFont(self._mono_font())
        self.view.setStyleSheet(
            f"QTextEdit{{background:{theme.C_PANEL};color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER};"
            f"selection-background-color:{theme.C_ACCENT};}}")
        root.addWidget(self.view, 1)

        self.lb_status = QLabel("")
        self.lb_status.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};")
        root.addWidget(self.lb_status)

        # 代码输入在父窗口也能触发 (分析完自动跟随后台)
        self.destroyed.connect(self._cleanup)

        self.view.setPlainText(
            "尚未选择标的。\n请先在左侧自选股/主窗口分析一只股票, 再点击「载入当前股票」"
            "或直接输入代码。\n数据源: 腾讯/东财实时估值 · 东财主力资金流 · "
            "财报按公告日 as-of (A/B/C/D 分层, 无前视)。")

    # ── 字体 ──
    def _mono_font(self):
        try:
            fam = theme.mono_font_family()
            pt = int(theme.font_pt("body")[:-2])
            return QFont(fam, pt)
        except Exception:
            return QFont("monospace", 10)

    # ── 对外 API ──
    def attach_stock(self, code):
        """记录当前主窗口分析的标的; 若 Tab 可见则立即刷新。"""
        if not code:
            return
        code = str(code)
        self._stock = code
        self._pending_code = code
        try:
            sym = normalize_symbol(code)
        except Exception:
            sym = code
        self.lb_stock.setText(f"{sym} · 基本面分析")
        if self.isVisible():
            self.refresh()

    def refresh(self):
        """以后台线程重新抓取并渲染当前标的。"""
        code = self._stock or self._pending_code
        if not code:
            self.view.setPlainText("尚未选择标的。\n请先在左侧自选股/主窗口分析一只股票, "
                                   "再点击「载入当前股票」或直接输入代码。")
            return
        if self._busy:
            return
        try:
            symbol = normalize_symbol(code)
        except ValueError:
            symbol = code
        self._busy = True
        self.btn_refresh.setEnabled(False)
        self.lb_status.setText(f"正在抓取 {symbol} 基本面/资金流/财报 ...")
        self._th = _FundWorker(symbol, symbol, self._show_rows, fetch=self._fetch,
                               parent=self)
        self._th.result.connect(self._on_result)
        self._th.finished.connect(self._on_finished)
        self._th.start()

    def apply_theme(self):
        """主题切换后重刷内联配色。"""
        self.lb_status.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};")
        self.lb_stock.setStyleSheet(
            f"color:{theme.C_ACCENT};font-weight:bold;font-size:{theme.font_pt('body')};")
        self.view.setStyleSheet(
            f"QTextEdit{{background:{theme.C_PANEL};color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER};"
            f"selection-background-color:{theme.C_ACCENT};}}")

    # ── 事件 ──
    def showEvent(self, ev):
        super().showEvent(ev)
        if self._pending_code and self._pending_code != self._shown_stock:
            self._stock = self._pending_code
            self.refresh()

    def _cleanup(self, *_):
        th = self._th
        if th is not None:
            if th.isRunning():
                th.requestInterruption()
                th.wait(1500)
            th.deleteLater()
            self._th = None

    # ── 槽 ──
    def _on_result(self, payload):
        try:
            exp = normalize_symbol(self._stock or self._pending_code or "")
        except Exception:
            exp = None
        if exp and payload.get("symbol") != exp:
            # 过期结果: 标的已切换, 丢弃
            return
        self._shown_stock = payload["symbol"]
        self.view.setPlainText(payload["text"])
        if payload.get("error"):
            self.lb_status.setText(payload["error"])

    def _on_finished(self):
        self._busy = False
        self.btn_refresh.setEnabled(True)
        if self._shown_stock:
            self.lb_status.setText("")
        elif not self.lb_status.text():
            self.lb_status.setText("抓取完成 (部分数据源失败, 见报告)")

    def _on_refresh_clicked(self):
        if not self._stock and not self._pending_code:
            self._on_load_current()
            return
        self.refresh()

    def _on_load_current(self):
        code = getattr(self.parent(), "_current_code", None) or \
            getattr(self.parent(), "_current_symbol", None)
        if not code:
            self.lb_status.setText("主窗口尚未有分析标的, 请先分析一只股票")
            return
        self.attach_stock(code)

    def _on_manual_code(self):
        code = self.ed_code.text().strip()
        if not code:
            return
        self.ed_code.clear()
        self.attach_stock(code)

    def _on_rows_changed(self, idx):
        self._show_rows = {0: 12, 1: 24, 2: 36, 3: 0}.get(idx, 12)
        if self._stock:
            self.refresh()
