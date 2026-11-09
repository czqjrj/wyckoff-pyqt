"""A股市场状态独立 GUI (PyQt6)。

不依赖原 MainWindow, 单独运行:
    python scripts/ashare_outlook_gui.py

功能:
- 选择主指数, 刷新状态报告
- 渐进加载: 先出指数核心, 后台补板块/ETF/流动性
- 报告 / 图表 / 板块 / 个股 / 自选批量 / 个股日志 / 预测日志 七个页签
- 记录今日判断、回填到期结果、查看命中率/校准
- 导出 Markdown 报告
"""

from __future__ import annotations

import os
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from wyckoff.ashare_journal import (
    JOURNAL_FILE,
    STOCK_JOURNAL_FILE,
    _parse_trade_plan,
    backfill,
    backfill_stocks,
    compute_stats,
    compute_stock_stats,
    load_journal,
    record_outlook,
    record_stock,
    render_backfill_summary,
    render_stats,
    render_stock_stats,
)
from wyckoff.ashare_outlook import (
    OUTLOOK_INDICES,
    _plain_conclusion,
    _quality_issues,
    build_outlook,
    detect_stock_industry,
    fetch_sector_constituents_cached,
    render_report,
)
from wyckoff.ashare_watchlist import (
    add_watchlist,
    clear_watchlist,
    list_watchlist,
    parse_code_list,
    remove_watchlist,
)

from . import theme

# ─────────────────────────────────────────────────────────────
# 后台任务
# ─────────────────────────────────────────────────────────────


class _OutlookWorker(QThread):
    """构建 A 股状态报告。

    渐进模式 (progressive=True 且勾选了板块/ETF/流动性):
    先构建“指数核心”并 partial 发出，再补板块/ETF/流动性后 done 发出。
    """

    done = pyqtSignal(object)
    partial = pyqtSignal(object)
    stage = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, primary, options, parent=None):
        super().__init__(parent)
        self.primary = primary
        self.options = options

    def run(self):
        try:
            opts = dict(self.options or {})
            extras = {
                "include_sectors": bool(opts.pop("include_sectors", False)),
                "include_etf": bool(opts.pop("include_etf", False)),
                "include_liquidity": bool(opts.pop("include_liquidity", False)),
                "include_valuation": bool(opts.pop("include_valuation", False)),
            }
            progressive = bool(opts.pop("progressive", True)) and any(extras.values())
            core = dict(
                opts,
                include_sectors=False,
                include_etf=False,
                include_liquidity=False,
                include_valuation=False,
            )
            result = build_outlook(primary=self.primary, **core)
            if not progressive:
                self.done.emit(result)
                return
            self.partial.emit(result)
            if result.get("error"):
                self.done.emit(result)
                return
            self.stage.emit("核心已出，正在加载板块/ETF/流动性...")
            from wyckoff.ashare_outlook import load_extras

            self.done.emit(load_extras(result, **extras))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _BackfillWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.done.emit(backfill())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _StatsWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, horizon=20, parent=None):
        super().__init__(parent)
        self.horizon = horizon

    def run(self):
        try:
            self.done.emit(compute_stats(horizon=self.horizon))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _StockBackfillWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.done.emit(backfill_stocks())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _StockStatsWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, horizon=20, parent=None):
        super().__init__(parent)
        self.horizon = horizon

    def run(self):
        try:
            self.done.emit(compute_stock_stats(horizon=self.horizon))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _BatchWorker(QThread):
    row = pyqtSignal(object)
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        codes: list[str],
        market_result: dict | None,
        record: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.codes = list(codes or [])
        self.market_result = market_result
        self.record = bool(record)

    def run(self):
        try:
            from wyckoff.analysis import run_analysis

            results = []
            total = len(self.codes)
            for i, code in enumerate(self.codes, 1):
                self.progress.emit(i, total)
                try:
                    cap = {}

                    def _capture(
                        df, symbol, _c, _scale, _datalen, name, _phase_label, _conf_q, precomputed
                    ):
                        cap.update(precomputed)
                        cap["symbol"] = symbol
                        cap["name"] = name
                        cap["as_of"] = str(df["day"].iloc[-1])[:10]
                        cap["close"] = float(df["close"].iloc[-1])

                    text, _fig, _pf, _ind, summary, sections, market, _segs = run_analysis(
                        code,
                        datalen=500,
                        scale=240,
                        confirm_enabled=False,
                        kline_engine="pyqtgraph",
                        pnf_engine="pyqtgraph",
                        ind_engine="pyqtgraph",
                        mkt_engine="pyqtgraph",
                        vsa_backtest=False,
                        precomputed_cb=_capture,
                    )
                    data = {
                        "code": code,
                        "symbol": cap.get("symbol") or code,
                        "name": cap.get("name"),
                        "as_of": cap.get("as_of"),
                        "close": cap.get("close"),
                        "text": text,
                        "summary": summary or [],
                        "sections": sections or [],
                        "market": market,
                        "precomputed": cap,
                        "confirm": False,
                    }
                    trade = _parse_trade_plan((cap or {}).get("trade_plan"))
                    fusion = (cap or {}).get("fusion") or {}
                    row = {
                        "code": code,
                        "name": cap.get("name") or "",
                        "phase": (cap or {}).get("phase") or "--",
                        "score": fusion.get("score"),
                        "direction": trade.get("direction") or "",
                        "stop": trade.get("stop"),
                        "target1": trade.get("target1"),
                        "record_status": "未记录",
                    }
                    if self.record:
                        try:
                            res = record_stock(data, self.market_result)
                            row["record_status"] = (
                                "已记录" if res.get("ok") else f"失败: {res.get('reason')}"
                            )
                        except Exception as e:  # noqa: BLE001
                            row["record_status"] = f"失败: {type(e).__name__}: {e}"
                    results.append({"row": row, "data": data})
                    self.row.emit(row)
                except Exception as e:  # noqa: BLE001
                    row = {
                        "code": code,
                        "name": "",
                        "phase": "失败",
                        "score": None,
                        "direction": "",
                        "stop": None,
                        "target1": None,
                        "record_status": f"{type(e).__name__}: {e}"[:80],
                    }
                    results.append({"row": row, "data": {}})
                    self.row.emit(row)
            self.done.emit({"ok": True, "n": len(results), "results": results})
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class _ConstituentWorker(QThread):
    done = pyqtSignal(object)

    def __init__(self, name: str, label: str | None = None, parent=None):
        super().__init__(parent)
        self.name = name
        self.label = label

    def run(self):
        try:
            rows = fetch_sector_constituents_cached(self.name, self.label)
            self.done.emit({"name": self.name, "rows": rows})
        except Exception as e:  # noqa: BLE001
            self.done.emit({"name": self.name, "rows": [], "error": f"{type(e).__name__}: {e}"})


class _StockWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        code: str,
        confirm: bool = False,
        sector_names: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.code = code
        self.confirm = confirm
        self.sector_names = sector_names or []

    def run(self):
        try:
            from wyckoff.analysis import run_analysis

            industry: dict = {}
            try:
                industry = detect_stock_industry(self.code, self.sector_names)
            except Exception:  # noqa: BLE001
                industry = {}

            cap = {}

            def _capture(
                df, symbol, _c, _scale, _datalen, name, _phase_label, _conf_q, precomputed
            ):
                cap.update(precomputed)
                cap["symbol"] = symbol
                cap["name"] = name
                cap["as_of"] = str(df["day"].iloc[-1])[:10]
                cap["close"] = float(df["close"].iloc[-1])

            text, _fig, _pf, _ind, summary, sections, market, _segs = run_analysis(
                self.code,
                datalen=500,
                scale=240,
                confirm_enabled=self.confirm,
                kline_engine="pyqtgraph",
                pnf_engine="pyqtgraph",
                ind_engine="pyqtgraph",
                mkt_engine="pyqtgraph",
                vsa_backtest=False,
                precomputed_cb=_capture,
            )
            self.done.emit(
                {
                    "code": self.code,
                    "symbol": cap.get("symbol") or self.code,
                    "name": cap.get("name"),
                    "as_of": cap.get("as_of"),
                    "close": cap.get("close"),
                    "text": text,
                    "summary": summary or [],
                    "sections": sections or [],
                    "market": market,
                    "precomputed": cap,
                    "confirm": self.confirm,
                    "industry": industry,
                }
            )
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────
# 行业热力图
# ─────────────────────────────────────────────────────────────


def _fmt_pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "--"


def _heat_color(pct) -> QColor:
    try:
        v = max(-6.0, min(6.0, float(pct)))
    except (TypeError, ValueError):
        v = 0.0
    c = QColor(theme.C_UP if v >= 0 else theme.C_DOWN)
    c.setAlpha(int(35 + 175 * min(1.0, abs(v) / 6.0)))
    return c


class _SectorHeatmap(QWidget):
    """行业热力图: 一格一个行业, 颜色深浅=当日涨跌强度。"""

    COLUMNS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(3)
        self.setMinimumHeight(110)
        self.setMaximumHeight(210)

    def update_rows(self, rows: list[dict]):
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        rows = [r for r in (rows or []) if r.get("name")]
        if not rows:
            lab = QLabel("暂无行业数据")
            lab.setStyleSheet(f"color:{theme.C_MUTED};")
            self._grid.addWidget(lab, 0, 0)
            return
        for i, r in enumerate(rows):
            lab = QLabel(f"{r.get('name')}\n{_fmt_pct(r.get('pct'))}")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c = _heat_color(r.get("pct"))
            lab.setStyleSheet(
                f"QLabel {{ background: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});"
                f"color:{theme.C_TEXT}; border-radius:3px; padding:2px;"
                f"font-size:{theme.font_pt('mini')}; }}"
            )
            lab.setToolTip(
                f"{r.get('name')}\n"
                f"当日 {_fmt_pct(r.get('pct'))}   20日资金 {r.get('flow20_yi')} 亿\n"
                f"强度 {r.get('strength')}   风格 {r.get('tone') or '--'}"
            )
            self._grid.addWidget(lab, i // self.COLUMNS, i % self.COLUMNS)


# ─────────────────────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────────────────────


class AshareOutlookWindow(QMainWindow):
    def __init__(self, auto_load: bool = True):
        super().__init__()
        self.result: dict | None = None
        self._last_stock_data: dict = {}
        self._workers: set[QThread] = set()
        self._cons_worker: QThread | None = None
        self._cons_name: str = ""
        self._sector_manual: bool = False
        self.setWindowTitle("A股市场状态 · 独立版")
        self.resize(1280, 860)
        self._build_ui()
        self._load_journal_summary()
        self._load_stock_journal_summary()
        if auto_load:
            QTimer.singleShot(0, self.refresh)

    # ── UI ──────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(10)

        # 顶部标题 + 状态
        head = QHBoxLayout()
        title = QLabel("A股市场状态")
        title.setStyleSheet(
            f"color:{theme.C_TEXT};font-size:{theme.font_pt('display')};font-weight:bold;"
        )
        head.addWidget(title)
        self.lbl_head = QLabel("尚未加载")
        self.lbl_head.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('body')};")
        head.addWidget(self.lbl_head, 1)
        self.lbl_quality = QLabel("数据: --")
        self.lbl_quality.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        head.addWidget(self.lbl_quality)
        self.lbl_sources = QLabel("源: --")
        self.lbl_sources.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        head.addWidget(self.lbl_sources)
        self.lbl_alerts = QLabel("预警: --")
        self.lbl_alerts.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        head.addWidget(self.lbl_alerts)
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        head.addWidget(self.lbl_status)
        lay.addLayout(head)

        # 控制栏
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        ctrl.addWidget(self._label("指数"))
        self.cb_primary = QComboBox()
        for code, name in OUTLOOK_INDICES:
            self.cb_primary.addItem(name, code)
        self.cb_primary.setMinimumWidth(120)
        ctrl.addWidget(self.cb_primary)

        self.chk_sectors = QCheckBox("行业")
        self.chk_etf = QCheckBox("ETF")
        self.chk_liquidity = QCheckBox("流动性")
        self.chk_valuation = QCheckBox("估值")
        self.chk_backtest = QCheckBox("回测")
        self.chk_changes = QCheckBox("对比上次")
        self.chk_sectors.setToolTip("加载 90 个行业强弱（东财/同花顺，失败走新浪）")
        self.chk_etf.setToolTip("宽基 ETF 资金流/份额，推断国家队托底或撤退（限流时用量价代理）")
        self.chk_liquidity.setToolTip("M1/M2、社融、SHIBOR、10Y 国债，中期资金面")
        self.chk_valuation.setToolTip("指数 PE/股息率/ERP；历史分位靠本地每日累积")
        self.chk_backtest.setToolTip("状态分机械规则回测（含成本，仅参考）")
        self.chk_changes.setToolTip("与上次记录对比（需先记录过）")
        for chk in (
            self.chk_sectors,
            self.chk_etf,
            self.chk_liquidity,
            self.chk_valuation,
            self.chk_backtest,
            self.chk_changes,
        ):
            chk.setChecked(True)
            ctrl.addWidget(chk)

        ctrl.addStretch(1)
        self.btn_refresh = self._btn("刷新", self.refresh, primary=True)
        self.btn_record = self._btn("记录今日", self.record_current)
        self.btn_backfill = self._btn("回填", self.run_backfill)
        self.btn_stats = self._btn("统计", self.run_stats)
        self.btn_export = self._btn("导出", self.export_report)
        for b in (
            self.btn_refresh,
            self.btn_record,
            self.btn_backfill,
            self.btn_stats,
            self.btn_export,
        ):
            ctrl.addWidget(b)
        lay.addLayout(ctrl)

        # 页签
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_report_tab(), "报告")

        chart = QWidget()
        chart_lay = QVBoxLayout(chart)
        chart_lay.setContentsMargins(0, 0, 0, 0)
        chart_lay.setSpacing(6)
        self.kline_plot = pg.PlotWidget()
        self.score_plot = pg.PlotWidget()
        self.equity_plot = pg.PlotWidget()
        self.analog_plot = pg.PlotWidget()
        for plot in (self.kline_plot, self.score_plot, self.equity_plot, self.analog_plot):
            plot.setBackground(theme.C_PANEL)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.getAxis("left").setPen(pg.mkPen(theme.C_MUTED))
            plot.getAxis("bottom").setPen(pg.mkPen(theme.C_MUTED))
            plot.getAxis("left").setTextPen(pg.mkPen(theme.C_MUTED))
            plot.getAxis("bottom").setTextPen(pg.mkPen(theme.C_MUTED))
        self.kline_plot.setTitle("K线（刷新后显示）", color=theme.C_TEXT, size="10pt")
        self.kline_plot.addLegend(offset=(10, 10))
        self.score_plot.setTitle("状态分历史", color=theme.C_TEXT, size="10pt")
        self.equity_plot.setTitle("机械规则净值（仅参考）", color=theme.C_TEXT, size="10pt")
        self.equity_plot.addLegend(offset=(10, 10))
        self.analog_plot.setTitle("历史类比收益分布（刷新后显示）", color=theme.C_TEXT, size="10pt")
        self.analog_plot.addLegend(offset=(10, 10))
        split = QSplitter(Qt.Orientation.Vertical)
        for plot in (self.kline_plot, self.score_plot, self.equity_plot, self.analog_plot):
            split.addWidget(plot)
        split.setSizes([320, 200, 200, 180])
        chart_lay.addWidget(split, 1)
        self.chart_tab = chart
        self.tabs.addTab(chart, "图表")
        self.tabs.addTab(self._build_sector_tab(), "板块")
        self.tabs.addTab(self._build_stock_tab(), "个股")
        self.tabs.addTab(self._build_watchlist_tab(), "自选/批量")
        self.tabs.addTab(self._build_stock_journal_tab(), "个股日志")

        journal = QWidget()
        journal_lay = QVBoxLayout(journal)
        journal_lay.setContentsMargins(0, 0, 0, 0)
        self.journal_info = QLabel("")
        self.journal_info.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        journal_lay.addWidget(self.journal_info)
        self.journal_view = QTextBrowser()
        self.journal_view.setOpenExternalLinks(False)
        self.journal_view.setStyleSheet(
            f"QTextBrowser {{ background:{theme.C_BG}; color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER}; border-radius:6px; padding:8px; }}"
        )
        journal_lay.addWidget(self.journal_view, 1)
        self.tabs.addTab(journal, "预测日志")
        lay.addWidget(self.tabs, 1)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_conclusion)

    def _build_report_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.concl_card = QFrame()
        self.concl_card.setObjectName("conclCard")
        self.concl_card.setStyleSheet(
            f"QFrame#conclCard {{ background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER}; border-radius:6px; }}"
        )
        card_lay = QVBoxLayout(self.concl_card)
        card_lay.setContentsMargins(12, 8, 12, 8)
        card_lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.concl_state = QLabel("--")
        self.concl_state.setMinimumWidth(110)
        self.concl_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.concl_state.setStyleSheet(
            f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{theme.C_MUTED};"
        )
        top.addWidget(self.concl_state)
        self.concl_one = QLabel("刷新后显示一句话结论")
        self.concl_one.setWordWrap(True)
        self.concl_one.setStyleSheet(f"font-size:{theme.font_pt('body')};color:{theme.C_TEXT};")
        top.addWidget(self.concl_one, 1)
        self.btn_copy_concl = self._btn("复制结论", self.copy_conclusion)
        top.addWidget(self.btn_copy_concl)
        card_lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.concl_action = QLabel("仓位: --")
        self.concl_levels = QLabel("关键位: --")
        self.concl_alert = QLabel("预警: --")
        for lab in (self.concl_action, self.concl_levels, self.concl_alert):
            lab.setStyleSheet(f"font-size:{theme.font_pt('body-sm')};color:{theme.C_MUTED};")
            bottom.addWidget(lab, 1)
        card_lay.addLayout(bottom)
        lay.addWidget(self.concl_card)

        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(False)
        self.report_view.setStyleSheet(
            f"QTextBrowser {{ background:{theme.C_BG}; color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER}; border-radius:6px; padding:8px;"
            f"font-size:{theme.font_pt('body')}; }}"
        )
        self.report_view.setMarkdown("正在加载...")
        lay.addWidget(self.report_view, 1)
        return w

    def _label(self, text):
        lab = QLabel(text)
        lab.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('body-sm')};")
        return lab

    def _btn(self, text, slot, primary=False):
        b = QPushButton(text)
        b.clicked.connect(slot)
        b.setAutoDefault(False)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            b.setStyleSheet(
                f"QPushButton {{ background:{theme.C_ACCENT}; color:#fff;"
                f"border:none;border-radius:4px;padding:6px 14px;"
                f"font-size:{theme.font_pt('body-sm')};font-weight:bold; }}"
                f"QPushButton:disabled {{ background:{theme.C_BORDER}; }}"
            )
        return b

    def _build_sector_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.lbl_sector = QLabel("刷新后显示板块强弱（20日资金 + 当日强度）")
        self.lbl_sector.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        lay.addWidget(self.lbl_sector)
        self.sector_filter = QLineEdit()
        self.sector_filter.setPlaceholderText("搜索板块（如 银行 / 半导体）")
        self.sector_filter.textChanged.connect(self._filter_sectors)
        lay.addWidget(self.sector_filter)
        self.sector_heat = _SectorHeatmap()
        lay.addWidget(self.sector_heat)
        self.sector_table = QTableWidget(0, 6)
        self.sector_table.setHorizontalHeaderLabels(
            ["板块", "当日%", "20日资金(亿)", "强度", "风格", "成交额(亿)"]
        )
        self.sector_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sector_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sector_table.setSortingEnabled(True)
        self.sector_table.setAlternatingRowColors(True)
        self.sector_table.verticalHeader().setVisible(False)
        self.sector_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sector_table.cellDoubleClicked.connect(self._sector_to_stock)
        self.sector_table.itemSelectionChanged.connect(self._on_sector_selected)
        lay.addWidget(self.sector_table, 2)
        self.lbl_cons = QLabel("成分股：选一个行业")
        self.lbl_cons.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};")
        lay.addWidget(self.lbl_cons)
        self.cons_table = QTableWidget(0, 6)
        self.cons_table.setHorizontalHeaderLabels(
            ["代码", "名称", "涨跌%", "成交额(亿)", "换手%", "PE"]
        )
        self.cons_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cons_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cons_table.setSortingEnabled(True)
        self.cons_table.setAlternatingRowColors(True)
        self.cons_table.verticalHeader().setVisible(False)
        self.cons_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cons_table.cellDoubleClicked.connect(self._cons_to_stock)
        lay.addWidget(self.cons_table, 1)
        hint = QLabel("单击行业看成分股（新浪）；双击成分股直接分析；点表头排序。")
        hint.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(hint)
        return w

    def _build_stock_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(self._label("股票代码"))
        self.stock_code = QLineEdit("600519")
        self.stock_code.setPlaceholderText("如 600519 / sh600519 / 贵州茅台")
        self.stock_code.setMinimumWidth(220)
        self.stock_code.returnPressed.connect(self.analyze_stock)
        self.stock_code.textChanged.connect(self._on_stock_code_changed)
        top.addWidget(self.stock_code)
        self.stock_sector = QComboBox()
        self.stock_sector.addItem("（可选）所属板块", "")
        self.stock_sector.setMinimumWidth(150)
        self.stock_sector.currentIndexChanged.connect(self._on_sector_combo_changed)
        top.addWidget(self.stock_sector)
        self.btn_stock = self._btn("分析", self.analyze_stock, primary=True)
        top.addWidget(self.btn_stock)
        self.btn_stock_record = self._btn("记录个股", self.record_stock_current)
        top.addWidget(self.btn_stock_record)
        self.chk_stock_full = QCheckBox("抓资金/板块（慢）")
        top.addWidget(self.chk_stock_full)
        top.addStretch(1)
        lay.addLayout(top)

        self.stock_banner = QLabel("先刷新大盘，再把个股放到市场环境里看")
        self.stock_banner.setWordWrap(True)
        self.stock_banner.setStyleSheet(
            f"color:{theme.C_TEXT};font-size:{theme.font_pt('body-sm')};"
            f"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
            f"border-radius:4px;padding:6px;"
        )
        lay.addWidget(self.stock_banner)

        self.stock_check = QTableWidget(4, 3)
        self.stock_check.setHorizontalHeaderLabels(["维度", "现状", "结论"])
        self.stock_check.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stock_check.verticalHeader().setVisible(False)
        self.stock_check.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stock_check.setMaximumHeight(150)
        lay.addWidget(self.stock_check)

        self.stock_summary = QTableWidget(0, 3)
        self.stock_summary.setHorizontalHeaderLabels(["项目", "数值", "方向"])
        self.stock_summary.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stock_summary.verticalHeader().setVisible(False)
        self.stock_summary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stock_summary.setMaximumHeight(230)
        lay.addWidget(self.stock_summary)

        self.stock_view = QTextBrowser()
        self.stock_view.setOpenExternalLinks(False)
        self.stock_view.setStyleSheet(
            f"QTextBrowser {{ background:{theme.C_BG}; color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER}; border-radius:6px; padding:8px; }}"
        )
        lay.addWidget(self.stock_view, 1)
        note = QLabel("个股分析来自旧 Wyckoff 系统，逆风局请打折；不构成投资建议。")
        note.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(note)
        self.stock_tab = w
        self._update_stock_check({})
        return w

    def _build_watchlist_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(self._label("代码"))
        self.watch_codes = QLineEdit()
        self.watch_codes.setPlaceholderText("批量添加：600519, 000001, sh600519")
        self.watch_codes.setMinimumWidth(280)
        top.addWidget(self.watch_codes, 1)
        self.btn_watch_add = self._btn("添加自选", self._add_watchlist)
        top.addWidget(self.btn_watch_add)
        self.btn_watch_remove = self._btn("移除选中", self._remove_watchlist)
        top.addWidget(self.btn_watch_remove)
        self.btn_watch_clear = self._btn("清空自选", self._clear_watchlist)
        top.addWidget(self.btn_watch_clear)
        lay.addLayout(top)

        self.watch_table = QTableWidget(0, 3)
        self.watch_table.setHorizontalHeaderLabels(["代码", "名称", "添加时间"])
        self.watch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.watch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.watch_table.setAlternatingRowColors(True)
        self.watch_table.verticalHeader().setVisible(False)
        self.watch_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.watch_table.cellDoubleClicked.connect(self._watch_to_stock)
        lay.addWidget(self.watch_table, 1)

        mid = QHBoxLayout()
        self.lbl_batch_info = QLabel("批量：选中自选股后运行；不选中则全部运行")
        self.lbl_batch_info.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        mid.addWidget(self.lbl_batch_info, 1)
        self.btn_batch_analyze = self._btn("批量分析", self._batch_analyze, primary=True)
        mid.addWidget(self.btn_batch_analyze)
        self.btn_batch_record = self._btn("批量记录", self._batch_record)
        mid.addWidget(self.btn_batch_record)
        lay.addLayout(mid)

        self.batch_table = QTableWidget(0, 8)
        self.batch_table.setHorizontalHeaderLabels(
            ["代码", "名称", "阶段", "综合分", "方向", "止损", "目标1", "记录状态"]
        )
        self.batch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.batch_table.setAlternatingRowColors(True)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.batch_table, 1)

        note = QLabel("批量分析为快速模式（不抓资金/板块）；批量记录会写入个股日志。")
        note.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(note)

        self.watch_tab = w
        self._batch_data: dict[str, dict] = {}
        self._load_watchlist()
        return w

    def _load_watchlist(self):
        items = list_watchlist()
        self.watch_table.setRowCount(len(items))
        for i, r in enumerate(items):
            code_item = QTableWidgetItem(str(r.get("code") or "--"))
            code_item.setData(Qt.ItemDataRole.UserRole, r)
            name_item = QTableWidgetItem(str(r.get("name") or ""))
            added_item = QTableWidgetItem(str(r.get("added_at") or ""))
            self.watch_table.setItem(i, 0, code_item)
            self.watch_table.setItem(i, 1, name_item)
            self.watch_table.setItem(i, 2, added_item)

    def _add_watchlist(self):
        codes = parse_code_list(self.watch_codes.text())
        if not codes:
            return
        add_watchlist(codes)
        self.watch_codes.clear()
        self._load_watchlist()

    def _selected_watch_codes(self) -> list[str]:
        rows = sorted({i.row() for i in self.watch_table.selectedItems()})
        if rows:
            out = []
            for r in rows:
                item = self.watch_table.item(r, 0)
                if item and item.text():
                    out.append(item.text())
            return out
        return [
            self.watch_table.item(i, 0).text()
            for i in range(self.watch_table.rowCount())
            if self.watch_table.item(i, 0) and self.watch_table.item(i, 0).text()
        ]

    def _remove_watchlist(self):
        codes = self._selected_watch_codes()
        if not codes:
            return
        remove_watchlist(codes)
        self._load_watchlist()

    def _clear_watchlist(self):
        if self.watch_table.rowCount() == 0:
            return
        ans = QMessageBox.question(self, "清空自选", "确定清空所有自选股吗？")
        if ans == QMessageBox.StandardButton.Yes:
            clear_watchlist()
            self._load_watchlist()

    def _watch_to_stock(self, row: int, _col: int):
        item = self.watch_table.item(row, 0)
        if not item or not item.text():
            return
        self.stock_code.setText(item.text())
        self.tabs.setCurrentWidget(self.stock_tab)
        self.analyze_stock()

    def _batch_analyze(self):
        self._run_batch(record=False)

    def _batch_record(self):
        codes = self._selected_watch_codes()
        if not codes:
            return
        if all(code in self._batch_data for code in codes):
            for code in codes:
                self._record_batch_code(code)
            self.lbl_batch_info.setText(f"批量记录：{len(codes)} 只（基于最近分析）")
            return
        self._run_batch(record=True)

    def _run_batch(self, record: bool):
        codes = self._selected_watch_codes()
        if not codes:
            QMessageBox.information(self, "提示", "先添加自选股，或选中要运行的代码。")
            return
        if len(codes) > 30:
            ans = QMessageBox.question(
                self,
                "数量较多",
                f"将运行 {len(codes)} 只，最多建议 30 只。继续吗？",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            codes = codes[:30]
        self._batch_data = {}
        self.batch_table.setRowCount(0)
        worker = _BatchWorker(codes, self.result, record=record, parent=self)
        worker.row.connect(self._on_batch_row)
        worker.progress.connect(self._on_batch_progress)
        msg = f"批量{'分析并记录' if record else '分析'} {len(codes)} 只..."
        self._start(worker, self._on_batch_done, self._on_error, msg)

    def _on_batch_progress(self, i: int, n: int):
        self.lbl_batch_info.setText(f"批量 {i}/{n}")

    def _on_batch_row(self, row: dict):
        r = self.batch_table.rowCount()
        self.batch_table.insertRow(r)
        for col, key in enumerate(
            ("code", "name", "phase", "score", "direction", "stop", "target1", "record_status")
        ):
            value = row.get(key)
            item = QTableWidgetItem("" if value is None else str(value))
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, row)
            self.batch_table.setItem(r, col, item)

    def _on_batch_done(self, summary: dict):
        self._batch_data = {}
        for item in summary.get("results") or []:
            row = item.get("row") or {}
            data = item.get("data") or {}
            if row.get("code") and data:
                self._batch_data[str(row.get("code"))] = data
        self.lbl_batch_info.setText(f"完成 {summary.get('n', 0)} 只；可点“批量记录”写入个股日志。")

    def _record_batch_code(self, code: str):
        data = self._batch_data.get(code)
        if not data:
            return
        try:
            res = record_stock(data, self.result)
            status = "已记录" if res.get("ok") else f"失败: {res.get('reason')}"
        except Exception as e:  # noqa: BLE001
            status = f"失败: {type(e).__name__}: {e}"
        for i in range(self.batch_table.rowCount()):
            item = self.batch_table.item(i, 0)
            if item and item.text() == code:
                self.batch_table.setItem(i, 7, QTableWidgetItem(status))
                break

    def _build_stock_journal_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.stock_journal_info = QLabel("个股日志: --")
        self.stock_journal_info.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
        )
        top.addWidget(self.stock_journal_info, 1)
        self.btn_stock_backfill = self._btn("回填个股", self.run_stock_backfill)
        self.btn_stock_stats = self._btn("个股统计", self.run_stock_stats)
        top.addWidget(self.btn_stock_backfill)
        top.addWidget(self.btn_stock_stats)
        lay.addLayout(top)
        self.stock_journal_view = QTextBrowser()
        self.stock_journal_view.setOpenExternalLinks(False)
        self.stock_journal_view.setStyleSheet(
            f"QTextBrowser {{ background:{theme.C_BG}; color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER}; border-radius:6px; padding:8px; }}"
        )
        lay.addWidget(self.stock_journal_view, 1)
        hint = QLabel("记录个股后，等 5/20/60 个交易日再回填；样本少时只看方向。")
        hint.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(hint)
        self._stock_journal_tab = w
        return w

    # ── 任务管理 ────────────────────────────────────────────
    def _start(self, worker: QThread, on_done, on_failed, msg: str):
        if self._workers:
            return
        self._workers.add(worker)
        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        worker.finished.connect(lambda w=worker: self._finish_worker(w))
        self._set_busy(True, msg)
        worker.start()

    def _finish_worker(self, worker: QThread):
        self._workers.discard(worker)
        self._set_busy(False, "就绪")

    def _set_busy(self, busy: bool, msg: str = ""):
        for b in (
            self.btn_refresh,
            self.btn_backfill,
            self.btn_stats,
            self.btn_record,
            self.btn_stock,
            self.btn_stock_record,
            self.btn_stock_backfill,
            self.btn_stock_stats,
            self.btn_batch_analyze,
            self.btn_batch_record,
        ):
            b.setEnabled(not busy)
        self.lbl_status.setText(msg if busy else "就绪")
        if msg:
            self.statusBar().showMessage(msg)

    def _on_error(self, msg: str):
        self.lbl_status.setText("失败")
        QMessageBox.critical(self, "出错了", msg)

    # ── 刷新 ────────────────────────────────────────────────
    def refresh(self):
        primary = self.cb_primary.currentData() or "sh000001"
        options = {
            "datalen": 1200,
            "include_sectors": self.chk_sectors.isChecked(),
            "include_etf": self.chk_etf.isChecked(),
            "include_liquidity": self.chk_liquidity.isChecked(),
            "include_valuation": self.chk_valuation.isChecked(),
            "include_backtest": self.chk_backtest.isChecked(),
            "include_changes": self.chk_changes.isChecked(),
            "progressive": True,
        }
        worker = _OutlookWorker(primary, options, self)
        worker.partial.connect(self._on_outlook)
        worker.stage.connect(self._on_stage)
        self._start(worker, self._on_outlook, self._on_error, "正在拉取指数核心...")

    def _on_stage(self, msg: str):
        self.lbl_status.setText(msg)
        self.statusBar().showMessage(msg)

    def _on_outlook(self, result: dict):
        self.result = result
        if result.get("error"):
            self.report_view.setMarkdown(f"# 加载失败\n\n{result['error']}")
            self.lbl_head.setText("加载失败")
            self.concl_state.setText("加载失败")
            self.concl_one.setText(result["error"])
            return
        self.report_view.setMarkdown(render_report(result))
        self._update_header(result)
        self._update_conclusion_card(result)
        issues = _quality_issues(result.get("data_quality") or {})
        if issues:
            text = "数据: " + "、".join(issues[:3])
            if len(issues) > 3:
                text += "…"
            self.lbl_quality.setText(text)
            self.lbl_quality.setStyleSheet(
                f"color:{theme.C_DOWN};font-size:{theme.font_pt('caption')};"
            )
        else:
            self.lbl_quality.setText("数据: 正常")
            self.lbl_quality.setStyleSheet(
                f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
            )
        self._update_source_label(result)
        self._update_alerts(result.get("alerts") or [])
        self._render_score_chart(result)
        self._render_equity_chart(result)
        self._render_kline(result)
        self._render_analogs(result)
        self._render_sectors(result)
        self._populate_sector_combo(result)
        self._filter_sectors(self.sector_filter.text())
        self._update_stock_check(getattr(self, "_last_stock_data", {}))
        self._load_journal_summary()

    def _update_source_label(self, result: dict):
        dq = result.get("data_quality") or {}
        n_ok = n_stale = n_missing = 0
        for key in ("sectors", "etf", "liquidity", "valuation"):
            q = dq.get(key)
            if not isinstance(q, dict):
                continue
            if q.get("ok") and not q.get("stale"):
                n_ok += 1
            elif q.get("stale"):
                n_stale += 1
            else:
                n_missing += 1
        if n_ok + n_stale + n_missing == 0:
            self.lbl_sources.setText("源: --")
            color = theme.C_MUTED
        else:
            self.lbl_sources.setText(f"源: 正常 {n_ok} / 过期 {n_stale} / 缺失 {n_missing}")
            color = theme.C_DOWN if n_missing else theme.C_AMBER if n_stale else theme.C_MUTED
        self.lbl_sources.setStyleSheet(f"color:{color};font-size:{theme.font_pt('caption')};")

    def _update_alerts(self, alerts: list[dict]):
        if not alerts:
            self.lbl_alerts.setText("预警: 无")
            self.lbl_alerts.setToolTip("当前没有触发机械预警。")
            self.lbl_alerts.setStyleSheet(
                f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};"
            )
            return
        warns = [a for a in alerts if a.get("level") == "warn"]
        titles = [str(a.get("title") or "") for a in alerts if a.get("title")]
        text = "预警: " + "、".join(titles[:3])
        if len(titles) > 3:
            text += "…"
        self.lbl_alerts.setText(text)
        self.lbl_alerts.setToolTip(
            "\n".join(f"[{a.get('level')}] {a.get('title')}：{a.get('detail')}" for a in alerts)
        )
        color = theme.C_DOWN if warns else theme.C_AMBER
        self.lbl_alerts.setStyleSheet(
            f"color:{color};font-size:{theme.font_pt('caption')};font-weight:bold;"
        )

    def _update_header(self, result: dict):
        primary = result.get("primary")
        p = (result.get("indices") or {}).get(primary) or {}
        rb = result.get("risk_budget") or {}
        state = p.get("state") or "--"
        color = theme.C_MUTED
        if state in ("进攻", "中性偏多"):
            color = theme.C_UP
        elif state in ("防御", "谨慎", "风险规避"):
            color = theme.C_DOWN
        self.lbl_head.setText(
            f"{p.get('name', '--')}  {p.get('close', '--')}  "
            f"{state}（{p.get('score', '--')} 分）  "
            f"仓位参考 {rb.get('low', '--')}%-{rb.get('high', '--')}%"
        )
        self.lbl_head.setStyleSheet(
            f"color:{color};font-size:{theme.font_pt('body')};font-weight:bold;"
        )

    def _update_conclusion_card(self, result: dict):
        p = (result.get("indices") or {}).get(result.get("primary")) or {}
        rb = result.get("risk_budget") or {}
        lv = result.get("primary_levels") or {}
        alerts = result.get("alerts") or []
        state = p.get("state") or "--"
        score = p.get("score")
        color = theme.C_MUTED
        if state in ("进攻", "中性偏多"):
            color = theme.C_UP
        elif state in ("防御", "谨慎", "风险规避"):
            color = theme.C_DOWN
        self.concl_state.setText(f"{state}\n{score} 分" if score is not None else state)
        self.concl_state.setStyleSheet(
            f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{color};"
        )
        rows = _plain_conclusion(result)
        one = next((a for q, a in rows if q == "一句话结论"), "")
        self.concl_one.setText(one or f"{p.get('name') or ''} {state}")
        pos = (
            f"{rb.get('low')}%-{rb.get('high')}%"
            if rb.get("low") is not None and rb.get("high") is not None
            else "--"
        )
        self.concl_action.setText(f"仓位参考: {pos}")
        sup, res = lv.get("tr_bottom"), lv.get("tr_top")
        self.concl_levels.setText(
            f"关键位: 下沿 {sup if sup is not None else '--'} / "
            f"上沿 {res if res is not None else '--'}"
        )
        if alerts:
            self.concl_alert.setText(f"预警: {len(alerts)} 条 · {alerts[0].get('title')}")
            self.concl_alert.setStyleSheet(
                f"font-size:{theme.font_pt('body-sm')};color:{theme.C_AMBER};"
            )
        else:
            self.concl_alert.setText("预警: 无")
            self.concl_alert.setStyleSheet(
                f"font-size:{theme.font_pt('body-sm')};color:{theme.C_MUTED};"
            )
        self.concl_card.setStyleSheet(
            f"QFrame#conclCard {{ background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER}; border-left:4px solid {color};"
            f"border-radius:6px; }}"
        )

    def copy_conclusion(self):
        if not self.result:
            QMessageBox.information(self, "提示", "先刷新报告，再复制结论。")
            return
        text = "\n".join(f"{q} {a}" for q, a in _plain_conclusion(self.result))
        QApplication.clipboard().setText(text)
        self.lbl_status.setText("结论已复制")

    # ── 板块 ────────────────────────────────────────────────
    def _render_sectors(self, result: dict):
        sec = result.get("sectors") or {}
        rows = sec.get("all") or []
        if not rows:
            rows = list(sec.get("top") or []) + list(sec.get("bottom") or [])
        self.sector_heat.update_rows(sec.get("all") or rows)
        self.sector_table.blockSignals(True)
        self.sector_table.setSortingEnabled(False)
        self.sector_table.setRowCount(len(rows))
        self.cons_table.setRowCount(0)
        self.lbl_cons.setText("成分股：选一个行业")
        self._cons_name = ""
        up_color = QColor(theme.C_UP)
        down_color = QColor(theme.C_DOWN)
        for i, r in enumerate(rows):
            pct = r.get("pct")
            flow = r.get("flow20_yi")
            strength = r.get("strength")
            amount = r.get("amount_yi")
            name_item = QTableWidgetItem(str(r.get("name") or "--"))
            name_item.setData(
                Qt.ItemDataRole.UserRole,
                {"name": r.get("name"), "label": r.get("label")},
            )
            pct_item = QTableWidgetItem()
            pct_item.setData(Qt.ItemDataRole.DisplayRole, float(pct or 0.0))
            flow_item = QTableWidgetItem()
            flow_item.setData(Qt.ItemDataRole.DisplayRole, float(flow or 0.0))
            strength_item = QTableWidgetItem()
            strength_item.setData(Qt.ItemDataRole.DisplayRole, float(strength or 0.0))
            amount_item = QTableWidgetItem()
            amount_item.setData(Qt.ItemDataRole.DisplayRole, float(amount or 0.0))
            tone_map = {
                "bullish": "偏强",
                "bearish": "偏弱",
                "mixed": "混合",
                "neutral": "中性",
            }
            tone_item = QTableWidgetItem(
                tone_map.get(str(r.get("tone")), str(r.get("tone") or "--"))
            )
            if pct is not None:
                bg = QColor(up_color if pct > 0 else down_color)
                bg.setAlpha(35)
                pct_item.setBackground(QBrush(bg))
            if flow is not None:
                flow_item.setForeground(QBrush(QColor(theme.C_UP if flow > 0 else theme.C_DOWN)))
            self.sector_table.setItem(i, 0, name_item)
            self.sector_table.setItem(i, 1, pct_item)
            self.sector_table.setItem(i, 2, flow_item)
            self.sector_table.setItem(i, 3, strength_item)
            self.sector_table.setItem(i, 4, tone_item)
            self.sector_table.setItem(i, 5, amount_item)
        self.sector_table.setSortingEnabled(True)
        self.sector_table.sortItems(3, Qt.SortOrder.DescendingOrder)
        self.sector_table.blockSignals(False)
        gap = sec.get("style_gap")
        gap_txt = "--" if gap is None else f"{gap:+.2f}"
        self.lbl_sector.setText(
            f"板块强弱：{len(rows)} 个；防御/成长 20 日资金差 {gap_txt} 亿；点表头排序。"
        )

    def _filter_sectors(self, text: str):
        text = (text or "").strip()
        for i in range(self.sector_table.rowCount()):
            item = self.sector_table.item(i, 0)
            name = item.text() if item else ""
            self.sector_table.setRowHidden(i, bool(text) and text not in name)

    def _on_sector_selected(self):
        item = self.sector_table.currentItem()
        if not item:
            return
        name_item = self.sector_table.item(item.row(), 0)
        if not name_item:
            return
        meta = name_item.data(Qt.ItemDataRole.UserRole) or {}
        name = str(meta.get("name") or name_item.text() or "").strip()
        if not name or name == "--":
            return
        if self._cons_name == name and self.cons_table.rowCount() > 0:
            return
        self._cons_name = name
        self.lbl_cons.setText(f"成分股：{name} 加载中...")
        self.cons_table.setRowCount(0)
        worker = _ConstituentWorker(name, meta.get("label"), self)
        self._cons_worker = worker
        self._workers.add(worker)
        worker.done.connect(self._on_constituents)
        worker.finished.connect(lambda: self._workers.discard(worker))
        worker.start()

    def _on_constituents(self, data: dict):
        name = str(data.get("name") or "")
        if name != self._cons_name:
            return
        err = data.get("error")
        if err:
            self.lbl_cons.setText(f"成分股：{name} 加载失败（{str(err)[:80]}）")
            return
        rows = data.get("rows") or []
        if not rows:
            self.lbl_cons.setText(f"成分股：{name} · 0 只（东财限流或新浪无对应行业）")
            return
        src = next((r.get("source") for r in rows if r.get("source")), "")
        src_txt = {"eastmoney": "东财", "sina": "新浪"}.get(str(src), str(src))
        self.lbl_cons.setText(
            f"成分股：{name} · {len(rows)} 只" + (f" · 来源{src_txt}" if src_txt else "")
        )
        self.cons_table.setSortingEnabled(False)
        self.cons_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pct = r.get("pct")
            code_item = QTableWidgetItem(str(r.get("code") or "--"))
            code_item.setData(Qt.ItemDataRole.UserRole, r)
            name_item = QTableWidgetItem(str(r.get("name") or "--"))
            name_item.setData(Qt.ItemDataRole.UserRole, r)
            pct_item = QTableWidgetItem()
            pct_item.setData(Qt.ItemDataRole.DisplayRole, float(pct or 0.0))
            amount_item = QTableWidgetItem()
            amount_item.setData(Qt.ItemDataRole.DisplayRole, float(r.get("amount_yi") or 0.0))
            turnover_item = QTableWidgetItem()
            turnover_item.setData(Qt.ItemDataRole.DisplayRole, float(r.get("turnover") or 0.0))
            pe_item = QTableWidgetItem()
            pe_item.setData(Qt.ItemDataRole.DisplayRole, float(r.get("pe") or 0.0))
            if pct is not None:
                color = theme.C_UP if pct > 0 else theme.C_DOWN
                pct_item.setForeground(QBrush(QColor(color)))
                bg = QColor(color)
                bg.setAlpha(30)
                pct_item.setBackground(QBrush(bg))
            self.cons_table.setItem(i, 0, code_item)
            self.cons_table.setItem(i, 1, name_item)
            self.cons_table.setItem(i, 2, pct_item)
            self.cons_table.setItem(i, 3, amount_item)
            self.cons_table.setItem(i, 4, turnover_item)
            self.cons_table.setItem(i, 5, pe_item)
        self.cons_table.setSortingEnabled(True)
        if rows:
            self.cons_table.sortItems(2, Qt.SortOrder.DescendingOrder)

    def _cons_to_stock(self, row: int, _col: int):
        item = self.cons_table.item(row, 0)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole) or {}
        code = str(meta.get("code") or item.text() or "").strip()
        if not code or code == "--":
            return
        if self._cons_name:
            idx = self.stock_sector.findData(self._cons_name)
            if idx < 0:
                self.stock_sector.addItem(self._cons_name, self._cons_name)
                idx = self.stock_sector.count() - 1
            self.stock_sector.setCurrentIndex(idx)
        self.tabs.setCurrentWidget(self.stock_tab)
        self.stock_code.setText(code)
        self.analyze_stock()

    def _sector_to_stock(self, row: int, _col: int):
        item = self.sector_table.item(row, 0)
        if not item:
            return
        name = item.text()
        idx = self.stock_sector.findData(name)
        if idx < 0:
            self.stock_sector.addItem(name, name)
            idx = self.stock_sector.count() - 1
        self.stock_sector.setCurrentIndex(idx)
        self.tabs.setCurrentWidget(self.stock_tab)
        self.stock_code.setFocus()
        self.stock_code.selectAll()

    def _populate_sector_combo(self, result: dict):
        rows = (result.get("sectors") or {}).get("all") or []
        current = self.stock_sector.currentData()
        self.stock_sector.blockSignals(True)
        self.stock_sector.clear()
        self.stock_sector.addItem("（可选）所属板块", "")
        for r in rows:
            name = r.get("name")
            if name:
                self.stock_sector.addItem(str(name), str(name))
        idx = self.stock_sector.findData(current) if current else -1
        self.stock_sector.setCurrentIndex(idx if idx >= 0 else 0)
        self._sector_manual = bool(self.stock_sector.currentData())
        self.stock_sector.blockSignals(False)

    # ── 个股 ────────────────────────────────────────────────
    def _set_check_row(self, row: int, dim: str, status: str, tone: str):
        tone_map = {
            "bullish": "偏多",
            "bearish": "偏空",
            "caution": "谨慎",
            "neutral": "中性",
        }
        color = theme.C_MUTED
        if tone == "bullish":
            color = theme.C_UP
        elif tone == "bearish":
            color = theme.C_DOWN
        elif tone == "caution":
            color = theme.C_ACCENT
        label = tone_map.get(tone, tone or "中性")
        self.stock_check.setItem(row, 0, QTableWidgetItem(dim))
        status_item = QTableWidgetItem(status)
        status_item.setForeground(QBrush(QColor(color)))
        self.stock_check.setItem(row, 1, status_item)
        label_item = QTableWidgetItem(label)
        label_item.setForeground(QBrush(QColor(color)))
        self.stock_check.setItem(row, 2, label_item)

    def _on_sector_combo_changed(self, _idx: int):
        self._sector_manual = bool(self.stock_sector.currentData())
        self._update_stock_check(getattr(self, "_last_stock_data", {}))

    def _on_stock_code_changed(self, text: str):
        if not (text or "").strip():
            self._sector_manual = False

    def _auto_select_sector(self, industry: dict):
        name = str((industry or {}).get("industry") or "").strip()
        if not name:
            return
        current = self.stock_sector.currentData()
        if self._sector_manual and current:
            return
        self.stock_sector.blockSignals(True)
        idx = self.stock_sector.findData(name)
        if idx < 0:
            for cand in (industry or {}).get("candidates") or []:
                cand = str(cand or "").strip()
                if not cand:
                    continue
                idx = self.stock_sector.findData(cand)
                if idx >= 0:
                    break
        if idx < 0:
            self.stock_sector.addItem(name, name)
            idx = self.stock_sector.count() - 1
        self.stock_sector.setCurrentIndex(idx)
        self._sector_manual = False
        self.stock_sector.blockSignals(False)

    def _update_stock_check(self, data: dict):
        # 大盘
        market_status = "大盘未加载"
        market_tone = "neutral"
        if self.result:
            primary = self.result.get("primary")
            p = (self.result.get("indices") or {}).get(primary) or {}
            rb = self.result.get("risk_budget") or {}
            state = p.get("state") or "--"
            market_status = (
                f"{state}（{p.get('score', '--')} 分）/ "
                f"仓位 {rb.get('low', '--')}%-{rb.get('high', '--')}%"
            )
            if state in ("进攻", "中性偏多"):
                market_tone = "bullish"
            elif state in ("防御", "谨慎", "风险规避"):
                market_tone = "bearish"

        # 板块
        sector_name = self.stock_sector.currentData() or ""
        sector_status = "未选板块"
        sector_tone = "neutral"
        if self.result and sector_name:
            rows = (self.result.get("sectors") or {}).get("all") or []
            row = next((r for r in rows if r.get("name") == sector_name), None)
            if row:
                sector_status = (
                    f"{sector_name} 强度 {row.get('strength', '--')} / "
                    f"20日资金 {row.get('flow20_yi', '--')} 亿"
                )
                sector_tone = row.get("tone") or "neutral"

        # 个股
        pre = (data or {}).get("precomputed") or {}
        fusion = pre.get("fusion") or {}
        stock_status = "先分析个股"
        stock_tone = "neutral"
        if data:
            score = fusion.get("score")
            stock_status = (
                f"{pre.get('phase', '--')} / 综合 {score if score is not None else '--'}"
                f"（{fusion.get('confidence', '--')}）"
            )
            if score is not None and score > 0:
                stock_tone = "bullish"
            elif score is not None and score < 0:
                stock_tone = "bearish"

        # 结论
        tones = [market_tone, sector_tone, stock_tone]
        bulls = tones.count("bullish")
        bears = tones.count("bearish")
        if not data:
            conclusion, concl_tone = "先分析个股，再看三击", "neutral"
        elif not sector_name:
            conclusion, concl_tone = "板块未选，先看大盘+个股", "neutral"
        elif bulls == 3:
            conclusion, concl_tone = "三击共振（偏多）", "bullish"
        elif market_tone == "bearish":
            conclusion, concl_tone = "大盘逆风，降级/轻仓", "bearish"
        elif sector_tone == "bearish":
            conclusion, concl_tone = "板块逆风，谨慎", "bearish"
        elif bears >= 2:
            conclusion, concl_tone = "两项偏空，观望", "bearish"
        else:
            conclusion, concl_tone = "信号分歧，等确认", "neutral"

        self._set_check_row(0, "大盘", market_status, market_tone)
        self._set_check_row(1, "板块", sector_status, sector_tone)
        self._set_check_row(2, "个股", stock_status, stock_tone)
        self._set_check_row(3, "三击结论", conclusion, concl_tone)

    def analyze_stock(self):
        code = self.stock_code.text().strip()
        if not code:
            QMessageBox.information(self, "提示", "先输入股票代码。")
            return
        sector_names = [
            self.stock_sector.itemData(i)
            for i in range(1, self.stock_sector.count())
            if self.stock_sector.itemData(i)
        ]
        worker = _StockWorker(code, self.chk_stock_full.isChecked(), sector_names, self)
        self._start(worker, self._on_stock, self._on_error, f"正在分析 {code}...")

    def _on_stock(self, data: dict):
        self._last_stock_data = data
        pre = data.get("precomputed") or {}
        phase = pre.get("phase") or "--"
        fusion = pre.get("fusion") or {}
        code = data.get("code") or "--"
        market_txt = "大盘未加载"
        if self.result:
            primary = self.result.get("primary")
            p = (self.result.get("indices") or {}).get(primary) or {}
            rb = self.result.get("risk_budget") or {}
            market_txt = (
                f"大盘 {p.get('name', '--')} {p.get('state', '--')}"
                f"（{p.get('score', '--')} 分），仓位参考 "
                f"{rb.get('low', '--')}%-{rb.get('high', '--')}%"
            )
        fusion_txt = f"综合 {fusion.get('score', '--')} ({fusion.get('confidence', '--')})"
        ind = data.get("industry") or {}
        industry_txt = ""
        if ind.get("industry"):
            src_map = {"local": "本地", "cninfo": "巨潮/中证", "eastmoney": "东财"}
            src = src_map.get(str(ind.get("source")), str(ind.get("source") or ""))
            industry_txt = f"   |   行业 {ind.get('industry')}"
            if src:
                industry_txt += f"（{src}）"
        self.stock_banner.setText(f"{market_txt}   |   {code}  {phase}  {fusion_txt}{industry_txt}")
        self._auto_select_sector(ind)
        self._update_stock_check(data)

        summary = data.get("summary") or []
        self.stock_summary.setRowCount(len(summary))
        for i, card in enumerate(summary):
            label = QTableWidgetItem(str(card.get("label") or ""))
            value = QTableWidgetItem(str(card.get("value") or ""))
            tone = card.get("tone")
            if tone == "bullish":
                value.setForeground(QBrush(QColor(theme.C_UP)))
            elif tone == "bearish":
                value.setForeground(QBrush(QColor(theme.C_DOWN)))
            elif tone == "caution":
                value.setForeground(QBrush(QColor(theme.C_ACCENT)))
            tone_map = {
                "bullish": "偏多",
                "bearish": "偏空",
                "caution": "谨慎",
                "neutral": "中性",
            }
            self.stock_summary.setItem(i, 0, label)
            self.stock_summary.setItem(i, 1, value)
            self.stock_summary.setItem(
                i, 2, QTableWidgetItem(tone_map.get(str(tone), str(tone or "")))
            )

        text = data.get("text") or "（无分析结果）"
        if not data.get("confirm"):
            text = (
                "【快速模式】未抓资金/板块/基本面，结论只基于K线结构。\n"
                "完整确认请勾选“抓资金/板块（慢）”或使用主程序。\n\n" + text
            )
        self.stock_view.setPlainText(text)

    # ── 图表 ────────────────────────────────────────────────
    def _x_ticks(self, dates: list[str]):
        n = len(dates)
        if n == 0:
            return []
        step = max(1, n // 6)
        return [(i, dates[i][5:]) for i in range(0, n, step)]

    def _render_score_chart(self, result: dict):
        self.score_plot.clear()
        hist = result.get("score_history") or {}
        dates = hist.get("dates") or []
        scores = hist.get("score") or []
        if not dates or not scores:
            self.score_plot.setTitle("状态分历史（无数据）", color=theme.C_TEXT)
            return
        xs = [i for i, v in enumerate(scores) if v is not None]
        ys = [scores[i] for i in xs]
        for level, color in (
            (20, theme.C_DOWN),
            (30, theme.C_MUTED),
            (50, theme.C_MUTED),
            (70, theme.C_UP),
        ):
            self.score_plot.addItem(
                pg.InfiniteLine(
                    pos=level, angle=0, pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine)
                )
            )
        self.score_plot.plot(
            xs,
            ys,
            pen=pg.mkPen(theme.C_ACCENT, width=2),
            connect="finite",
        )
        self.score_plot.setYRange(0, 100)
        self.score_plot.getAxis("bottom").setTicks([self._x_ticks(dates)])
        self.score_plot.setTitle(
            "状态分历史（20/30/50/70 分界线）", color=theme.C_TEXT, size="10pt"
        )

    def _render_equity_chart(self, result: dict):
        self.equity_plot.clear()
        legend = self.equity_plot.plotItem.legend
        if legend is not None:
            legend.clear()
        bt = result.get("backtest_strategy") or {}
        eq = bt.get("equity") or {}
        dates = eq.get("dates") or []
        if not dates:
            self.equity_plot.setTitle("机械规则净值（无回测数据）", color=theme.C_TEXT)
            return
        xs = list(range(len(dates)))
        series = (
            ("strategy", "状态分", theme.C_ACCENT),
            ("overlay", "MA200+状态分", theme.C_AMBER),
            ("ma200", "MA200", theme.C_UP),
            ("buy_hold", "买入持有", theme.C_MUTED),
        )
        for key, name, color in series:
            vals = eq.get(key) or []
            if vals:
                self.equity_plot.plot(
                    xs[: len(vals)], vals, pen=pg.mkPen(color, width=2), name=name
                )
        self.equity_plot.getAxis("bottom").setTicks([self._x_ticks(dates)])
        self.equity_plot.setTitle("机械规则净值（仅参考）", color=theme.C_TEXT, size="10pt")

    def _render_kline(self, result: dict):
        self.kline_plot.clear()
        legend = self.kline_plot.plotItem.legend
        if legend is not None:
            legend.clear()
        ph = result.get("price_history") or {}
        dates = ph.get("dates") or []
        try:
            o = np.asarray(ph.get("open"), dtype=float)
            h = np.asarray(ph.get("high"), dtype=float)
            lo = np.asarray(ph.get("low"), dtype=float)
            c = np.asarray(ph.get("close"), dtype=float)
        except (TypeError, ValueError):
            o = h = lo = c = np.array([])
        if not dates or len(c) == 0 or not (len(o) == len(h) == len(lo) == len(c)):
            self.kline_plot.setTitle("K线（无数据）", color=theme.C_TEXT, size="10pt")
            return
        xs = np.arange(len(c), dtype=float)
        valid = np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & np.isfinite(c)
        up = c >= o
        span = float(np.nanmax(h[valid]) - np.nanmin(lo[valid])) if valid.any() else 0.0
        min_body = max(span * 0.0008, 1e-6)
        for mask, color in ((valid & up, theme.C_UP), (valid & ~up, theme.C_DOWN)):
            if not mask.any():
                continue
            wx = np.repeat(xs[mask], 2)
            wy = np.empty(wx.size, dtype=float)
            wy[0::2] = lo[mask]
            wy[1::2] = h[mask]
            self.kline_plot.plot(wx, wy, pen=pg.mkPen(color, width=1), connect="pairs")
            body = np.maximum(np.abs(c[mask] - o[mask]), min_body)
            self.kline_plot.addItem(
                pg.BarGraphItem(
                    x=xs[mask],
                    height=body,
                    y0=np.minimum(o[mask], c[mask]),
                    width=0.62,
                    brush=pg.mkBrush(color),
                    pen=pg.mkPen(color, width=1),
                )
            )
        for key, name, color in (
            ("ma20", "MA20", theme.C_ACCENT),
            ("ma50", "MA50", theme.C_AMBER),
            ("ma200", "MA200", theme.C_UP),
        ):
            vals = np.asarray(ph.get(key) or [], dtype=float)
            if len(vals) == len(xs) and np.isfinite(vals).any():
                self.kline_plot.plot(xs, vals, pen=pg.mkPen(color, width=1.2), name=name)
        lv = result.get("primary_levels") or {}
        for val, name, color in (
            (lv.get("tr_bottom"), "P&F下沿", theme.C_DOWN),
            (lv.get("tr_top"), "P&F上沿", theme.C_UP),
        ):
            try:
                y = float(val)
            except (TypeError, ValueError):
                continue
            self.kline_plot.addItem(
                pg.InfiniteLine(
                    pos=y,
                    angle=0,
                    pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                    label=name,
                    labelOpts={"position": 0.04, "color": color},
                )
            )
        snap = (result.get("indices") or {}).get(result.get("primary")) or {}
        name = snap.get("name") or "指数"
        self.kline_plot.getAxis("bottom").setTicks([self._x_ticks(dates)])
        self.kline_plot.setTitle(f"{name} K线（近 {len(xs)} 日）", color=theme.C_TEXT, size="10pt")

    def _render_analogs(self, result: dict):
        self.analog_plot.clear()
        legend = self.analog_plot.plotItem.legend
        if legend is not None:
            legend.clear()
        hs = (result.get("analogs") or {}).get("horizons") or {}
        series = []
        for h, color in (("5", theme.C_ACCENT), ("20", theme.C_AMBER), ("60", theme.C_UP)):
            st = hs.get(h) or {}
            vals = np.asarray(st.get("samples") or [], dtype=float)
            vals = vals[np.isfinite(vals)]
            if vals.size >= 5:
                series.append((h, vals, color))
        if not series:
            self.analog_plot.setTitle(
                "历史类比收益分布（无样本数据）", color=theme.C_TEXT, size="10pt"
            )
            return
        lo = min(v.min() for _, v, _ in series)
        hi = max(v.max() for _, v, _ in series)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        edges = np.linspace(lo, hi, 21)
        centers = (edges[:-1] + edges[1:]) / 2
        width = (edges[1] - edges[0]) * 0.9
        for h, vals, color in series:
            counts, _ = np.histogram(vals, bins=edges)
            c = QColor(color)
            c.setAlpha(110)
            self.analog_plot.addItem(
                pg.BarGraphItem(
                    x=centers,
                    height=counts,
                    width=width,
                    brush=pg.mkBrush(c),
                    pen=pg.mkPen(color),
                    name=f"{h}日",
                )
            )
        self.analog_plot.addItem(
            pg.InfiniteLine(
                pos=0.0,
                angle=90,
                pen=pg.mkPen(theme.C_MUTED, width=1, style=Qt.PenStyle.DashLine),
            )
        )
        self.analog_plot.getAxis("bottom").setLabel("未来收益 %")
        self.analog_plot.getAxis("left").setLabel("样本数")
        self.analog_plot.setTitle(
            "历史类比：未来收益分布（5/20/60日）", color=theme.C_TEXT, size="10pt"
        )

    # ── 日志 ────────────────────────────────────────────────
    def _load_journal_summary(self):
        data = load_journal()
        records = data.get("records") or []
        last = records[-1] if records else {}
        self.journal_info.setText(
            f"日志: {len(records)} 条   最近: {last.get('as_of', '--')}   "
            f"文件: {os.path.basename(JOURNAL_FILE)}"
        )
        lines = [
            "## 预测日志",
            "",
            f"- 记录数: **{len(records)}**",
            f"- 最近记录: {last.get('as_of', '--')} {last.get('name', '')} {last.get('state', '')}",
            "",
            "点上方 **记录今日 / 回填 / 统计** 查看。",
            "",
            "> 回填要等 5/20/60 个交易日后才有结果；样本少时别急着下结论。",
        ]
        self.journal_view.setMarkdown("\n".join(lines))

    def record_current(self):
        if not self.result or self.result.get("error"):
            QMessageBox.information(self, "提示", "先刷新报告，再记录。")
            return
        info = record_outlook(self.result)
        if info.get("ok"):
            QMessageBox.information(
                self,
                "已记录",
                f"已记录: {info.get('id')}\n"
                f"{'覆盖' if info.get('replaced') else '新增'}，"
                f"共 {info.get('total')} 条\n{info.get('path')}",
            )
            self._load_journal_summary()
        else:
            QMessageBox.warning(self, "记录失败", info.get("reason", "未知错误"))

    def record_stock_current(self):
        data = getattr(self, "_last_stock_data", {})
        if not data:
            QMessageBox.information(self, "提示", "先分析个股，再记录。")
            return
        info = record_stock(data, self.result)
        if info.get("ok"):
            QMessageBox.information(
                self,
                "已记录个股",
                f"已记录: {info.get('id')}\n"
                f"{'覆盖' if info.get('replaced') else '新增'}，"
                f"共 {info.get('total')} 条\n{info.get('path')}",
            )
            self._load_stock_journal_summary()
        else:
            QMessageBox.warning(self, "记录失败", info.get("reason", "未知错误"))

    def _load_stock_journal_summary(self):
        data = load_journal(STOCK_JOURNAL_FILE)
        records = data.get("records") or []
        last = records[-1] if records else {}
        self.stock_journal_info.setText(
            f"个股日志: {len(records)} 条   最近: {last.get('as_of', '--')}   "
            f"文件: {os.path.basename(STOCK_JOURNAL_FILE)}"
        )
        self.stock_journal_view.setMarkdown(
            "\n".join(
                [
                    "## 个股预测日志",
                    "",
                    f"- 记录数: **{len(records)}**",
                    f"- 最近记录: {last.get('as_of', '--')} "
                    f"{last.get('name', '')} {last.get('direction', '')}",
                    "",
                    "点上方 **回填个股 / 个股统计** 查看。",
                    "",
                    "> 回填要等 5/20/60 个交易日后才有结果；样本少时只看方向。",
                ]
            )
        )

    def run_backfill(self):
        worker = _BackfillWorker(self)
        self._start(worker, self._on_backfill, self._on_error, "正在回填...")

    def _on_backfill(self, summary: dict):
        self.journal_view.setMarkdown(render_backfill_summary(summary))
        self._load_journal_summary_keep_view()

    def run_stats(self):
        worker = _StatsWorker(20, self)
        self._start(worker, self._on_stats, self._on_error, "正在统计...")

    def _on_stats(self, stats: dict):
        self.journal_view.setMarkdown(render_stats(stats))
        self._load_journal_summary_keep_view()

    def _load_journal_summary_keep_view(self):
        data = load_journal()
        records = data.get("records") or []
        last = records[-1] if records else {}
        self.journal_info.setText(f"日志: {len(records)} 条   最近: {last.get('as_of', '--')}")

    def run_stock_backfill(self):
        worker = _StockBackfillWorker(self)
        self._start(worker, self._on_stock_backfill, self._on_error, "正在回填个股...")

    def _on_stock_backfill(self, summary: dict):
        self.stock_journal_view.setMarkdown(render_backfill_summary(summary, title="个股预测回填"))
        self._load_stock_journal_summary_keep_view()

    def run_stock_stats(self):
        worker = _StockStatsWorker(20, self)
        self._start(worker, self._on_stock_stats, self._on_error, "正在统计个股...")

    def _on_stock_stats(self, stats: dict):
        self.stock_journal_view.setMarkdown(render_stock_stats(stats))
        self._load_stock_journal_summary_keep_view()

    def _load_stock_journal_summary_keep_view(self):
        data = load_journal(STOCK_JOURNAL_FILE)
        records = data.get("records") or []
        last = records[-1] if records else {}
        self.stock_journal_info.setText(
            f"个股日志: {len(records)} 条   最近: {last.get('as_of', '--')}"
        )

    # ── 导出 ────────────────────────────────────────────────
    def export_report(self):
        if not self.result or self.result.get("error"):
            QMessageBox.information(self, "提示", "先刷新报告，再导出。")
            return
        default = f"ashare_outlook_{self.result.get('as_of') or time.strftime('%Y%m%d')}.md"
        path, selected = QFileDialog.getSaveFileName(
            self,
            "导出报告 / 图表",
            default,
            "Markdown (*.md);;PDF (*.pdf);;图表 PNG (*.png);;所有文件 (*)",
        )
        if not path:
            return
        suffix = os.path.splitext(path)[1].lower()
        if "PDF" in selected and suffix != ".pdf":
            path += ".pdf"
            suffix = ".pdf"
        elif "PNG" in selected and suffix != ".png":
            path += ".png"
            suffix = ".png"
        try:
            if suffix == ".pdf":
                from wyckoff.ashare_export import write_pdf

                write_pdf(render_report(self.result), path, title="A股市场状态报告")
            elif suffix == ".png":
                self.tabs.setCurrentWidget(self.chart_tab)
                self.chart_tab.repaint()
                pix = self.chart_tab.grab()
                if not pix.save(path, "PNG"):
                    raise RuntimeError("PNG 保存失败")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(render_report(self.result))
            QMessageBox.information(self, "已导出", path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(e))

    # ── 关闭 ────────────────────────────────────────────────
    def closeEvent(self, event):
        if any(w.isRunning() for w in self._workers):
            QMessageBox.information(self, "正在运行", "数据还在加载，请稍候再关闭。")
            event.ignore()
            return
        super().closeEvent(event)
