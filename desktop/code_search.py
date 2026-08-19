# -*- coding: utf-8 -*-
"""键盘精灵 (股票代码快速搜索)。

对应 legacy wx 版的 "键盘精灵": 在非输入控件上敲字母/数字 → 弹出
悬浮搜索窗, 输入代码/名称/拼音即可定位并分析股票。
- 输入框打字 (防抖 300ms) → 后台线程 search_stock → 结果列表
- Enter 选中分析, Esc 关闭, 上下键移动
- 非模态悬浮窗 (StayOnTop), 可复用
"""
from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QVBoxLayout,
)

from wyckoff.pinyin import search_stock

from . import theme


class _SearchThread(QThread):
    result = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def __init__(self, query, parent=None):
        super().__init__(parent)
        self._query = query

    def run(self):
        try:
            res = search_stock(self._query, limit=20)
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.result.emit(self._query, res)


class CodeSearchDialog(QDialog):
    """键盘精灵悬浮窗。picked(code) 在用户确认选择时发出。"""

    picked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setMinimumSize(400, 260)
        self._results = []
        self._running_threads = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame(self)
        head.setStyleSheet(
            f"background:{theme.C_ACCENT};border:none;border-radius:6px 6px 0 0;")
        hh = QHBoxLayout(head)
        hh.setContentsMargins(10, 5, 10, 5)
        ti = QLabel("键盘精灵", head)
        ti.setStyleSheet(f"color:#ffffff;font-weight:bold;font-size:12pt;"
                         f"background:transparent;")
        hh.addWidget(ti)
        tip = QLabel("输入代码/名称/拼音  (Esc 关闭)", head)
        tip.setStyleSheet(f"color:rgba(255,255,255,200);font-size:10pt;"
                          f"background:transparent;")
        hh.addWidget(tip)
        hh.addStretch(1)
        root.addWidget(head)

        body = QFrame(self)
        body.setStyleSheet(
            f"background:{theme.C_PANEL};border:1px solid {theme.C_ACCENT};"
            f"border-top:none;border-radius:0 0 6px 6px;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(8)

        self.entry = QLineEdit(body)
        self.entry.setStyleSheet(
            f"background:{theme.C_BG};color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER};border-radius:4px;"
            f"padding:5px 8px;font-size:13pt;")
        bl.addWidget(self.entry)

        self.result_list = QListWidget(body)
        self.result_list.setStyleSheet(
            f"background:{theme.C_BG};color:{theme.C_TEXT};"
            f"border:1px solid {theme.C_BORDER};border-radius:4px;font-size:12pt;")
        bl.addWidget(self.result_list, 1)
        root.addWidget(body)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._do_search)

        self.entry.textChanged.connect(self._on_query_changed)
        self.entry.returnPressed.connect(self._confirm)
        self.result_list.itemDoubleClicked.connect(lambda _i: self._confirm())

        self.setStyleSheet(f"QDialog{{background:{theme.C_PANEL};}}")

    # ── 对外 ──
    def open_with(self, text=""):
        """显示并填入初始文本 (聚焦输入框)。"""
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
        self.entry.setText(text)
        self.entry.setFocus()
        self.entry.setCursorPosition(len(text))
        if text:
            self._on_query_changed(text)

    # ── 搜索 ──
    def _on_query_changed(self, text):
        self._timer.stop()
        if not text.strip():
            self.result_list.clear()
            self._results = []
            return
        self._timer.start()

    def _do_search(self):
        q = self.entry.text().strip()
        if not q:
            return
        # 只保留最新一次搜索的结果, 但已启动的搜索线程不能被销毁
        # (QThread 运行中销毁 → SIGABRT / "Destroyed while thread is still running")。
        # 线程不挂父节点, 由 _running_threads 持有引用直到 finished, 避免对话框销毁连带销毁运行中的线程。
        th = _SearchThread(q)
        th.result.connect(self._on_result)
        th.failed.connect(self._on_search_failed)
        self._running_threads.append(th)
        th.finished.connect(
            lambda t=th: self._running_threads.remove(t)
            if t in self._running_threads else None)
        th.start()

    def _on_result(self, query, results):
        if self.entry.text().strip() != query:
            return
        self._results = results
        self.result_list.clear()
        for r in results:
            code = r.get("code", "")
            name = r.get("name", "")
            item = QListWidgetItem(f"{name}  {code}")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.result_list.addItem(item)
        if self.result_list.count():
            self.result_list.setCurrentRow(0)

    def _on_search_failed(self, err):
        """搜索异常 (网络/接口) → 列表给出提示, 而不是静默空结果。"""
        if not self.entry.text().strip():
            return
        self.result_list.clear()
        self._results = []
        item = QListWidgetItem("搜索失败, 请检查网络后重试")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.result_list.addItem(item)

    # ── 交互 ──
    def _confirm(self):
        item = self.result_list.currentItem()
        code = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if not code and self._results:
            code = self._results[0].get("code", "")
        if code:
            self.picked.emit(code)
            self.hide()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.hide()
            ev.accept()
            return
        if ev.key() == Qt.Key.Key_Return and self.result_list.hasFocus():
            self._confirm()
            ev.accept()
            return
        super().keyPressEvent(ev)
