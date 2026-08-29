"""结果表格窗口基类: 此前 extra_windows 15 个 QDialog 复制同一骨架
(标题条+说明+表格+双击加载+加入自选股/存为待观察/导出CSV+进度行)。

子类只需声明 COLS 并调用 set_rows(); 双击发 code_requested 信号。
列定义: COLS = [(key, 标题, 宽) 或 (key, 标题, 宽, fmt)]
  fmt 可为 格式字符串 (".2f"/".0f"/"pct1"...) 或 callable(value, row)->str;
  值为 None/"" 时显示 "-"。EXPORT_HEADERS 缺省由 COLS 派生 (key→中文)。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import theme
from .panel_header import PanelHeader


def make_table(select_rows=True):
    """与 extra_windows._table 同规格的主题化表格工厂。"""
    t = QTableWidget()
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection
                       if select_rows else
                       QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setDefaultSectionSize(28)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setProperty("themedRole", "table")
    t.setStyleSheet(f"QTableWidget{{background:{theme.C_PANEL};"
                    f"border:1px solid {theme.C_BORDER};}}"
                    f"QTableWidget::item:selected{{background:{theme.C['sel']};}}")
    return t


_FMT_PCT1 = "{:.1f}"


def _default_fmt(v):
    return "" if v is None else str(v)


class ResultsTableDialog(QDialog):
    TITLE = ""
    PREFIX = "results"
    COLS = ()
    EXPORT_HEADERS = None       # [(key, 中文)]; None → 由 COLS 派生
    SHOW_ADD_WATCH = True
    SHOW_SAVE_CANDIDATES = False

    code_requested = pyqtSignal(str)

    def __init__(self, parent=None, title=None, description=""):
        super().__init__(parent)
        self._rows = []
        if title:
            self.setWindowTitle(title)
        self.resize(920, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(PanelHeader(title or self.TITLE))
        self.head = QLabel(description)
        self.head.setStyleSheet(f"color:{theme.C_MUTED};")
        self.head.setWordWrap(True)
        root.addWidget(self.head)

        self.table = make_table()
        keys = [c[0] for c in self.COLS]
        self.table.setColumnCount(len(keys))
        self.table.setHorizontalHeaderLabels([c[1] for c in self.COLS])
        for i, c in enumerate(self.COLS):
            if len(c) > 2 and isinstance(c[2], (int, float)):
                self.table.setColumnWidth(i, int(c[2]))
        self.table.itemDoubleClicked.connect(self._on_double_clicked)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        if self.SHOW_ADD_WATCH:
            b = QPushButton("加入自选股")
            b.clicked.connect(self.add_watch_selected)
            btns.addWidget(b)
        if self.SHOW_SAVE_CANDIDATES:
            b = QPushButton("存为待观察")
            b.clicked.connect(self.save_candidates_selected)
            btns.addWidget(b)
        b_exp = QPushButton("导出CSV")
        b_exp.clicked.connect(self.export_csv)
        btns.addWidget(b_exp)
        btns.addStretch(1)
        self.prog = QLabel("")
        self.prog.setStyleSheet(f"color:{theme.C_MUTED};")
        btns.addWidget(self.prog, 1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        root.addLayout(btns)
        self.btn_row = btns     # 子类可插入自定义动作按钮

    # ── 数据 ──
    def set_rows(self, rows):
        self._rows = list(rows or [])
        self._refill()

    def _cell_value(self, col_spec, row):
        key = col_spec[0]
        fmt = col_spec[3] if len(col_spec) > 3 else None
        v = row.get(key, "") if isinstance(row, dict) else None
        if v is None or v == "":
            return "-"
        if callable(fmt):
            try:
                return str(fmt(v, row))
            except Exception:
                return str(v)
        if isinstance(fmt, str):
            try:
                if fmt == "pct1":
                    return _FMT_PCT1.format(v)
                return f"{v:{fmt}}"
            except Exception:
                return str(v)
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    def _refill(self):
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(self._rows))
            for ri, r in enumerate(self._rows):
                for ci, col in enumerate(self.COLS):
                    it = QTableWidgetItem(self._cell_value(col, r))
                    if ci == 0:
                        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    color = self.row_color(r)
                    if color and col[0] == self.color_col():
                        it.setForeground(QColor(color))
                    self.table.setItem(ri, ci, it)
        finally:
            self.table.setUpdatesEnabled(True)

    # ── 子类可覆盖的钩子 ──
    def row_color(self, row):
        """返回该行的强调色 (None=默认); 配合 color_col() 指定着色列。"""
        return None

    def color_col(self):
        return self.COLS[0][0] if self.COLS else ""

    # ── 内置动作 ──
    def selected_codes(self, col=0):
        codes = []
        for it in self.table.selectedItems():
            r, c = it.row(), it.column()
            if c != col:
                continue
            v = self.table.item(r, col)
            if v is not None:
                codes.append(v.text().strip())
        seen, out = set(), []
        for c in codes:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _on_double_clicked(self, _item):
        codes = self.selected_codes()
        if codes:
            self.code_requested.emit(codes[0])

    def add_watch_selected(self):
        from wyckoff.storage import load_watchlist, save_watchlist
        codes = self.selected_codes()
        added = [c for c in codes if c and c not in load_watchlist()]
        if added:
            save_watchlist(load_watchlist() + added)
            QMessageBox.information(self, "加入自选股",
                                    f"已添加 {len(added)} 只到自选股")
        else:
            QMessageBox.information(self, "加入自选股", "未选中可添加的股票")

    def candidate_signals(self, row):
        """行 → 待观察记录的信号字段 (缺省取 signals 列拼接)。"""
        s = row.get("signals", "")
        return "+".join(s) if isinstance(s, list) else str(s or "")

    def save_candidates_selected(self):
        from datetime import datetime

        from wyckoff.storage import load_candidates, save_candidates
        sel = set(self.selected_codes())
        if not sel:
            QMessageBox.information(self, "存为待观察", "未选中任何股票")
            return
        recs = load_candidates()
        have = {r.get("code") for r in recs}
        new = 0
        for r in self._rows:
            code = str(r.get("code", ""))
            if code not in sel or code in have:
                continue
            recs.insert(0, {
                "code": code, "name": r.get("name", ""),
                "score": r.get("score", 0), "phase": r.get("phase", ""),
                "conf_q": r.get("conf_q", ""),
                "signals": self.candidate_signals(r),
                "sector": r.get("sector", ""), "sector20": r.get("sector20"),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
            have.add(code)
            new += 1
        save_candidates(recs)
        QMessageBox.information(self, "存为待观察",
                                f"已存 {new} 只到待观察清单")

    def export_rows(self):
        """导出的数据源 (缺省当前表内行); 子类可映射字段后返回。"""
        return self._rows

    def export_csv(self, prefix=None, headers=None, rows=None):
        from .components.csv_export import export_results_csv
        headers = headers or self.EXPORT_HEADERS or \
            [(c[0], c[1]) for c in self.COLS]
        return export_results_csv(self, prefix or self.PREFIX, headers,
                                  rows if rows is not None else self.export_rows())
