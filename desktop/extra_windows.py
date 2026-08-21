# -*- coding: utf-8 -*-
"""候选池 & 行业板块扫描窗口 (PyQt6)。

- CandidatesWindow: 待观察候选清单 (load_candidates), 双击加载分析
- SectorWindow: 全市场板块扫描 (scan_sectors), 双击板块展开成份股扫描
- ScanWindow: 批量威科夫信号扫描 (自选股 / 全市场)
- ScreenerWidget: 综合选股 (威科夫+基本面+资金流+技术 多维评分)
- NteamWindow: 国家队 ETF 跟踪 (track_nteam)
- HoldingsWindow: 国家队持仓透视 (fetch_nt_holdings)
- EtfMonitorWindow: ETF 三因子份额监测 (monitor_etfs)
"""
import os
import threading
import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from wyckoff.paths import DATA_DIR
from wyckoff.storage import (
    load_candidates, load_settings, load_watchlist, save_candidates,
    save_watchlist,
)

from . import theme

_SCAN_COL_CN = {"code": "代码", "name": "名称", "last": "现价", "phase": "阶段",
                "confirm": "确认", "signals": "信号", "score": "评分",
                "flow20": "20日主力(亿)", "sector": "板块", "sector20": "板块20日(亿)",
                "date": "时间", "price": "指数", "pct": "涨跌%", "tone": "方向",
                "total_score": "总分", "tech_score": "技术分",
                "flow_score": "资金分", "fund_score": "基本面分",
                "pe": "PE", "pb": "PB", "mcap_yi": "市值(亿)",
                # ── 高级扫描中心 ──
                "sig": "买点", "sig_price": "事件价", "pull": "回撤%",
                "break": "突破形态", "res": "压制位", "lead": "突破幅度%",
                "target": "目标位", "tgt_dist": "距目标%",
                "kind": "类型", "vr": "量比", "band": "位置%",
                "level": "关键位", "kp": "价位", "dist": "距离%", "wave": "波浪",
                "cost": "成本", "pnl": "盈亏%", "stop": "止损", "low60": "60日低",
                "broke": "破位", "advice": "建议",
                "origin": "原信号", "status": "状态", "near_hi": "距20日高%",
                "days": "加入天数",
                "sec_score": "板块分", "sec_flow": "板块资金(亿)",
                "times": "上榜次数", "net": "净买额(亿)", "inst_net": "机构净买(亿)",
                "pct_1m": "近1月涨跌%",
                "mrg_bal": "融资余额(亿)", "mrg_chg": "融资变动%",
                "mrg_delta": "净增(亿)", "sec_bal": "融券余额(亿)",
                "unlock_date": "解禁日", "value_yi": "解禁市值(亿)", "ratio": "占流通%",
                "type": "解禁类型", "pct20": "解禁前20日%",
"hold_chg": "持仓变动%", "market": "通道",
                 "ampl": "变动幅度%", "msg": "说明",
                 # ── 第二轮: 派发/平台/吸筹/大宗/调研/涨停/质押 ──
                 "chain": "吸筹链", "high20": "前高(20日)",
                 "premium": "折溢%", "amount_yi": "金额(亿)",
                 "inst_num": "机构数", "way": "方式",
                 "limit_times": "连板数", "open_cnt": "炸板次数",
                 "industry": "行业", "pct_y1": "近1年%"}


def _table(select_rows=True):
    t = QTableWidget()
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection
                       if select_rows else QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    # 统一行高: 避免逐行测量, 大批量数据渲染提速
    t.verticalHeader().setDefaultSectionSize(28)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    # 关闭排序时的自动 Resize (性能瓶颈), 手动控制列宽
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setStyleSheet(f"QTableWidget{{background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};}}"
                    f"QTableWidget::item:selected{{background:{theme.C['sel']};}}")
    return t


_NUM_COLS = {"total_score", "tech_score", "flow_score", "fund_score",
             "pe", "pb", "mcap_yi", "last", "score", "flow20", "sector20"}


def _fill(table, cols, rows, color_cols=()):
    # 批量更新: 禁用重绘, 避免逐行 setItem 触发重绘 (大批量数据提速 3-5x)
    table.setUpdatesEnabled(False)
    try:
        table.clear()
        table.setColumnCount(len(cols))
        table.setRowCount(len(rows))
        table.setHorizontalHeaderLabels([_SCAN_COL_CN.get(c, c) for c in cols])
        for ri, r in enumerate(rows):
            for ci, c in enumerate(cols):
                val = r.get(c, "") if isinstance(r, dict) else r[ci]
                # 数值列格式化显示
                if c in _NUM_COLS and isinstance(val, (int, float)) and val:
                    if c in ("pe",):
                        txt = f"{val:.1f}"
                    elif c in ("pb",):
                        txt = f"{val:.2f}"
                    elif c in ("mcap_yi", "flow20", "sector20"):
                        txt = f"{val:.0f}"
                    else:
                        txt = f"{val:.0f}"
                    it = QTableWidgetItem(txt)
                    it.setData(Qt.ItemDataRole.UserRole, val)
                else:
                    txt = str(val) if val else "-"
                    it = QTableWidgetItem(txt)
                    # 尝试将字符串数值用于排序
                    if c in _NUM_COLS and val:
                        try:
                            it.setData(Qt.ItemDataRole.UserRole, float(str(val).replace(",", "")))
                        except (ValueError, TypeError):
                            pass
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter if ci == 0
                                    else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(ri, ci, it)
            if isinstance(r, dict):
                for c in color_cols:
                    if c in cols:
                        ci = cols.index(c)
                        it = table.item(ri, ci)
                        if it is not None:
                            it.setForeground(_color_for(r))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    finally:
        table.setUpdatesEnabled(True)


def _color_for(r):
    from PyQt6.QtGui import QColor
    conf = r.get("conf_q")
    if conf == "high":
        return QColor(theme.C_ACCENT)
    if conf == "caution":
        return QColor(theme.C_AMBER)
    return QColor(theme.C_TEXT)


def _holdings_ai_prompt(symbol, holders, latest_report, exited):
    """构造国家队持仓透视的 AI 解读 prompt。"""
    if not holders:
        return ("国家队持仓透视 (暂无数据): 该股前十大股东中未识别出国家队机构, "
                "无需 AI 解读。")
    def _fmt(h):
        cost = f"{h.get('cost'):.2f}" if h.get("cost") else "未知"
        price = f"{h.get('price'):.2f}" if h.get("price") else "未知"
        pnl = f"{h.get('pnl_pct'):+.1f}%" if h.get("pnl_pct") is not None else "未知"
        chg = ("新进" if h["status"] == "新进" else
               (f"较上期 {h['change_ratio']:+.2f}%" if h.get("change_ratio") is not None else "持平"))
        return (f"- [{h['category']}] {h['name']}: {chg}, 持股占比 "
                f"{h.get('pct')}%, 估算成本 {cost}, 现价 {price}, 盈亏 {pnl}, "
                f"建仓于 {h['first_report']}")
    detail = "\n".join(_fmt(h) for h in holders)
    exit_txt = ""
    if exited:
        exit_txt = "\n已退出/跌出前十:\n" + "\n".join(
            f"- [{e['category']}] {e['name']} (最近报表期 {e['last_report']})"
            for e in exited)
    return f"""你是一名资深 A股研究员。请基于以下『国家队(汇金/证金/社保/养老/外管局)持仓透视』数据,
给普通投资者写一段通俗、有依据的解读。

# 数据 (最新报表期 {latest_report or '未知'}, 代码 {symbol})
{detail}
{exit_txt}

# 解读要求
- 先给一句话结论: 国家队对该股的参与程度 (重仓/轻仓/持续加仓/开始撤退等)。
- 然后解读: 机构类型构成 (汇金/社保/养老等谁更积极)、增减持动作含义、
  估算成本 vs 现价的浮盈浮亏暗示什么、以及已退出机构的含义。
- 提醒局限: 这是季报滞后数据, 只反映披露时点, 且持股跌出前十即"失联";
  成本为估算, 未计分红与高抛低吸。
- 结尾给 A股可执行参考 (如"随社保加仓关注, 跌破其估算成本线需警惕"), 注明
  "仅供参考, 不构成投资建议"。
- 自然连贯段落, 200~400 字, 不要标题/序号/markdown。【铁律】A股不能做空,
  任何建议只落在 关注/低吸/持有/减仓/回避 等动作上。"""


def _etf_ai_prompt(results):
    """构造 ETF 三因子份额监测的 AI 解读 prompt。"""
    if not results:
        return "ETF 三因子监测 (暂无数据), 无需 AI 解读。"
    alert = [r for r in results if r["signal"] != "正常" and r["signal"] != "数据不足"]
    def _fmt(r):
        return (f"- {r['name']}({r['symbol']}): 信号『{r['signal']}』, 强度 "
                f"{r.get('strength'):.2f}, 量比 {r.get('vol_ratio')}, "
                f"份额1日 {r.get('share_1d'):+.2f}%, 份额5日 "
                f"{r.get('share_5d'):+.2f}%, ETF近5日 {r.get('etf_ret5'):+.2f}%, "
                f"基准近5日 {r.get('bench_ret5'):+.2f}%")
    focus = "\n".join(_fmt(r) for r in alert) if alert else "无异常信号 (全部正常)"
    normal_n = sum(1 for r in results if r["signal"] == "正常")
    return f"""你是一名资深 A股市场研究员, 请基于以下『ETF 三因子份额监测』数据
(量能50% + 方向20% + 份额30%, 用于推测汇金等国家队在宽基 ETF 上的加仓/减仓信号),
给普通投资者写一段通俗解读。

# 数据 (共 {len(results)} 只宽基 ETF, 异常 {len(alert)} 只, 正常 {normal_n} 只)
{focus}

# 解读要求
- 先给一句话总览: 国家队在宽基 ETF 上是整体加仓、减仓还是按兵不动 (基于份额变化与
  护盘特征推断)。
- 重点解读出现『高确信买入/卖出』或『中等关注』的 ETF: 份额增减 + 量能放大 +
  逆势走强的组合说明什么。
- 必须强调: ETF 份额增加≠一定是国家队买入 (可能是机构/散户申购), 属概率性信号。
- 结尾给 A股可执行参考 (如"关注沪深300/上证50 ETF 份额连续放量增长")并注明
  "仅供参考, 不构成投资建议"。
- 自然连贯段落, 200~400 字, 不要标题/序号/markdown。
- 【铁律】A股不能做空: 任何建议只落在 关注/低吸/持有/减仓/回避 等动作上,
  严禁出现 做空/放空/开空仓/空头回补 等做空指令。"""


def _selected_codes(table, col=0):
    codes = []
    for idx in sorted({i.row() for i in table.selectedIndexes()}):
        it = table.item(idx, col)
        if it is not None and it.text():
            codes.append(it.text())
    return codes


def _accent_header(text):
    """带 accent 饰条的窗口标题条。"""
    box = QWidget()
    h = QHBoxLayout(box)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    strip = QLabel()
    strip.setFixedSize(5, 16)
    strip.setStyleSheet(f"background:{theme.C_ACCENT};border-radius:2px;")
    h.addWidget(strip)
    lab = QLabel(text)
    lab.setStyleSheet("font-weight:bold;font-size:12pt;")
    h.addWidget(lab)
    h.addStretch(1)
    return box


class _AiPanel(QWidget):
    """通用 AI 解读面板: 标题 + 解读区 + 生成/刷新按钮。

    右侧栏复用: 国家队持仓透视 / ETF 三因子监测 等工具窗口。
    run() 传入 (prompt, settings), 后台线程调用 interpret_prompt 并回填。
    """

    def __init__(self, title="AI 解读", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        strip = QLabel()
        strip.setFixedSize(4, 14)
        strip.setStyleSheet(f"background:{theme.C_ACCENT};border-radius:2px;")
        head.addWidget(strip)
        lab = QLabel(title)
        lab.setStyleSheet("font-weight:bold;")
        head.addWidget(lab)
        head.addStretch(1)
        self.btn_tts = QPushButton("▶ 语音朗读")
        self.btn_tts.setToolTip("朗读下方 AI 解读内容")
        self.btn_tts.clicked.connect(self._on_tts_click)
        head.addWidget(self.btn_tts)
        self.btn = QPushButton("生成 AI 解读")
        self.btn.clicked.connect(self._on_click)
        head.addWidget(self.btn)
        lay.addLayout(head)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setPlaceholderText(
            "点击『生成 AI 解读』, 由大模型基于当前窗口数据给出通俗解读。\n"
            "(需在 设置→AI 中启用 AI 解读并配置 API Key)")
        lay.addWidget(self.txt, 1)

        self._th = None
        self._tts_playing = False

    def _on_tts_click(self):
        """朗读本面板的 AI 解读文本 (与 main_window 的 TTS 逻辑一致)。"""
        from wyckoff.tts import is_enabled, speak, stop
        settings = load_settings()
        if self._tts_playing:
            stop()
            self._tts_playing = False
            self.btn_tts.setText("▶ 语音朗读")
            return
        if not is_enabled(settings):
            self.btn_tts.setText("▶ 语音朗读")
            self.txt.setPlainText(
                "语音朗读未启用: 请在 设置→语音播报 中启用并配置引擎。")
            return
        text = (self.txt.toPlainText() or "").strip()
        if not text or text.startswith("正在请求") or "不可用" in text:
            self.txt.setPlainText("暂无可朗读的 AI 解读, 请先生成解读内容。")
            return
        self._tts_playing = True
        self.btn_tts.setText("■ 停止")
        ok = speak(text, settings, on_done=self._on_tts_done)
        if not ok:
            self._tts_playing = False
            self.btn_tts.setText("▶ 语音朗读")

    def _on_tts_done(self, ok, err):
        self._tts_playing = False
        self.btn_tts.setText("▶ 语音朗读")

    def _on_click(self):
        if self._th is not None and self._th.isRunning():
            return
        prompt = self._prompt_fn() if hasattr(self, "_prompt_fn") else ""
        if not prompt:
            self.txt.setPlainText("暂无可解读的数据 (数据尚未加载完成)")
            return
        self.btn.setEnabled(False)
        self.btn.setText("解读中...")
        self.txt.setPlainText("正在请求 AI 解读...")
        self._th = _AiInterpretThread(prompt, load_settings(), self)
        self._th.finished.connect(self._on_done)
        self._th.start()

    def _on_done(self, text):
        self.btn.setEnabled(True)
        self.btn.setText("生成 AI 解读")
        if text:
            self.txt.setPlainText(text)
        else:
            self.txt.setPlainText(
                "AI 解读不可用: 未启用 AI 解读 或未配置 API Key / 模型调用失败。\n"
                "请在 设置→AI 中启用『AI 解读』并填入 DeepSeek/OpenAI 兼容 API Key。")

    def bind_prompt(self, fn):
        """绑定 prompt 构造函数: fn() -> str (每次点击时调用, 取最新数据)。"""
        self._prompt_fn = fn

    def clear(self):
        self.txt.clear()


class _AiInterpretThread(QThread):
    finished = pyqtSignal(object)

    def __init__(self, prompt, settings, parent=None):
        super().__init__(parent)
        self._prompt = prompt
        self._settings = settings

    def run(self):
        try:
            from wyckoff.interpret import interpret_prompt
            # 用户显式点击 → 只要有 Key 即可调用, 不依赖自动解读开关
            out = interpret_prompt(self._prompt, self._settings, min_len=80,
                                   require_enabled=False)
        except Exception:
            out = None
        self.finished.emit(out)


class CandidatesWindow(QDialog):
    """待观察清单: [{code, name, score, phase, conf_q, signals, sector, sector20, date}]"""

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("待观察清单")
        self.resize(920, 460)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("待观察清单"))
        self.head = QLabel()
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        for text, cb in (("加入自选股", self._add_watch), ("删除", self._delete),
                         ("导出CSV", self._export)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)
        self._empty = QLabel()
        self._empty.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self._empty)

    def refresh(self):
        recs = load_candidates()
        self.head.setText(f"待观察清单 · 共 {len(recs)} 条 (双击加载分析)")
        cols = ("code", "name", "score", "phase", "confirm", "signals", "sector", "date")
        self._recs = recs
        _fill(self.table, cols, recs, color_cols=("confirm",))
        self._empty.setText("" if recs else "(暂无, 在板块扫描结果里'存为待观察')")

    def _load_selected(self, _item):
        codes = _selected_codes(self.table)
        if codes and self.on_load:
            self.on_load(codes[0])

    def _add_watch(self):
        codes = load_watchlist()
        added = [c for c in _selected_codes(self.table) if c and c not in codes]
        if added:
            save_watchlist(codes + added)
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
            if getattr(self.parent(), "reload_watchlist", None):
                self.parent().reload_watchlist()
        else:
            QMessageBox.information(self, "加入自选股", "未选中可添加的股票")

    def _delete(self):
        codes = _selected_codes(self.table)
        if not codes:
            return
        recs = load_candidates()
        keep = [r for r in recs if r.get("code") not in codes]
        save_candidates(keep)
        self.refresh()

    def _export(self):
        recs = load_candidates()
        if not recs:
            QMessageBox.information(self, "导出", "待观察清单为空")
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_candidates_{ts}.csv")
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "评分", "阶段", "确认", "信号",
                        "板块", "板块20日(亿)", "保存时间"])
            for r in recs:
                w.writerow([
                    r.get("code", ""), r.get("name", ""), r.get("score", 0),
                    r.get("phase", ""),
                    {"high": "高置信", "caution": "需谨慎"}.get(r.get("conf_q"), ""),
                    r.get("signals", ""), r.get("sector", "") or "",
                    f"{r['sector20']:.2f}" if r.get("sector20") is not None else "",
                    r.get("date", "")])
        QMessageBox.information(self, "导出完成", f"待观察清单已导出:\n{path}")


class _SectorScanThread(QThread):
    stock_done = pyqtSignal(int, int)
    finished = pyqtSignal(object)

    def __init__(self, bk_code, board_name, limit, confirm_enabled, parent=None):
        super().__init__(parent)
        self._bk = bk_code
        self._name = board_name
        self._limit = limit
        self._confirm = confirm_enabled

    def run(self):
        from wyckoff.backtest import scan_sector_stocks
        done = {"n": 0}

        def on_result(_r):
            done["n"] += 1
            self.stock_done.emit(done["n"], self._limit)

        try:
            results = scan_sector_stocks(self._bk, self._name, limit=self._limit,
                                         confirm_enabled=self._confirm,
                                         on_result=on_result)
        except Exception as e:
            results = []
            import traceback
            traceback.print_exc()
        self.finished.emit(results)


class SectorDetailWindow(QDialog):
    def __init__(self, sector, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.sector = sector
        self.setWindowTitle(f"{sector['name']} · 成份股扫描")
        self.resize(1120, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header(f"{sector['name']} · 成份股扫描"))

        self.head = QLabel(f"正在扫描 {sector['name']} 成份股 ...")
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)
        self.prog = QLabel("")
        self.prog.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self.prog)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        hb = QHBoxLayout()
        self.btn_save = QPushButton("存为待观察")
        self.btn_save.clicked.connect(self._save_candidates)
        self.btn_save.setEnabled(False)
        hb.addWidget(self.btn_save)
        self.btn_add = QPushButton("加入自选股")
        self.btn_add.clicked.connect(self._add_watch)
        self.btn_add.setEnabled(False)
        hb.addWidget(self.btn_add)
        self.btn_export = QPushButton("导出CSV")
        self.btn_export.clicked.connect(self._export)
        self.btn_export.setEnabled(False)
        hb.addWidget(self.btn_export)
        hb.addStretch(1)
        root.addLayout(hb)

        self._results = []
        self._thread = _SectorScanThread(sector.get("bk_code", ""), sector["name"], 30,
                                         True, self)
        self._thread.stock_done.connect(self._on_progress)
        self._thread.finished.connect(self._on_done)
        self._thread.start()

    def _on_progress(self, n, total):
        self.prog.setText(f"已扫描 {n}/{total} ...")

    def _on_done(self, results):
        self._results = results
        self.head.setText(f"{self.sector['name']} · 成份股扫描 · 共 {len(results)} 只 (双击加载分析)")
        self.prog.setText("")
        cols = ("code", "name", "last", "phase", "confirm", "signals", "score", "flow20")
        _fill(self.table, cols, results, color_cols=("confirm",))
        self.btn_save.setEnabled(True)
        self.btn_add.setEnabled(True)
        self.btn_export.setEnabled(True)
        for c in ("code", "score", "last"):
            if c in cols:
                idx = cols.index(c)
                self.table.horizontalHeader().resizeSection(idx, 90)
        if not results:
            QMessageBox.warning(self, "扫描", "未获取到该板块成份股 (可能实时源不可用)")

    def _load_selected(self, _item):
        codes = _selected_codes(self.table)
        if codes and self.on_load:
            self.on_load(codes[0])

    def _save_candidates(self):
        codes = _selected_codes(self.table)
        if not codes:
            QMessageBox.information(self, "存为待观察", "请先勾选要观察的行")
            return
        recs = load_candidates()
        have = {r["code"] for r in recs}
        new = 0
        for r in self._results:
            if r.get("code") in codes:
                if r["code"] in have:
                    recs = [x for x in recs if x["code"] != r["code"]]
                from datetime import datetime
                recs.insert(0, {"code": r["code"], "name": r.get("name", ""),
                                "score": r.get("score", 0), "phase": r.get("phase", ""),
                                "conf_q": r.get("conf_q", ""),
                                "signals": "+".join(r.get("signals") or []),
                                "sector": r.get("sector", ""),
                                "sector20": r.get("sector20"),
                                "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
                new += 1
        save_candidates(recs)
        QMessageBox.information(self, "存为待观察", f"已存 {new} 只到待观察清单")

    def _add_watch(self):
        codes = load_watchlist()
        added = [c for c in _selected_codes(self.table) if c and c not in codes]
        if added:
            save_watchlist(codes + added)
            if getattr(self.parent(), "reload_watchlist", None):
                self.parent().reload_watchlist()
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "未选中可添加的股票")

    def _export(self):
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_sector_{self.sector['name']}_{ts}.csv")
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "现价", "阶段", "确认", "信号", "评分",
                        "20日主力(亿)"])
            for r in self._results:
                w.writerow([r.get("code", ""), r.get("name", ""), r.get("last", ""),
                            r.get("phase", ""), r.get("conf_q", ""),
                            "+".join(r.get("signals") or []), r.get("score", 0),
                            f"{r['flow20']:.2f}" if r.get("flow20") is not None else ""])
        QMessageBox.information(self, "导出完成", f"已导出到:\n{path}")


class SectorWindow(QDialog):
    """全市场板块扫描窗口。双击板块打开成份股扫描。"""

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("板块扫描")
        self.resize(1060, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("板块扫描"))
        self.head = QLabel("正在获取全市场板块数据 ...")
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)
        self.table = _table(select_rows=False)
        self.table.itemDoubleClicked.connect(self._open_detail)
        root.addWidget(self.table, 1)
        hb = QHBoxLayout()
        btn = QPushButton("导出CSV")
        btn.clicked.connect(self._export)
        hb.addWidget(btn)
        hb.addStretch(1)
        root.addLayout(hb)
        self._sectors = []
        self._refresh_thread = None
        self._start_scan()

    def refresh(self):
        self._start_scan()

    def _start_scan(self):
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        self.head.setText("正在获取全市场板块数据 ...")
        self._refresh_thread = _SectorListThread(self)
        self._refresh_thread.finished.connect(self._on_scan_done)
        self._refresh_thread.start()

    def _on_scan_done(self, sectors):
        self._sectors = sectors
        live = [s for s in sectors if s.get("live", True)]
        offline = len(sectors) - len(live)
        txt = f"共 {len(sectors)} 个行业板块"
        if live:
            txt += (f" · 主力流入 {sum(1 for s in live if s.get('flow20_yi', 0) > 0)} / "
                    f"流出 {sum(1 for s in live if s.get('flow20_yi', 0) < 0)} 个")
        if offline:
            txt += f" · {offline} 个板块实时数据暂不可用"
        txt += " · 双击板块查看成份股"
        self.head.setText(txt)
        cols = ("name", "price", "pct", "flow20", "tone", "score")
        tone_cn = {"bullish": "偏多", "bearish": "偏空", "mixed": "分化", "neutral": "离线"}
        rows = []
        for s in sectors:
            live_s = s.get("live", True)
            rows.append({
                "name": s["name"],
                "price": f"{s['price']:.2f}" if live_s else "-",
                "pct": f"{s['pct']:+.2f}%" if live_s else "-",
                "flow20": f"{'+' if s.get('flow20_yi', 0) >= 0 else ''}{s.get('flow20_yi', 0):.2f}"
                          if live_s else "-",
                "tone": tone_cn.get(s["tone"], s["tone"]),
                "score": str(s["score"]) if live_s else "-",
            })
        _fill(self.table, cols, rows)
        from PyQt6.QtGui import QColor
        tone_color = {"bullish": theme.C_UP, "bearish": theme.C_DOWN, "mixed": theme.C_AMBER}
        for ri, s in enumerate(sectors):
            if s.get("tone") in tone_color:
                it = self.table.item(ri, cols.index("tone"))
                if it is not None:
                    it.setForeground(QColor(tone_color[s["tone"]]))

    def _open_detail(self, _item):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        idx = sorted(rows)[0]
        if idx >= len(self._sectors):
            return
        sector = self._sectors[idx]
        if not sector.get("bk_code"):
            QMessageBox.information(self, "板块", f"{sector['name']}: 暂不支持查看成份股 (无板块码映射)")
            return
        dlg = SectorDetailWindow(sector, self, on_load=self.on_load)
        dlg.exec()

    def _export(self):
        if not self._sectors:
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_sectors_{ts}.csv")
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["板块", "指数", "涨跌%", "20日主力(亿)", "方向", "评分"])
            for s in self._sectors:
                w.writerow([s["name"], s.get("price", ""), s.get("pct", ""),
                            s.get("flow20_yi", ""), s.get("tone", ""), s.get("score", "")])
        QMessageBox.information(self, "导出完成", f"已导出到:\n{path}")


class _SectorListThread(QThread):
    finished = pyqtSignal(object)

    def run(self):
        from wyckoff.backtest import scan_sectors
        try:
            sectors = scan_sectors()
        except Exception:
            sectors = []
        self.finished.emit(sectors)


class _ScanThread(QThread):
    batch = pyqtSignal(object, int, int)
    finished = pyqtSignal()

    def __init__(self, codes, confirm_enabled, parent=None):
        super().__init__(parent)
        self._codes = codes
        self._confirm = confirm_enabled

    def run(self):
        from wyckoff.backtest import (
            reset_scan_confirm, scan_stock_signals, signal_score,
        )
        reset_scan_confirm()
        total = len(self._codes)
        batch = []
        for i, c in enumerate(self._codes):
            try:
                r = scan_stock_signals(c, confirm_enabled=self._confirm)
                if r:
                    r["score"] = signal_score(r)
                    batch.append(r)
            except Exception:
                import traceback
                traceback.print_exc()
            if len(batch) >= 5 or i == total - 1:
                b = batch
                batch = []
                self.batch.emit(b, i + 1, total)
        self.finished.emit()


class ScanWindow(QDialog):
    """批量威科夫信号扫描窗口 (自选股 / 全市场共用)。"""

    COL_CN = {"code": "代码", "name": "名称", "last": "现价", "phase": "阶段",
              "pe": "PE", "confirm": "确认", "flow20": "主力20", "sector": "板块",
              "score": "评分", "signals": "信号"}

    def __init__(self, codes, title, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle(title)
        self.resize(1120, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header(title))

        self.head = QLabel("正在扫描 ...")
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)
        self.prog = QLabel("")
        self.prog.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self.prog)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        hb = QHBoxLayout()
        for text, cb in (("全选", self._select_all), ("加入自选股", self._add_watch),
                         ("存为待观察", self._save_candidates), ("导出CSV", self._export)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            hb.addWidget(b)
        hb.addStretch(1)
        root.addLayout(hb)

        self._results = []
        self._thread = _ScanThread(codes, True, self)
        self._thread.batch.connect(self._on_batch)
        self._thread.finished.connect(self._on_done)
        self._thread.start()

    def _on_batch(self, batch, done, total):
        self._results.extend(batch)
        self.prog.setText(f"已扫描 {done}/{total} ...")
        self._fill_table()
        if self.table.rowCount() == 1:
            self.table.scrollToBottom()

    def _on_done(self):
        self.head.setText(f"扫描完成 · 共 {len(self._results)} 只有信号 (双击加载分析)")
        self.prog.setText("")
        self._fill_table()
        if not self._results:
            QMessageBox.information(self, "扫描", "未扫描到任何信号")

    def _fill_table(self):
        cols = ("code", "name", "last", "phase", "pe", "confirm", "flow20",
                "sector", "score", "signals")
        rows = []
        for r in self._results:
            sig = "+".join(r["signals"]) if r.get("signals") else "-"
            conf = {"high": "高置信", "caution": "需谨慎"}.get(r.get("conf_q"), "-")
            f20 = r.get("flow20")
            flow_txt = f"{f20:+.1f}亿" if f20 is not None else "-"
            pe = f"{r['pe']:.1f}" if r.get("pe") and r["pe"] > 0 else "-"
            sec = r.get("sector")
            sec_txt = f"{sec} {r['sector20']:+.1f}亿" if sec and r.get("sector20") is not None else (sec or "-")
            rows.append({
                "code": r["code"], "name": r.get("name", ""),
                "last": f"{r['last']:.2f}" if r.get("last") is not None else "-",
                "phase": r.get("phase", ""), "pe": pe, "confirm": conf,
                "flow20": flow_txt, "sector": sec_txt, "score": str(r.get("score", 0)),
                "signals": sig,
            })
        self.table.setSortingEnabled(False)
        _fill(self.table, cols, rows, color_cols=("confirm",))
        for c, w in (("code", 70), ("name", 100), ("last", 60), ("phase", 110),
                     ("pe", 50), ("confirm", 70), ("flow20", 80), ("sector", 130),
                     ("score", 50), ("signals", 180)):
            if c in cols:
                self.table.horizontalHeader().resizeSection(cols.index(c), w)

    def _selected_rows(self):
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _load_selected(self, _item):
        rows = self._selected_rows()
        if rows and rows[0] < len(self._results) and self.on_load:
            self.on_load(self._results[rows[0]]["code"])

    def _select_all(self):
        self.table.selectAll()

    def _add_watch(self):
        rows = self._selected_rows()
        codes = [self._results[i]["code"] for i in rows if i < len(self._results)]
        if not codes:
            QMessageBox.information(self, "加入自选股", "请先选择要加入的行")
            return
        watch = load_watchlist()
        added = [c for c in codes if c and c not in watch]
        if added:
            save_watchlist(watch + added)
            if getattr(self.parent(), "reload_watchlist", None):
                self.parent().reload_watchlist()
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "所选股票已在自选股中")

    def _save_candidates(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "存为待观察", "请先选择要观察的行")
            return
        from datetime import datetime
        recs = load_candidates()
        have = {r["code"] for r in recs}
        new = 0
        for i in rows:
            if i >= len(self._results):
                continue
            r = self._results[i]
            if r["code"] in have:
                recs = [x for x in recs if x["code"] != r["code"]]
            recs.insert(0, {"code": r["code"], "name": r.get("name", ""),
                            "score": r.get("score", 0), "phase": r.get("phase", ""),
                            "conf_q": r.get("conf_q", ""),
                            "signals": "+".join(r.get("signals") or []),
                            "sector": r.get("sector", ""),
                            "sector20": r.get("sector20"),
                            "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
            new += 1
        save_candidates(recs)
        QMessageBox.information(self, "存为待观察", f"已存 {new} 只到待观察清单")

    def _export(self):
        if not self._results:
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_scan_{ts}.csv")
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "现价", "阶段", "PE", "确认", "主力20(亿)",
                        "板块", "板块20日(亿)", "评分", "信号"])
            for r in self._results:
                w.writerow([r.get("code", ""), r.get("name", ""), r.get("last", ""),
                            r.get("phase", ""), r.get("pe", ""),
                            {"high": "高置信", "caution": "需谨慎"}.get(r.get("conf_q"), ""),
                            r.get("flow20", ""), r.get("sector", ""),
                            r.get("sector20", ""), r.get("score", 0),
                            "+".join(r.get("signals") or [])])
        QMessageBox.information(self, "导出完成", f"已导出到:\n{path}")


# ── 高级扫描中心 ──

# 数字列格式: "p"=价格两位小数, "pct1"=百分比一位小数, "yi"=亿两位, "int"=整数
_SCAN_NUM_FMT = {
    "last": "p", "sig_price": "p", "res": "p", "target": "p", "kp": "p",
    "cost": "p", "stop": "p", "low60": "p",
    "pull": "pct1", "lead": "pct1", "tgt_dist": "pct1", "dist": "pct1",
    "pct": "pct1", "band": "int", "vr": "pct1", "score": "int",
    "pnl": "pct1", "near_hi": "pct1", "pct20": "pct1", "ampl": "pct1",
    "sec_score": "int", "times": "int", "days": "int",
    "sec_flow": "yi", "net": "yi", "inst_net": "yi", "mrg_bal": "yi",
    "mrg_delta": "yi", "sec_bal": "yi", "value_yi": "yi",
    "mrg_chg": "pct1", "ratio": "pct1", "pct_1m": "pct1", "hold_chg": "pct1",
    "premium": "pct1", "inst_num": "int", "limit_times": "int", "open_cnt": "int",
    "high20": "p", "pct_y1": "pct1", "market_value": "yi", "amount_yi": "yi",
}


def _fmt_cell(col, val):
    fmt = _SCAN_NUM_FMT.get(col)
    try:
        if fmt == "p":
            return f"{val:.2f}"
        if fmt == "pct1":
            return f"{val:.1f}"
        if fmt == "yi":
            return f"{val:.2f}"
        if fmt == "int":
            return f"{val:.0f}"
    except (TypeError, ValueError):
        pass
    return None


class _AdvScanThread(QThread):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, scan_key, codes, parent=None):
        super().__init__(parent)
        self.key = scan_key
        self.codes = codes
        self.cancel = threading.Event()

    def run(self):
        from wyckoff.scan_adv import run_scan
        try:
            rows = run_scan(self.key, codes=self.codes,
                            workers=6, cancel_event=self.cancel)
            self.finished.emit(rows or [])
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")


class ScanCenterWindow(QDialog):
    """扫描中心: 汇聚 13 类专项扫描 (回踩买点/P&F突破/量能异动/…/北向)。"""

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("扫描中心")
        self.resize(1240, 600)

        from wyckoff.scan_adv import SCAN_REGISTRY
        self._registry = list(SCAN_REGISTRY)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("扫描中心"))

        # 控制栏: 类型 + 范围 + 开始/停止 + 说明
        ctrl = QHBoxLayout()
        self.cmb_type = QComboBox()
        for s in self._registry:
            self.cmb_type.addItem(f"{s['title']} — {s['desc']}", s["key"])
        self.cmb_type.currentIndexChanged.connect(self._on_type_changed)
        ctrl.addWidget(QLabel("扫描类型:"))
        ctrl.addWidget(self.cmb_type, 1)

        self.cmb_uni = QComboBox()
        self.cmb_uni.addItem("自选股", "watch")
        self.cmb_uni.addItem("全市场活跃股", "market")
        ctrl.addWidget(QLabel("范围:"))
        ctrl.addWidget(self.cmb_uni)

        self.btn_start = QPushButton("开始扫描")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        root.addLayout(ctrl)

        self.head = QLabel("")
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)
        self.prog = QLabel("")
        self.prog.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self.prog)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        hb = QHBoxLayout()
        for text, cb in (("全选", self._select_all), ("加入自选股", self._add_watch),
                         ("存为待观察", self._save_candidates), ("导出CSV", self._export_csv)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            hb.addWidget(b)
        hb.addStretch(1)
        root.addLayout(hb)

        self._thread = None
        self._results = []
        self._on_type_changed()

    # ── 交互 ──
    def _on_type_changed(self):
        idx = self.cmb_type.currentIndex()
        if idx < 0:
            return
        s = self._registry[idx]
        self.cmb_uni.setVisible(s.get("need_universe", True))
        self.head.setText("")
        self.prog.setText(s.get("desc", ""))
        self.table.clear()

    @staticmethod
    def _universe():
        from wyckoff.storage import load_watchlist
        codes = load_watchlist()
        return codes

    @staticmethod
    def _market_codes():
        from wyckoff.scan_adv import _market_codes
        return _market_codes()

    def _start(self):
        if self._thread is not None and self._thread.isRunning():
            return
        idx = self.cmb_type.currentIndex()
        s = self._registry[idx]
        key = self.cmb_type.itemData(idx)
        codes = None
        if s.get("need_universe", True):
            uni = self.cmb_uni.currentData()
            codes = self._watch_codes() if uni == "watch" else self._market_codes()
            if not codes:
                QMessageBox.information(self, "扫描中心", "扫描范围为空 (先添加自选股, 或稍后重试全市场)")
                return
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._results = []
        self.head.setText(f"运行中: {s['title']} ...")
        self._thread = _AdvScanThread(key, codes, self)
        self._thread.finished.connect(self._on_done)
        self._thread.failed.connect(self._on_fail)
        self._thread.start()

    def _watch_codes(self):
        from wyckoff.storage import load_watchlist
        return load_watchlist()

    def _stop(self):
        if self._thread is not None:
            self._thread.cancel.set()
            self.prog.setText("正在停止 (完成在运行项后停留) ...")

    def _on_fail(self, msg):
        self._reset_buttons()
        self.head.setText("扫描失败")
        self.prog.setText(msg)
        QMessageBox.warning(self, "扫描中心", f"扫描失败:\n{msg}")

    def _on_done(self, rows):
        self._reset_buttons()
        self._results = rows
        self.head.setText(f"扫描完成 · 共 {len(rows)} 条结果 (双击加载分析)")
        self.prog.setText("")
        self._fill(rows)
        if not rows:
            QMessageBox.information(self, "扫描中心", "本次扫描无结果, 可能条件过严或数据源不可用")

    def _reset_buttons(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _cols(self):
        from wyckoff.scan_adv import SCAN_COLUMNS
        idx = self.cmb_type.currentIndex()
        key = self.cmb_type.itemData(idx)
        base = ["code", "name", "last"]
        extra = list(SCAN_COLUMNS.get(key, ["msg"]))
        out = []
        seen = set()
        for c in base + extra:
            if c not in seen:
                out.append(c)
                seen.add(c)
        return out

    def _fill(self, rows):
        cols = self._cols()
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels([_SCAN_COL_CN.get(c, c) for c in cols])
        for ri, r in enumerate(rows):
            for ci, c in enumerate(cols):
                val = r.get(c, "")
                if c in _SCAN_NUM_FMT and isinstance(val, (int, float)) and val is not None:
                    val = _fmt_cell(c, val)
                it = QTableWidgetItem(str(val) if val else "-")
                it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(ri, ci, it)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c, w in (("code", 70), ("name", 100), ("last", 66), ("msg", 320), ("score", 56)):
            if c in cols:
                self.table.horizontalHeader().resizeSection(cols.index(c), w)

    def _selected_rows(self):
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _load_selected(self, _item):
        rows = self._selected_rows()
        if rows and rows[0] < len(self._results) and self.on_load:
            code = self._results[rows[0]].get("code")
            if code:
                self.on_load(code)

    def _select_all(self):
        self.table.selectAll()

    def _add_watch(self):
        rows = self._selected_rows()
        codes = [self._results[i]["code"] for i in rows if i < len(self._results)
                 and self._results[i].get("code")]
        if not codes:
            QMessageBox.information(self, "加入自选股", "请先选择要加入的行")
            return
        watch = load_watchlist()
        added = [c for c in codes if c and c not in watch]
        if added:
            save_watchlist(watch + added)
            if getattr(self.parent(), "reload_watchlist", None):
                self.parent().reload_watchlist()
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "所选股票已在自选股中")

    def _save_candidates(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "存为待观察", "请先选择要观察的行")
            return
        from datetime import datetime
        recs = load_candidates()
        have = {r["code"] for r in recs}
        new = 0
        for i in rows:
            if i >= len(self._results):
                continue
            r = self._results[i]
            code = r.get("code")
            if not code:
                continue
            recs = [x for x in recs if x["code"] != code]
            recs.insert(0, {"code": code, "name": r.get("name", ""),
                            "score": r.get("score", 0),
                            "phase": r.get("phase", ""),
                            "conf_q": "",
                            "signals": str(r.get("sig") or r.get("kind")
                                           or r.get("status") or r.get("msg") or ""),
                            "sector": r.get("sector", ""),
                            "sector20": None,
                            "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
            new += 1
        save_candidates(recs)
        QMessageBox.information(self, "存为待观察", f"已存 {new} 只到待观察清单")

    def _export_csv(self):
        if not self._results:
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_scancenter_{ts}.csv")
        import csv
        cols = self._cols()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([_SCAN_COL_CN.get(c, c) for c in cols])
            for r in self._results:
                w.writerow([r.get(c, "") for c in cols])
        QMessageBox.information(self, "导出完成", f"已导出到:\n{path}")


class _NteamThread(QThread):
    finished = pyqtSignal(object)

    def run(self):
        from wyckoff.nteam import track_nteam
        try:
            res = track_nteam(force=False)
        except Exception:
            import traceback
            traceback.print_exc()
            res = []
        self.finished.emit(res)


class NteamWindow(QDialog):
    """国家队 ETF 跟踪: 监测汇金/证金常用宽基 ETF 的主力资金异动。"""

    COL_CN = {"code": "代码", "name": "名称", "last": "现价", "pct": "涨跌%",
              "y1": "主力近1日(亿)", "y5": "主力近5日(亿)", "y20": "主力近20日(亿)",
              "ratio": "强度", "verdict": "信号", "source": "数据源"}

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("国家队ETF跟踪")
        self.resize(1120, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("国家队ETF跟踪"))
        self.head = QLabel("正在加载 ...")
        self.head.setStyleSheet("font-weight:bold;")
        root.addWidget(self.head)
        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)
        hb = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        hb.addWidget(btn)
        btn2 = QPushButton("加入自选股")
        btn2.clicked.connect(self._add_watch)
        hb.addWidget(btn2)
        note = QLabel("双击一行 → 对该ETF做威科夫分析; 资金流断连时自动用量价代理")
        note.setStyleSheet(f"color:{theme.C_MUTED};")
        hb.addWidget(note, 1)
        root.addLayout(hb)
        self._results = []
        self.refresh()

    def refresh(self):
        self.head.setText("正在加载 ...")
        self._th = _NteamThread(self)
        self._th.finished.connect(self._on_done)
        self._th.start()

    def _on_done(self, results):
        self._results = results
        from wyckoff.nteam import nteam_summary
        s = nteam_summary(results)
        self.head.setText(
            f"共 {s['total']} 只 · 疑似买入 {s['buy']} · 疑似减仓 {s['sell']} · "
            f"净流入 {s['inflow']} · 净流出 {s['outflow']} · 量价代理 {s['proxy']} · "
            f"数据断连 {s['down']}")
        rows = []
        for r in results:
            rows.append({
                "code": r["code"], "name": r["name"],
                "last": f"{r['price']:.3f}" if r["price"] else "-",
                "pct": f"{r['pct']:+.1f}%" if r["pct"] is not None else "-",
                "y1": f"{r['y1']:+.1f}" if r["y1"] is not None else "-",
                "y5": f"{r['y5']:+.1f}" if r["y5"] is not None else "-",
                "y20": f"{r['y20']:+.1f}" if r["y20"] is not None else "-",
                "ratio": f"{r['ratio']:.1f}" if r["ratio"] is not None else "-",
                "verdict": r["verdict"],
                "source": {"flow": "资金流", "proxy": "量价", "none": "无"}.get(
                    r.get("source"), "-"),
            })
        cols = ("code", "name", "last", "pct", "y1", "y5", "y20", "ratio",
                "verdict", "source")
        _fill(self.table, cols, rows)
        for c, w in (("code", 60), ("name", 150), ("last", 60), ("pct", 60),
                     ("y1", 80), ("y5", 80), ("y20", 86), ("ratio", 55),
                     ("verdict", 130), ("source", 60)):
            self.table.horizontalHeader().resizeSection(cols.index(c), w)
        from PyQt6.QtGui import QColor
        for ri, r in enumerate(results):
            it = self.table.item(ri, cols.index("verdict"))
            if it is None:
                continue
            if "买入" in r["verdict"]:
                it.setForeground(QColor(theme.C_UP))
            elif "减仓" in r["verdict"]:
                it.setForeground(QColor(theme.C_DOWN))
            elif r["verdict"] == "数据断连":
                it.setForeground(QColor(theme.C_MUTED))

    def _load_selected(self, _item):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if rows and rows[0] < len(self._results) and self.on_load:
            self.on_load(self._results[rows[0]]["code"])

    def _add_watch(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        codes = [self._results[i]["code"] for i in rows if i < len(self._results)]
        if not codes:
            QMessageBox.information(self, "加入自选股", "请先选择要加入的行")
            return
        watch = load_watchlist()
        added = [c for c in codes if c and c not in watch]
        if added:
            save_watchlist(watch + added)
            if getattr(self.parent(), "reload_watchlist", None):
                self.parent().reload_watchlist()
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "所选股票已在自选股中")


class _HoldingsThread(QThread):
    finished = pyqtSignal(object, object)

    def __init__(self, symbol, parent=None):
        super().__init__(parent)
        self._symbol = symbol

    def run(self):
        from wyckoff.holdings import fetch_nt_holdings
        try:
            res = fetch_nt_holdings(self._symbol, max_quarters=6)
        except Exception as e:
            res = None
            self.finished.emit(self._symbol, {"error": f"{e}"})
            return
        self.finished.emit(self._symbol, res)


class HoldingsWindow(QDialog):
    """国家队持仓透视: 十大股东中的汇金/证金/社保, 季度增减持 + 建仓成本估算。"""

    COLS = ("机构", "类型", "持股数", "占股本", "较上期", "状态",
            "估算成本", "现价", "盈亏", "建仓季度")

    def __init__(self, code, name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"国家队持仓透视 — {name or code}")
        self.resize(1500, 600)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header(f"国家队持仓透视 — {name or code}"))

        # 左右分栏: 左侧 = 数据表 + 摘要; 右侧 = AI 解读
        body = QHBoxLayout()
        body.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(6)

        self.status = QLabel("正在抓取十大股东 (季度数据) ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        left.addWidget(self.status)

        self.table = _table()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        left.addWidget(self.table, 1)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setMaximumHeight(140)
        left.addWidget(self.txt)

        hb = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        hb.addWidget(btn)
        hb.addStretch(1)
        left.addLayout(hb)

        body.addLayout(left, 1)

        self.ai_panel = _AiPanel("AI 解读 · 国家队持仓")
        self.ai_panel.setFixedWidth(380)
        body.addWidget(self.ai_panel, 0)
        root.addLayout(body, 1)

        from wyckoff.utils import normalize_symbol
        try:
            symbol = normalize_symbol(code)
        except ValueError:
            symbol = code
        self._symbol = symbol
        self._holders = []
        self.refresh()

    def refresh(self):
        self.status.setText("正在抓取十大股东 (季度数据) ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        self._th = _HoldingsThread(self._symbol, self)
        self._th.finished.connect(self._on_done)
        self._th.start()

    def _on_done(self, symbol, res):
        if not res:
            self.status.setText("加载失败")
            return
        if res.get("error"):
            self.status.setText(res["error"])
            self.status.setStyleSheet(f"color:{theme.C_UP};")
            return
        from wyckoff.holdings import format_shares
        holders = res["holders"]
        if holders:
            self.status.setText(f"最新报表期 {res['latest_report']} · 国家队持仓 {len(holders)} 家")
            self.status.setStyleSheet(f"color:{theme.C_DOWN};font-weight:bold;")
        else:
            self.status.setText(f"报表期 {res['latest_report'] or '无'} · "
                                "前十大股东中未发现国家队机构")
            self.status.setStyleSheet(f"color:{theme.C_MUTED};")
        self.table.setRowCount(len(holders))
        from PyQt6.QtGui import QColor
        for ri, h in enumerate(holders):
            cost = f"{h['cost']:.2f}" if h.get("cost") else "-"
            price = f"{h['price']:.2f}" if h.get("price") else "-"
            pnl = f"{h['pnl_pct']:+.1f}%" if h.get("pnl_pct") is not None else "-"
            if h.get("change_ratio") is not None:
                chg = f"{h['change_ratio']:+.2f}%"
            elif h["status"] == "新进":
                chg = "新进"
            else:
                chg = "-"
            vals = [h["name"], h["category"], format_shares(h["shares"]),
                    f"{h['pct']}%" if h.get("pct") is not None else "-",
                    chg, h["status"], cost, price, pnl, h["first_report"]]
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter
                                    if ci in (1, 5, 6, 7, 8) else
                                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(ri, ci, it)
            if h.get("pnl_pct") is not None:
                color = QColor(theme.C_UP if h["pnl_pct"] > 0 else theme.C_DOWN)
                for ci in (6, 7, 8):
                    it = self.table.item(ri, ci)
                    if it is not None:
                        it.setForeground(color)
            elif h["status"] == "新进":
                for ci in range(10):
                    it = self.table.item(ri, ci)
                    if it is not None:
                        it.setForeground(QColor(theme.C_ACCENT))
        for c, w in ((0, 210), (1, 60), (2, 95), (3, 70), (4, 80), (5, 55),
                     (6, 75), (7, 60), (8, 70), (9, 85)):
            self.table.horizontalHeader().resizeSection(c, w)
        lines = [f"最新报表期: {res['latest_report'] or '无'} · "
                 f"前十大股东中识别出国家队 {len(holders)} 家"]
        for h in holders:
            cost = f"{h['cost']:.2f}" if h.get("cost") else "-"
            price = f"{h['price']:.2f}" if h.get("price") else "-"
            pnl = f"{h['pnl_pct']:+.1f}%" if h.get("pnl_pct") is not None else "-"
            chg_txt = ("新进" if h["status"] == "新进" else
                       (f"较上期 {h['change_ratio']:+.2f}%" if h.get("change_ratio") is not None else "持平"))
            lines.append(f"  [{h['category']}] {h['name']} · {chg_txt} · "
                         f"建仓 {h['first_report']} · 成本 {cost} · 现价 {price} · 盈亏 {pnl}")
        if res.get("exited"):
            lines.append("已退出/跌出前十:")
            for e in res["exited"]:
                lines.append(f"  [{e['category']}] {e['name']} · 最近报表期 {e['last_report']}")
        # 盈亏汇总 + 极端盈亏提醒
        pnls = [h["pnl_pct"] for h in holders if h.get("pnl_pct") is not None]
        if pnls:
            import statistics as _st
            avg = _st.mean(pnls)
            up_n = sum(1 for p in pnls if p > 0)
            dn_n = sum(1 for p in pnls if p < 0)
            lines.append("")
            lines.append(f"盈亏汇总: {up_n} 家浮盈 / {dn_n} 家浮亏 · "
                         f"平均盈亏 {avg:+.1f}%")
            for h in holders:
                p = h.get("pnl_pct")
                if p is None:
                    continue
                if p <= -20:
                    lines.append(f"  ⚠ [{h['category']}] {h['name']} 深套 {p:+.1f}% "
                                 f"(成本 {h.get('cost'):.2f} vs 现价 {h.get('price'):.2f}), "
                                 f"建仓{h['first_report']}, 关注是否继续减仓")
                elif p >= 50:
                    lines.append(f"  ★ [{h['category']}] {h['name']} 浮盈 {p:+.1f}%, "
                                 f"注意高位派发风险 (建仓{h['first_report']})")
        self.txt.setPlainText("\n".join(lines))

        # AI 解读数据绑定
        self._holders = holders
        self._latest_report = res.get("latest_report")
        self._exited = res.get("exited", [])
        self.ai_panel.bind_prompt(
            lambda: _holdings_ai_prompt(self._symbol, self._holders,
                                        self._latest_report, self._exited))


class _EtfMonitorThread(QThread):
    finished = pyqtSignal(object)

    def run(self):
        from wyckoff.etf_factor import monitor_etfs
        try:
            res = monitor_etfs()
        except Exception:
            import traceback
            traceback.print_exc()
            res = []
        self.finished.emit(res)


class EtfMonitorWindow(QDialog):
    """ETF 三因子份额监测: 量能50% + 方向20% + 份额30%。"""

    COLS = ("名称", "代码", "现价", "涨跌", "量比", "份额1日", "份额5日",
            "ETF近5日", "基准近5日", "强度", "信号")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ETF 三因子份额监测")
        self.resize(1560, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("ETF 三因子份额监测"))

        # 左右分栏: 左侧 = 数据表; 右侧 = AI 解读
        body = QHBoxLayout()
        body.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(6)

        self.status = QLabel("正在抓取 ETF 份额与行情 (约 30~60 秒) ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        left.addWidget(self.status)

        self.table = _table()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        left.addWidget(self.table, 1)

        hb = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        hb.addWidget(btn)
        note = QLabel("三因子 = 量能50% + 方向20% + 份额30% "
                      "(≥0.7高确信 / 0.5~0.7中等 / <0.5正常)")
        note.setStyleSheet(f"color:{theme.C_MUTED};")
        hb.addWidget(note, 1)
        left.addLayout(hb)
        warn = QLabel("⚠ ETF 份额增加≠一定是国家队买入 (可能为机构/散户申购), 属概率性信号")
        warn.setStyleSheet(f"color:{theme.C_AMBER};")
        left.addWidget(warn)

        body.addLayout(left, 1)

        self.ai_panel = _AiPanel("AI 解读 · ETF 三因子")
        self.ai_panel.setFixedWidth(380)
        body.addWidget(self.ai_panel, 0)
        root.addLayout(body, 1)

        self._results = []
        self.refresh()

    def refresh(self):
        self.status.setText("正在抓取 ETF 份额与行情 (约 30~60 秒) ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        self._th = _EtfMonitorThread(self)
        self._th.finished.connect(self._on_done)
        self._th.start()

    def _on_done(self, results):
        self._results = results
        self.ai_panel.bind_prompt(lambda: _etf_ai_prompt(self._results))
        n_alert = sum(1 for r in results if r["signal"] != "正常" and r["signal"] != "数据不足")
        self.status.setText(f"共 {len(results)} 只宽基 ETF · 异常信号 {n_alert} 只")
        self.status.setStyleSheet(f"color:{theme.C_DOWN};font-weight:bold;")
        self.table.setRowCount(len(results))
        from PyQt6.QtGui import QColor
        for ri, r in enumerate(results):
            pct = f"{r['pct']:+.2f}%" if r.get("pct") is not None else "-"
            vr = f"{r['vol_ratio']:.2f}" if r.get("vol_ratio") is not None else "-"
            s1 = f"{r['share_1d']:+.2f}%" if r.get("share_1d") is not None else "-"
            s5 = f"{r['share_5d']:+.2f}%" if r.get("share_5d") is not None else "-"
            e5 = f"{r['etf_ret5']:+.2f}%" if r.get("etf_ret5") is not None else "-"
            b5 = f"{r['bench_ret5']:+.2f}%" if r.get("bench_ret5") is not None else "-"
            vals = (r["name"], r["symbol"], f"{r['price']:.3f}" if r.get("price") else "-",
                    pct, vr, s1, s5, e5, b5, f"{r['strength']:.2f}", r["signal"])
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter
                                    if ci in (1, 4, 9) else
                                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(ri, ci, it)
            if "买入" in r["signal"]:
                self.table.item(ri, 10).setForeground(QColor(theme.C_UP))
            elif "卖出" in r["signal"]:
                self.table.item(ri, 10).setForeground(QColor(theme.C_DOWN))
        for c, w in ((0, 130), (1, 70), (2, 60), (3, 60), (4, 50), (5, 70),
                     (6, 70), (7, 75), (8, 75), (9, 50), (10, 100)):
            self.table.horizontalHeader().resizeSection(c, w)


# ──────────────────────────────────────── 自选股预警管理 ────────────────────────────────────────

class AlertsWindow(QDialog):
    """自选股预警管理: 新增价格阈值/信号预警, 查看/删除已有规则。

    支持:
      - 价格阈值: 现价 突破(>=) / 跌破(<=) 目标价 触发
      - 信号预警: 出现指定威科夫信号类型 (Spring/ST/UTAD...) 触发
    """

    KINDS = {"price_up": "价格 ≥", "price_down": "价格 ≤", "signal": "出现信号"}

    def __init__(self, parent=None, on_trigger=None):
        super().__init__(parent)
        self.on_trigger = on_trigger
        self.setWindowTitle("自选股预警")
        self.resize(760, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("自选股预警"))

        # 新增规则行
        form = QWidget()
        fl = QHBoxLayout(form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        self.ed_code = QLineEdit()
        self.ed_code.setPlaceholderText("股票代码 (如 600104 / sh600104)")
        self.ed_code.setFixedWidth(130)
        fl.addWidget(self.ed_code)
        self.cb_kind = QComboBox()
        self.cb_kind.addItem("价格 ≥ (突破)", "price_up")
        self.cb_kind.addItem("价格 ≤ (跌破)", "price_down")
        self.cb_kind.addItem("出现信号", "signal")
        self.cb_kind.currentIndexChanged.connect(self._on_kind_changed)
        fl.addWidget(self.cb_kind)
        self.ed_target = QLineEdit()
        self.ed_target.setPlaceholderText("目标价 或 信号类型 (如 Spring)")
        self.ed_target.setFixedWidth(160)
        fl.addWidget(self.ed_target)
        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._add)
        fl.addWidget(btn_add)
        fl.addStretch(1)
        root.addWidget(form)

        self.table = _table()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["代码", "名称", "类型", "目标", "状态", "触发时间"])
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        for text, cb in (("删除选中", self._del), ("启用/停用", self._toggle),
                         ("刷新", self.refresh)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)
        self.refresh()

    def _on_kind_changed(self):
        kind = self.cb_kind.currentData()
        if kind == "signal":
            self.ed_target.setPlaceholderText("信号类型 (如 Spring)")
        else:
            self.ed_target.setPlaceholderText("目标价 (数字)")

    def _add(self):
        from wyckoff.alerts import add_alert, load_alerts
        from wyckoff.utils import normalize_symbol
        code = self.ed_code.text().strip()
        if not code:
            QMessageBox.information(self, "提示", "请填写股票代码")
            return
        try:
            code = normalize_symbol(code)[2:]
        except ValueError:
            QMessageBox.warning(self, "错误", f"无法识别的代码: {code}")
            return
        kind = self.cb_kind.currentData()
        target = self.ed_target.text().strip()
        if not target:
            QMessageBox.information(self, "提示", "请填写目标价或信号类型")
            return
        name = ""
        try:
            from wyckoff.datasource import fetch_name
            name = fetch_name("sh" + code if code.startswith("6") else
                              "sz" + code if code.startswith(("0", "3")) else code) or ""
        except Exception:
            pass
        ok = add_alert(code, kind, target, name=name)
        if ok:
            self.ed_code.clear()
            self.ed_target.clear()
            self.refresh()
        else:
            QMessageBox.information(self, "提示", "该预警规则已存在")

    def _del(self):
        from wyckoff.alerts import remove_alert
        removed = 0
        for idx in sorted({i.row() for i in self.table.selectedIndexes()},
                          reverse=True):
            code = self.table.item(idx, 0).text()
            kind = self.table.item(idx, 2).data(Qt.ItemDataRole.UserRole)
            target = self.table.item(idx, 3).text()
            remove_alert(code, kind, target)
            removed += 1
        if removed:
            self.refresh()

    def _toggle(self):
        from wyckoff.alerts import enable_alert
        for idx in {i.row() for i in self.table.selectedIndexes()}:
            code = self.table.item(idx, 0).text()
            kind = self.table.item(idx, 2).data(Qt.ItemDataRole.UserRole)
            target = self.table.item(idx, 3).text()
            on = not bool(self.table.item(idx, 4).data(Qt.ItemDataRole.UserRole))
            enable_alert(code, kind, target, on)
        self.refresh()

    def refresh(self):
        from wyckoff.alerts import load_alerts
        from datetime import datetime
        records = load_alerts()
        self.table.setRowCount(len(records))
        from PyQt6.QtGui import QColor
        for ri, r in enumerate(records):
            vals = [r.get("code"), r.get("name") or "-",
                    self.KINDS.get(r.get("kind"), r.get("kind")), r.get("target")]
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter
                                    if ci in (0, 2) else Qt.AlignmentFlag.AlignLeft)
                self.table.setItem(ri, ci, it)
            self.table.item(ri, 2).setData(Qt.ItemDataRole.UserRole, r.get("kind"))
            on = bool(r.get("enabled"))
            st = QTableWidgetItem("启用" if on else "已触发")
            st.setData(Qt.ItemDataRole.UserRole, on)
            st.setForeground(QColor(theme.C_DOWN if on else theme.C_MUTED))
            st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(ri, 4, st)
            ts = r.get("triggered_ts")
            ttxt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
            it = QTableWidgetItem(ttxt)
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(ri, 5, it)
        self.table.horizontalHeader().resizeSection(0, 90)
        self.table.horizontalHeader().resizeSection(1, 140)
        self.table.horizontalHeader().resizeSection(2, 90)
        self.table.horizontalHeader().resizeSection(3, 160)


# ──────────────────────────────────────── 我的持仓 (个人持仓簿) ────────────────────────────────────────

class _PortfolioRTThread(QThread):
    """后台拉取持仓实时行情。"""
    finished = pyqtSignal(object)

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = codes

    def run(self):
        from wyckoff.datasource import fetch_realtime
        try:
            out = fetch_realtime(self._codes) or {}
        except Exception:
            out = {}
        self.finished.emit(out)


class PortfolioWindow(QDialog):
    """我的持仓: 记录买入价/数量/成本, 结合实时价算盈亏与止损提醒。"""

    COLS = ("代码", "名称", "持股数", "成本价", "现价", "盈亏%", "盈亏额",
            "止损", "距止损%", "买入日期", "备注")

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("我的持仓")
        self.resize(1150, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("我的持仓 · 成本 / 盈亏 / 止损跟踪"))

        self.status = QLabel("加载持仓中...")
        self.status.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self.status)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        btn_add = QPushButton("新增持仓")
        btn_add.clicked.connect(self._add)
        btns.addWidget(btn_add)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._del)
        btns.addWidget(btn_del)
        btn_refresh = QPushButton("刷新行情")
        btn_refresh.clicked.connect(self.refresh)
        btns.addWidget(btn_refresh)
        btn_export = QPushButton("导出CSV")
        btn_export.clicked.connect(self._export_csv)
        btns.addWidget(btn_export)
        btns.addStretch(1)
        root.addLayout(btns)

        self.summary = QLabel()
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setStyleSheet(f"color:{theme.C_MUTED};")
        root.addWidget(self.summary)

        from wyckoff.storage import load_portfolio
        self._records = load_portfolio()
        self.refresh()

    def refresh(self):
        codes = [str(r["code"]) for r in self._records]
        if not codes:
            self._render()
            return
        self._rt_th = _PortfolioRTThread(codes, self)
        self._rt_th.finished.connect(self._on_rt)
        self._rt_th.start()

    def _on_rt(self, rt):
        self._last_rt = rt
        self._render()

    def _render(self):
        from PyQt6.QtGui import QColor
        self.table.setRowCount(len(self._records))
        rt = getattr(self, "_last_rt", {}) or {}
        total_cost = total_mv = 0.0
        for ri, r in enumerate(self._records):
            code = str(r["code"])
            shares = float(r.get("shares", 0) or 0)
            cost = float(r.get("cost", 0) or 0)
            stop = r.get("stop")
            info = rt.get(code) or {}
            price = float(info["price"]) if info.get("price") else None
            cost_amt = shares * cost
            mv = shares * price if price else None
            pnl_pct = (price / cost - 1) * 100 if (price and cost) else None
            pnl_amt = (mv - cost_amt) if (mv is not None) else None
            if cost_amt:
                total_cost += cost_amt
            if mv is not None:
                total_mv += mv

            name = r.get("name") or (info.get("name") or "")
            vals = [
                code, name,
                f"{shares:,.0f}" if shares else "-",
                f"{cost:.3f}" if cost else "-",
                f"{price:.3f}" if price else "-",
                f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-",
                f"{pnl_amt:+,.0f}" if pnl_amt is not None else "-",
                f"{stop:.3f}" if stop else "-",
                f"{(price / stop - 1) * 100:+.1f}%" if (price and stop) else "-",
                r.get("buy_date") or "-",
                r.get("note") or "",
            ]
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter if ci in (0, 2, 3, 4, 5, 6, 7, 8, 9)
                                    else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(ri, ci, it)
            # 盈亏着色
            if pnl_pct is not None:
                color = QColor(theme.C_UP if pnl_pct >= 0 else theme.C_DOWN)
                for ci in (5, 6):
                    it = self.table.item(ri, ci)
                    if it is not None:
                        it.setForeground(color)
            # 接近止损提醒
            if price and stop:
                dist = (price / stop - 1) * 100
                if dist <= 3:
                    it = self.table.item(ri, 8)
                    if it is not None:
                        it.setForeground(QColor(theme.C_AMBER))
        for c, w in ((0, 80), (1, 120), (2, 70), (3, 70), (4, 70), (5, 75),
                     (6, 85), (7, 70), (8, 80), (9, 90), (10, 150)):
            self.table.horizontalHeader().resizeSection(c, w)
        if self._records:
            tot_pnl = total_mv - total_cost
            tot_pct = tot_pnl / total_cost * 100 if total_cost else 0.0
            up = sum(1 for r in self._records
                     if (lambda p: p and p > 0)(self._last_pct(r)))
            dn = len(self._records) - up
            self.status.setText(f"持仓 {len(self._records)} 只 · {up} 盈 / {dn} 亏")
            self.status.setStyleSheet(f"color:{theme.C_DOWN};font-weight:bold;")
            color = theme.C_UP if tot_pnl >= 0 else theme.C_DOWN
            self.summary.setText(
                f"总成本 {total_cost:,.0f} · 总市值 {total_mv:,.0f} · "
                f"<span style='color:{color};'>总盈亏 {tot_pnl:+,.0f} "
                f"({tot_pct:+.2f}%)</span>")
            self.summary.setTextFormat(Qt.TextFormat.RichText)
        else:
            self.status.setText("暂无持仓, 点击『新增持仓』记录")
            self.status.setStyleSheet(f"color:{theme.C_MUTED};")
            self.summary.setText("")

    def _last_pct(self, r):
        code = str(r["code"])
        info = (getattr(self, "_last_rt", {}) or {}).get(code) or {}
        price = float(info["price"]) if info.get("price") else None
        cost = float(r.get("cost", 0) or 0)
        return (price / cost - 1) if (price and cost) else None

    def _add(self):
        from PyQt6.QtWidgets import QDialogButtonBox, QFormLayout, QLineEdit, QDialog
        dlg = QDialog(self)
        dlg.setWindowTitle("新增持仓")
        form = QFormLayout(dlg)
        ed_code = QLineEdit()
        ed_code.setPlaceholderText("股票代码")
        form.addRow("代码:", ed_code)
        ed_shares = QLineEdit()
        ed_shares.setPlaceholderText("持股数量 (股)")
        form.addRow("数量:", ed_shares)
        ed_cost = QLineEdit()
        ed_cost.setPlaceholderText("买入均价")
        form.addRow("成本价:", ed_cost)
        ed_date = QLineEdit()
        ed_date.setPlaceholderText("YYYY-MM-DD (可选)")
        form.addRow("买入日期:", ed_date)
        ed_stop = QLineEdit()
        ed_stop.setPlaceholderText("止损价 (可选)")
        form.addRow("止损价:", ed_stop)
        ed_note = QLineEdit()
        ed_note.setPlaceholderText("备注 (可选)")
        form.addRow("备注:", ed_note)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        code = ed_code.text().strip()
        if not code:
            return
        from wyckoff.utils import normalize_symbol
        try:
            code = normalize_symbol(code)[2:]
        except ValueError:
            QMessageBox.warning(self, "错误", f"无法识别的代码: {code}")
            return
        try:
            shares = float(ed_shares.text().strip())
            cost = float(ed_cost.text().strip())
        except ValueError:
            QMessageBox.warning(self, "错误", "数量与成本价需为数字")
            return
        stop_txt = ed_stop.text().strip()
        stop = float(stop_txt) if stop_txt else None
        name = ""
        try:
            from wyckoff.datasource import fetch_name
            name = fetch_name("sh" + code if code.startswith("6") else
                              "sz" + code if code.startswith(("0", "3")) else code) or ""
        except Exception:
            pass
        from wyckoff.storage import load_portfolio, save_portfolio
        recs = load_portfolio()
        recs.append({"code": code, "name": name, "shares": shares, "cost": cost,
                     "stop": stop, "buy_date": ed_date.text().strip(),
                     "note": ed_note.text().strip(), "created_ts": __import__("time").time()})
        save_portfolio(recs)
        self._records = load_portfolio()
        self.refresh()

    def _del(self):
        from wyckoff.storage import save_portfolio
        idxs = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for i in idxs:
            self._records.pop(i)
        save_portfolio(self._records)
        self.refresh()

    def _load_selected(self, item):
        if self.on_load:
            code = self.table.item(item.row(), 0).text()
            self.on_load(code)

    def _export_csv(self):
        from PyQt6.QtWidgets import QFileDialog
        import csv, os
        d = QFileDialog.getSaveFileName(self, "导出持仓 CSV", "wx_portfolio.csv",
                                        "CSV (*.csv)")[0]
        if not d:
            return
        rt = getattr(self, "_last_rt", {}) or {}
        rows = []
        for r in self._records:
            code = str(r["code"])
            info = rt.get(code) or {}
            price = float(info["price"]) if info.get("price") else ""
            cost = float(r.get("cost", 0) or 0)
            shares = float(r.get("shares", 0) or 0)
            pnl = (price / cost - 1) * 100 if (price and cost) else ""
            rows.append([code, r.get("name") or "", shares, cost, price, pnl,
                         r.get("stop") or "", r.get("buy_date") or "",
                         r.get("note") or ""])
        with open(d, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["代码", "名称", "持股数", "成本价", "现价", "盈亏%",
                         "止损", "买入日期", "备注"])
            wr.writerows(rows)
        QMessageBox.information(self, "导出完成", f"已导出持仓到:\n{d}")


# ──────────────────────────────────────── 自选股备注 ────────────────────────────────────────

class NotesWindow(QDialog):
    """自选股备注/笔记: 每只票记录买卖理由、观察要点。"""

    def __init__(self, code, name, parent=None):
        super().__init__(parent)
        self._code = code
        self.setWindowTitle(f"个股备注 — {name or code}")
        self.resize(520, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header(f"个股备注 — {name or code}"))

        self.txt = QTextEdit()
        self.txt.setPlaceholderText(
            "记录这只票的观察要点 / 买入理由 / 卖出计划 / 复盘心得...\n"
            "保存后存在本地 ~/.wyckoff/wx_notes.json, 随自选股随时查看。")
        root.addWidget(self.txt, 1)

        from wyckoff.storage import load_notes
        notes = load_notes()
        self.txt.setPlainText(notes.get(code, ""))

        hb = QHBoxLayout()
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        hb.addWidget(btn_save)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        hb.addWidget(btn_close)
        hb.addStretch(1)
        root.addLayout(hb)

    def _save(self):
        from wyckoff.storage import load_notes, save_notes
        notes = load_notes()
        notes[self._code] = self.txt.toPlainText().strip()
        save_notes(notes)
        self.statusBar_save_done()

    def statusBar_save_done(self):
        QMessageBox.information(self, "已保存", f"已保存 {self._code} 的备注。")


# ──────────────────────────────────────── 多股票对比 ────────────────────────────────────────

class _CompareScanThread(QThread):
    """后台扫描多只股票并汇总阶段/信号/评分。"""
    finished = pyqtSignal(object)

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self._codes = codes

    def run(self):
        from wyckoff.backtest import scan_stock_signals
        from wyckoff.utils import normalize_symbol
        out = []
        for c in self._codes:
            try:
                sym = normalize_symbol(c)
                r = scan_stock_signals(c, datalen=500, confirm_enabled=False)
                if r:
                    r["code"] = sym[2:]
                    out.append(r)
            except Exception:
                continue
        self.finished.emit(out)


class CompareWindow(QDialog):
    """多股票对比: 并排显示阶段 / 信号 / 方向 / 现价 / 相对强弱。"""

    def __init__(self, codes, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        self.setWindowTitle("多股票对比")
        self.resize(1080, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(_accent_header("多股票对比 · 阶段 / 信号 / 方向"))

        self.status = QLabel(f"正在对比 {len(codes)} 只股票 ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        root.addWidget(self.status)

        self.table = _table()
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        hb = QHBoxLayout()
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        hb.addWidget(btn)
        note = QLabel("双击任意行 → 加载该股分析")
        note.setStyleSheet(f"color:{theme.C_MUTED};")
        hb.addWidget(note, 1)
        root.addLayout(hb)

        self._codes = codes
        self.refresh()

    def refresh(self):
        self.status.setText(f"正在对比 {len(self._codes)} 只股票 ...")
        self.status.setStyleSheet(f"color:{theme.C_AMBER};font-weight:bold;")
        self._th = _CompareScanThread(self._codes, self)
        self._th.finished.connect(self._on_done)
        self._th.start()

    def _on_done(self, results):
        self.status.setText(f"对比完成 · {len(results)} 只")
        self.status.setStyleSheet(f"color:{theme.C_DOWN};font-weight:bold;")
        cols = ["代码", "名称", "现价", "阶段", "信号", "评分"]
        self.table.setColumnCount(len(cols))
        self.table.setRowCount(len(results))
        self.table.setHorizontalHeaderLabels(cols)
        from PyQt6.QtGui import QColor
        for ri, r in enumerate(results):
            code = r.get("code", "")
            signals = ", ".join(r.get("signals") or []) or "-"
            vals = [code, r.get("name") or "", f"{r['last']:.2f}" if r.get("last") else "-",
                    r.get("phase") or "-", signals,
                    f"{r.get('score', 0):+.1f}" if r.get("score") is not None else "-"]
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter
                                    if ci in (0, 2, 5) else Qt.AlignmentFlag.AlignLeft)
                self.table.setItem(ri, ci, it)
            # 阶段着色
            phase = r.get("phase") or ""
            tone = QColor(theme.C_UP if "底部" in phase or "上升" in phase else
                          theme.C_DOWN if "顶部" in phase or "下跌" in phase else
                          theme.C_TEXT)
            it = self.table.item(ri, 3)
            if it is not None:
                it.setForeground(tone)
            # 信号着色 (出现强信号高亮)
            sc = r.get("score")
            it = self.table.item(ri, 5)
            if it is not None and sc is not None:
                it.setForeground(QColor(theme.C_UP if sc > 0 else
                                        theme.C_DOWN if sc < 0 else theme.C_TEXT))
        for c, w in ((0, 70), (1, 130), (2, 70), (3, 170), (4, 260), (5, 60)):
            self.table.horizontalHeader().resizeSection(c, w)

    def _load_selected(self, item):
        if self.on_load:
            code = self.table.item(item.row(), 0).text()
            self.on_load(code)


# ──────────────────────────── 综合选股 ────────────────────────────

class _ScreenerThread(QThread):
    """后台综合选股线程: 获取选股宇宙 + 并行批量评分 + 严格无果自动放宽。

    放宽策略 (依次尝试, 复用内存/磁盘缓存, 二次扫描秒级):
      1) 严格过滤 → 2) 忽略阶段 → 3) 再忽略 信号/板块/最低分 (保留数值条件)。
    公开 batch 信号带 relaxed 标记, 供界面提示"已放宽"。
    每次运行会把宇宙来源/条件/结果摘要写入 wx_debug.log 便于诊断。
    """
    status = pyqtSignal(str)  # 状态消息
    progress = pyqtSignal(int, int, str)  # done, total, current_code
    batch = pyqtSignal(object, int, int, int, float, bool)  # results, done, total, errors, elapsed, relaxed
    finished = pyqtSignal()

    def __init__(self, filters, parent=None):
        super().__init__(parent)
        self._codes = None
        self._filters = filters
        self._cancel_event = threading.Event()

    def cancel(self):
        """请求尽早停止评分 (进行中的请求会自然结束)。"""
        self._cancel_event.set()

    def _build_universe(self):
        """动态成交额排名 → 离线抽样 → 内置静态, 返回 (codes, source_label)。"""
        from wyckoff.fundamental import fetch_market_universe, local_universe
        from wyckoff.backtest import MARKET_UNIVERSE
        n = int(self._filters.get("universe_size", 100) or 100)
        try:
            codes = fetch_market_universe(n)
            if codes:
                return codes, f"动态{len(codes)}"
            codes = local_universe(n)
            if codes:
                return codes, f"离线{len(codes)}"
        except Exception:
            pass
        return MARKET_UNIVERSE, f"内置{len(MARKET_UNIVERSE)}"

    def run(self):
        from wyckoff.screener import screen_stocks
        from wyckoff._log import log_msg
        start = time.time()
        self.status.emit("正在获取选股宇宙 ...")
        try:
            self._codes, src = self._build_universe()
        except Exception:
            import traceback
            traceback.print_exc()
            from wyckoff.backtest import MARKET_UNIVERSE
            self._codes, src = MARKET_UNIVERSE, "内置(异常)"
        total = len(self._codes)
        if not total or self._cancel_event.is_set():
            self.batch.emit([], 0, 0, 0, time.time() - start, False)
            self.finished.emit()
            return
        self.status.emit(f"选股宇宙就绪: {src} 只候选, 并行评分中 ...")
        # 执行选股 (并行) + 严格无果自动放宽
        errors = [0]
        done_n = [0]

        def on_progress(done, total, code):
            done_n[0] = done
            self.progress.emit(done, total, code)

        def on_error(code):
            errors[0] += 1

        def _score(filters, silent=False):
            try:
                return screen_stocks(
                    self._codes, filters,
                    on_progress=(None if silent else on_progress),
                    workers=10, cancel_event=self._cancel_event, on_error=on_error)
            except Exception as e:
                log_msg("综合选股", f"screen_stocks 异常: {e}")
                return []

        results = _score(self._filters)
        relaxed = False
        if not results and not self._cancel_event.is_set():
            # 严格/数值条件之外无结果 → 依次放宽阶段 → 信号/板块/最低分
            f2 = dict(self._filters)
            if f2.get("phases"):
                f2["phases"] = None
                cand = _score(f2, silent=True)
                if cand:
                    results, relaxed = cand, True
            if not results and not self._cancel_event.is_set():
                f3 = dict(f2)
                f3["signals"] = None
                f3["sector"] = None
                f3["min_score"] = 0
                cand = _score(f3, silent=True)
                if cand:
                    results, relaxed = cand, True
        el = time.time() - start
        log_msg(
            "综合选股",
            f"宇宙={src} filters={self._filters}\n"
            f"入选={len(results)} 错误={errors[0]} 放宽={relaxed} 耗时={el:.0f}s")
        self.batch.emit(results, done_n[0], total, errors[0], el, relaxed)
        self.finished.emit()


class ScreenerWidget(QWidget):
    """综合选股组件: 威科夫阶段 + 基本面 + 资金流 + 技术指标 多维评分。

    可嵌入主窗口标签页。
    支持预设策略 (价值吸筹/强势突破/超跌反弹/小盘成长/主力抢筹)
    和自定义筛选条件 (范围/市值/PE/PB/阶段/信号/板块/最低总分), 并行后台
    扫描 (可取消), 双击加载分析, 可批量加入自选股/待观察。
    """

    COL_CN = {
        "total_score": "总分", "code": "代码", "name": "名称", "last": "现价",
        "phase": "阶段", "signals": "信号", "tech_score": "技术分",
        "flow_score": "资金分", "fund_score": "基本面分", "pe": "PE",
        "pb": "PB", "mcap_yi": "市值(亿)", "sector": "板块",
    }

    def __init__(self, parent=None, on_load=None):
        super().__init__(parent)
        self.on_load = on_load
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # ── 筛选条件面板 (卡片样式) ──
        filter_box = QWidget()
        filter_box.setStyleSheet(
            f"QWidget#filterBox{{background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER};border-radius:8px;}}")
        filter_box.setObjectName("filterBox")
        filter_lay = QVBoxLayout(filter_box)
        filter_lay.setContentsMargins(14, 10, 14, 10)
        filter_lay.setSpacing(8)

        # 第一行: 预设 + 数值筛选 + 排序
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        row1.addWidget(QLabel("预设:"))
        self.cb_preset = QComboBox()
        self.cb_preset.setMinimumWidth(100)
        from wyckoff.screener import list_presets
        presets = list_presets()
        self.cb_preset.addItem("自定义")
        for p in presets:
            self.cb_preset.addItem(p["name"], p["key"])
        self.cb_preset.currentIndexChanged.connect(self._on_preset_changed)
        row1.addWidget(self.cb_preset)

        sep = QLabel("│")
        sep.setStyleSheet(f"color:{theme.C_BORDER};")
        row1.addWidget(sep)

        # 市值
        row1.addWidget(QLabel("市值(亿):"))
        self.sp_mcap_min = QDoubleSpinBox()
        self.sp_mcap_min.setRange(0, 999999)
        self.sp_mcap_min.setDecimals(0)
        self.sp_mcap_min.setSpecialValueText("不限")
        self.sp_mcap_min.setFixedWidth(70)
        row1.addWidget(self.sp_mcap_min)
        row1.addWidget(QLabel("~"))
        self.sp_mcap_max = QDoubleSpinBox()
        self.sp_mcap_max.setRange(0, 999999)
        self.sp_mcap_max.setDecimals(0)
        self.sp_mcap_max.setSpecialValueText("不限")
        self.sp_mcap_max.setFixedWidth(70)
        row1.addWidget(self.sp_mcap_max)

        # PE
        row1.addWidget(QLabel("PE:"))
        self.sp_pe_min = QDoubleSpinBox()
        self.sp_pe_min.setRange(-999, 9999)
        self.sp_pe_min.setDecimals(0)
        self.sp_pe_min.setSpecialValueText("不限")
        self.sp_pe_min.setFixedWidth(70)
        row1.addWidget(self.sp_pe_min)
        row1.addWidget(QLabel("~"))
        self.sp_pe_max = QDoubleSpinBox()
        self.sp_pe_max.setRange(-999, 9999)
        self.sp_pe_max.setDecimals(0)
        self.sp_pe_max.setSpecialValueText("不限")
        self.sp_pe_max.setFixedWidth(70)
        row1.addWidget(self.sp_pe_max)

        # PB
        row1.addWidget(QLabel("PB:"))
        self.sp_pb_min = QDoubleSpinBox()
        self.sp_pb_min.setRange(0, 999)
        self.sp_pb_min.setDecimals(1)
        self.sp_pb_min.setSpecialValueText("不限")
        self.sp_pb_min.setFixedWidth(70)
        row1.addWidget(self.sp_pb_min)
        row1.addWidget(QLabel("~"))
        self.sp_pb_max = QDoubleSpinBox()
        self.sp_pb_max.setRange(0, 999)
        self.sp_pb_max.setDecimals(1)
        self.sp_pb_max.setSpecialValueText("不限")
        self.sp_pb_max.setFixedWidth(70)
        row1.addWidget(self.sp_pb_max)

        row1.addStretch(1)

        # 排序
        row1.addWidget(QLabel("排序:"))
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(["综合评分", "技术面", "资金流", "基本面"])
        self.cb_sort.setMinimumWidth(90)
        row1.addWidget(self.cb_sort)

        filter_lay.addLayout(row1)

        # 第二行: 阶段筛选
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("阶段:"))
        self.chk_phase_acc = QCheckBox("吸筹")
        self.chk_phase_acc.setChecked(True)
        self.chk_phase_markup = QCheckBox("上升")
        self.chk_phase_markup.setChecked(True)
        self.chk_phase_dist = QCheckBox("派发")
        self.chk_phase_dist.setChecked(False)
        self.chk_phase_down = QCheckBox("下跌")
        self.chk_phase_down.setChecked(False)
        self.chk_phase_range = QCheckBox("区间")
        self.chk_phase_range.setChecked(True)
        for chk in (self.chk_phase_acc, self.chk_phase_markup,
                    self.chk_phase_dist, self.chk_phase_down, self.chk_phase_range):
            row2.addWidget(chk)
        sep2 = QLabel("│")
        sep2.setStyleSheet(f"color:{theme.C_BORDER};")
        row2.addWidget(sep2)
        # 信号筛选
        row2.addWidget(QLabel("信号:"))
        self.chk_sig_spring = QCheckBox("Spring")
        self.chk_sig_sc = QCheckBox("SC")
        self.chk_sig_st = QCheckBox("ST")
        self.chk_sig_sos = QCheckBox("SOS")
        self.chk_sig_psy = QCheckBox("PSY")
        for chk in (self.chk_sig_spring, self.chk_sig_sc, self.chk_sig_st,
                    self.chk_sig_sos, self.chk_sig_psy):
            row2.addWidget(chk)
        row2.addStretch(1)
        filter_lay.addLayout(row2)

        # 第三行: 范围 / 最低总分 / 板块 / 结果数
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        row3.addWidget(QLabel("范围:"))
        self.cb_universe = QComboBox()
        self.cb_universe.addItem("活跃A股 Top100", 100)
        self.cb_universe.addItem("活跃A股 Top200", 200)
        self.cb_universe.addItem("活跃A股 Top300", 300)
        self.cb_universe.setMinimumWidth(110)
        row3.addWidget(self.cb_universe)
        sep3 = QLabel("│")
        sep3.setStyleSheet(f"color:{theme.C_BORDER};")
        row3.addWidget(sep3)
        row3.addWidget(QLabel("最低总分:"))
        self.sp_min_score = QSpinBox()
        self.sp_min_score.setRange(0, 100)
        self.sp_min_score.setValue(0)
        self.sp_min_score.setPrefix("≥")
        self.sp_min_score.setFixedWidth(76)
        row3.addWidget(self.sp_min_score)
        row3.addWidget(QLabel("板块:"))
        self.ed_sector = QLineEdit()
        self.ed_sector.setPlaceholderText("逗号分隔, 如 银行,半导体")
        self.ed_sector.setClearButtonEnabled(True)
        row3.addWidget(self.ed_sector, 1)
        row3.addWidget(QLabel("结果数:"))
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(10, 200)
        self.sp_limit.setValue(50)
        self.sp_limit.setFixedWidth(76)
        row3.addWidget(self.sp_limit)
        filter_lay.addLayout(row3)

        root.addWidget(filter_box)

        # ── 操作按钮行 ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_start = QPushButton("开始选股")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setStyleSheet(
            f"QPushButton{{background:{theme.C_ACCENT};color:#fff;"
            f"font-weight:bold;padding:6px 20px;border-radius:6px;"
            f"border:none;}}"
            f"QPushButton:hover{{background:{theme.C_ACCENT};opacity:0.9;}}"
            f"QPushButton:disabled{{background:{theme.C_MUTED};color:#fff;}}")
        self.btn_start.clicked.connect(self._on_start)
        btn_row.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(
            f"QPushButton{{background:{theme.C_PANEL};color:{theme.C_TEXT};"
            f"padding:6px 16px;border-radius:6px;border:1px solid {theme.C_BORDER};}}"
            f"QPushButton:hover{{background:{theme.C_BORDER};}}")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.btn_cancel)
        self.prog = QLabel("")
        self.prog.setStyleSheet(
            f"color:{theme.C_MUTED};font-size:11pt;padding:0 8px;")
        btn_row.addWidget(self.prog, 1)
        root.addLayout(btn_row)

        # ── 进度条 ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(20)
        self.progress_bar.setStyleSheet(
            f"QProgressBar{{border:1px solid {theme.C_BORDER};border-radius:6px;"
            f"background:{theme.C_PANEL};text-align:center;color:{theme.C_TEXT};"
            f"font-size:10pt;}}"
            f"QProgressBar::chunk{{background:{theme.C_ACCENT};border-radius:4px;}}")
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # ── 结果表 ──
        self.table = _table()
        self.table.setSortingEnabled(True)
        self.table.itemDoubleClicked.connect(self._load_selected)
        root.addWidget(self.table, 1)

        # ── 底部操作 ──
        hb = QHBoxLayout()
        hb.setSpacing(8)
        for text, cb in (("全选", self._select_all), ("加入自选股", self._add_watch),
                         ("存为待观察", self._save_candidates), ("导出CSV", self._export)):
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:{theme.C_PANEL};color:{theme.C_TEXT};"
                f"padding:5px 14px;border-radius:5px;"
                f"border:1px solid {theme.C_BORDER};}}"
                f"QPushButton:hover{{background:{theme.C_BORDER};}}")
            b.clicked.connect(cb)
            hb.addWidget(b)
        hb.addStretch(1)
        root.addLayout(hb)

        self._results = []
        self._thread = None

    def _collect_filters(self):
        """收集界面筛选条件 → dict。"""
        sort_map = {"综合评分": "total_score", "技术面": "tech_score",
                    "资金流": "flow_score", "基本面": "fund_score"}
        phases = []
        if self.chk_phase_acc.isChecked():
            phases.append("底部整固")
        if self.chk_phase_markup.isChecked():
            phases.append("上升趋势")
        if self.chk_phase_dist.isChecked():
            phases.append("顶部构筑")
        if self.chk_phase_down.isChecked():
            phases.append("下跌趋势")
        if self.chk_phase_range.isChecked():
            phases.append("区间整理")
        signals = []
        if self.chk_sig_spring.isChecked():
            signals.append("Spring")
        if self.chk_sig_sc.isChecked():
            signals.append("SC")
        if self.chk_sig_st.isChecked():
            signals.append("ST")
        if self.chk_sig_sos.isChecked():
            signals.append("SOS")
        if self.chk_sig_psy.isChecked():
            signals.append("PSY")
        f = {
            "phases": phases or None,
            "signals": signals or None,
            "signals_mode": "any",  # 勾选信号 = 硬过滤 (任一命中才入选)
            "universe_size": self.cb_universe.currentData() or 100,
            "sort_by": sort_map.get(self.cb_sort.currentText(), "total_score"),
            "limit": self.sp_limit.value(),
        }
        ms = self.sp_min_score.value()
        if ms > 0:
            f["min_score"] = ms
        raw = self.ed_sector.text().replace("，", ",").replace("　", ",").replace(" ", ",")
        sector = [s.strip() for s in raw.split(",") if s.strip()]
        if sector:
            f["sector"] = sector
        mcmin = self.sp_mcap_min.value()
        mcmax = self.sp_mcap_max.value()
        if mcmin > 0:
            f["mcap_min"] = mcmin
        if mcmax > 0:
            f["mcap_max"] = mcmax
        pemin = self.sp_pe_min.value()
        pemax = self.sp_pe_max.value()
        if pemin > -999:
            f["pe_min"] = pemin
        if pemax < 9999:
            f["pe_max"] = pemax
        pbmin = self.sp_pb_min.value()
        pbmax = self.sp_pb_max.value()
        if pbmin > 0:
            f["pb_min"] = pbmin
        if pbmax < 999:
            f["pb_max"] = pbmax
        return f

    def _on_preset_changed(self, idx):
        """切换预设策略 → 填充筛选条件。"""
        if idx <= 0:
            return
        key = self.cb_preset.currentData()
        from wyckoff.screener import get_preset
        preset = get_preset(key)
        if not preset:
            return
        pf = preset["filters"]
        # 重置所有控件
        self.chk_phase_acc.setChecked(False)
        self.chk_phase_markup.setChecked(False)
        self.chk_phase_dist.setChecked(False)
        self.chk_phase_down.setChecked(False)
        self.chk_phase_range.setChecked(False)
        self.chk_sig_spring.setChecked(False)
        self.chk_sig_sc.setChecked(False)
        self.chk_sig_st.setChecked(False)
        self.chk_sig_sos.setChecked(False)
        self.chk_sig_psy.setChecked(False)
        self.sp_mcap_min.setValue(0)
        self.sp_mcap_max.setValue(0)
        self.sp_pe_min.setValue(-999)
        self.sp_pe_max.setValue(9999)
        self.sp_pb_min.setValue(0)
        self.sp_pb_max.setValue(999)
        # 设置阶段
        for p in pf.get("phases", []):
            if p == "底部整固":
                self.chk_phase_acc.setChecked(True)
            elif p == "上升趋势":
                self.chk_phase_markup.setChecked(True)
            elif p == "顶部构筑":
                self.chk_phase_dist.setChecked(True)
            elif p == "下跌趋势":
                self.chk_phase_down.setChecked(True)
            elif p == "区间整理":
                self.chk_phase_range.setChecked(True)
        # 设置信号
        sig_map = {
            "Spring": self.chk_sig_spring, "SC": self.chk_sig_sc,
            "ST": self.chk_sig_st, "SOS": self.chk_sig_sos, "PSY": self.chk_sig_psy,
        }
        for s in pf.get("signals", []):
            if s in sig_map:
                sig_map[s].setChecked(True)
        # 设置数值条件
        if pf.get("mcap_max"):
            self.sp_mcap_max.setValue(pf["mcap_max"])
        if pf.get("pe_max") is not None:
            self.sp_pe_max.setValue(pf["pe_max"])
        if pf.get("pb_max") is not None:
            self.sp_pb_max.setValue(pf["pb_max"])
        # 设置排序
        sort_rev = {"total_score": 0, "tech_score": 1, "flow_score": 2, "fund_score": 3}
        idx_s = sort_rev.get(pf.get("sort_by", "total_score"), 0)
        self.cb_sort.setCurrentIndex(idx_s)

    def _on_start(self):
        """开始选股: 获取宇宙 → 后台线程并行评分。"""
        if self._thread and self._thread.isRunning():
            return
        self.btn_start.setEnabled(False)
        self.btn_start.setText("选股中...")
        self.btn_cancel.show()
        self.prog.setText("正在获取选股宇宙 ...")
        self.table.setRowCount(0)
        self._results = []
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        QTimer.singleShot(50, self._run_screen)

    def _on_cancel(self):
        """取消进行中的选股 (保留已完成的 partial 结果)。"""
        if self._thread and self._thread.isRunning():
            self._thread.cancel()
            self.prog.setText("正在取消 ...")

    def _run_screen(self):
        filters = self._collect_filters()
        self._thread = _ScreenerThread(filters, self)
        self._thread.status.connect(self._on_status)
        self._thread.progress.connect(self._on_progress)
        self._thread.batch.connect(self._on_batch)
        self._thread.finished.connect(self._on_done)
        self._thread.start()

    def _on_status(self, msg):
        """状态更新: 显示当前阶段。"""
        self.prog.setText(msg)

    def _on_progress(self, done, total, code):
        """进度更新: 更新进度条和文字。"""
        pct = int(done / total * 100) if total > 0 else 0
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(done)
        self.prog.setText(f"分析中 {done}/{total} · {code}")

    def _on_batch(self, results, done, total, errors, elapsed, relaxed):
        self._results = results
        self.progress_bar.setValue(done)
        self._scan_info = (total, errors)
        cancelled = bool(self._thread and self._thread._cancel_event.is_set())
        prefix = "已取消 · " if cancelled else ""
        relax_note = " 已按放宽条件(放开阶段/信号)重扫" if relaxed and results else ""
        self.prog.setText(f"{prefix}分析完成 · {len(results)} 只入选 / {total} 只候选"
                          f" · {errors} 只跳过 · 耗时 {elapsed:.0f}s{relax_note}")
        self._fill_table()

    def _on_done(self):
        self.btn_start.setEnabled(True)
        self.btn_start.setText("开始选股")
        self.btn_cancel.hide()
        self.progress_bar.hide()
        if not self._results:
            cancelled = bool(self._thread and self._thread._cancel_event.is_set())
            if cancelled:
                self.prog.setText("已取消")
            else:
                total, errors = getattr(self, "_scan_info", (0, 0))
                self.prog.setText(
                    f"无符合条件股票 · 已分析 {total} 只"
                    " · 建议: 勾选'区间'阶段 / 放宽PE/PB市值 / 勾选更多信号 / 调大范围")
        self.table.setSortingEnabled(True)

    def _fill_table(self):
        cols = ("total_score", "code", "name", "last", "phase", "signals",
                "tech_score", "flow_score", "fund_score", "pe", "pb",
                "mcap_yi", "sector")
        rows = []
        for r in self._results:
            sig = "+".join(r.get("signals") or []) or "-"
            pe_val = r.get("pe") or 0
            pb_val = r.get("pb") or 0
            mc_val = r.get("mcap_yi") or 0
            name_s = r.get("name") or "-"
            rows.append({
                "total_score": r.get("total_score", 0),
                "code": r.get("code", ""), "name": name_s,
                "last": r.get("last") or 0,
                "phase": r.get("phase") or "-", "signals": sig,
                "tech_score": r.get("tech_score", 0),
                "flow_score": r.get("flow_score", 0),
                "fund_score": r.get("fund_score", 0),
                "pe": pe_val, "pb": pb_val, "mcap_yi": mc_val,
                "sector": r.get("sector") or "-",
            })
        _fill(self.table, cols, rows)
        # 列宽: 关键列设置最小宽度 + 自适应内容, 板块列 Stretch 填充剩余空间
        header = self.table.horizontalHeader()
        # 最小宽度映射 (实测文字宽+padding16+排序图标20, 留余量)
        # 2字=24px→60, 3字=36px→75, 4字=48px→85, 市值(亿)=44px→85
        min_widths = {"total_score": 60, "code": 75, "name": 95, "last": 70,
                      "phase": 140, "signals": 160, "tech_score": 75,
                      "flow_score": 75, "fund_score": 85, "pe": 55,
                      "pb": 55, "mcap_yi": 85, "sector": 110}
        for c, w in min_widths.items():
            if c in cols:
                idx = cols.index(c)
                header.resizeSection(idx, w)
        # 板块列设为 Stretch (填充剩余空间), 其余列 Interactive
        sector_idx = cols.index("sector") if "sector" in cols else -1
        for i in range(len(cols)):
            if i == sector_idx:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)

    def _selected_rows(self):
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _selected_codes(self):
        cols = ("total_score", "code", "name", "last", "phase", "signals",
                "tech_score", "flow_score", "fund_score", "pe", "pb",
                "mcap_yi", "sector")
        code_col = cols.index("code")
        codes = []
        for idx in self._selected_rows():
            it = self.table.item(idx, code_col)
            if it is not None and it.text() and it.text() != "-":
                codes.append(it.text())
        return codes

    def _load_selected(self, _item):
        codes = self._selected_codes()
        if codes and self.on_load:
            self.on_load(codes[0])

    def _select_all(self):
        self.table.selectAll()

    def _add_watch(self):
        codes = self._selected_codes()
        if not codes:
            QMessageBox.information(self, "加入自选股", "请先选择要加入的行")
            return
        watch = load_watchlist()
        added = [c for c in codes if c and c not in watch]
        if added:
            save_watchlist(watch + added)
            mw = self.window()
            if hasattr(mw, "reload_watchlist"):
                mw.reload_watchlist()
            QMessageBox.information(self, "加入自选股", f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "所选股票已在自选股中")

    def _save_candidates(self):
        codes = self._selected_codes()
        if not codes:
            QMessageBox.information(self, "存为待观察", "请先选择要观察的行")
            return
        from datetime import datetime
        recs = load_candidates()
        have = {r["code"] for r in recs}
        new = 0
        for c in codes:
            r = next((x for x in self._results if x.get("code") == c), None)
            if r is None:
                continue
            if r["code"] in have:
                recs = [x for x in recs if x["code"] != r["code"]]
            recs.insert(0, {
                "code": r["code"], "name": r.get("name", ""),
                "score": r.get("total_score", 0),
                "phase": r.get("phase", ""),
                "conf_q": r.get("conf_q", ""),
                "signals": "+".join(r.get("signals") or []),
                "sector": r.get("sector", ""),
                "sector20": r.get("sector20"),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            new += 1
        save_candidates(recs)
        QMessageBox.information(self, "存为待观察", f"已存 {new} 只到待观察清单")

    def _export(self):
        if not self._results:
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        path = os.path.join(DATA_DIR, f"wx_screener_{ts}.csv")
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "现价", "阶段", "信号", "总分",
                        "技术分", "资金分", "基本面分", "PE", "PB",
                        "市值(亿)", "板块"])
            for r in self._results:
                w.writerow([
                    r.get("code", ""), r.get("name", ""),
                    f"{r['last']:.2f}" if r.get("last") else "",
                    r.get("phase", ""),
                    "+".join(r.get("signals") or []),
                    f"{r.get('total_score', 0):.0f}",
                    f"{r.get('tech_score', 0):.0f}",
                    f"{r.get('flow_score', 0):.0f}",
                    f"{r.get('fund_score', 0):.0f}",
                    f"{r['pe']:.1f}" if r.get("pe") and r["pe"] > 0 else "",
                    f"{r['pb']:.2f}" if r.get("pb") and r["pb"] > 0 else "",
                    f"{r['mcap_yi']:.0f}" if r.get("mcap_yi") else "",
                    r.get("sector") or "",
                ])
        QMessageBox.information(self, "导出完成", f"已导出到:\n{path}")



