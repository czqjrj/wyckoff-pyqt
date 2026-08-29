"""AI 问股对话框: 基于当前分析报告的多轮对话窗口。

与主界面"AI解读"标签的关系:
  - AI解读 = 一次性生成的整篇译文 (interpret.interpret_report);
  - AI问股 = 带上下文的追问窗口 (wyckoff.ai_chat.ChatSession),
    系统提示已注入当前报告 + 该标的历史信号实证, 可连续追问
    "为什么看多 / 换个周期呢 / 止损怎么设" 而不必重新生成全文。

工程约束:
  - API 调用走 QThread 后台线程, 不阻塞 UI (与 _LabelAiThread 同模式);
  - 无 Key / 会话不可用 → 输入禁用 + 占位提示, 不报错弹窗;
  - 失败/退化回答不写入对话记录, 只在状态行提示可重试。
"""
import html

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout

from . import theme


class _AskThread(QThread):
    """后台执行一次 ask() (网络调用 60s 超时, 不能卡 UI)。"""
    result = pyqtSignal(object)

    def __init__(self, session, question, parent=None):
        super().__init__(parent)
        self._session = session
        self._question = question

    def run(self):
        try:
            out = self._session.ask(self._question)
        except Exception:
            out = None
        self.result.emit(out)


class AiChatDialog(QDialog):
    """多轮 AI 问股窗口。用法:
        dlg = AiChatDialog(self, settings, system_context, title="AI 问股 · 600519")
        dlg.exec()
    """

    def __init__(self, parent, settings, system_context, title="AI 问股"):
        super().__init__(parent)
        from wyckoff.ai_chat import ChatSession
        self.setWindowTitle(title)
        self.resize(560, 640)
        self._session = ChatSession(settings, system_context)
        self._thread = None
        self._pending_q = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.view = QTextBrowser(self)
        self.view.setOpenExternalLinks(False)
        lay.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("追问当前分析, 回车发送 (如: 为什么看多? 止损怎么设?)")
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        self.clear_btn = QPushButton("清空对话")
        self.clear_btn.setToolTip("清空多轮历史 (保留报告上下文)")
        self.clear_btn.clicked.connect(self._clear)
        row.addWidget(self.clear_btn)
        lay.addLayout(row)

        if not self._session.ok:
            hint = ("AI 问股不可用: 请先在 设置→AI 中配置 API Key "
                    "(无需打开自动解读开关)。")
            self._append_system(hint)
            self.input.setEnabled(False)
            self.send_btn.setEnabled(False)

    # ── 渲染 ──
    def _append_msg(self, who, text):
        color = theme.C_UP if who == "你" else theme.C_TEXT
        body = html.escape(text or "").replace("\n", "<br>")
        self.view.append(
            f'<div style="margin-top:8px;"><b style="color:{color};">{who}:</b>'
            f'<span style="color:{theme.C_TEXT};">{body}</span></div>')

    def _append_system(self, text):
        self.view.append(
            f'<div style="margin-top:8px;color:{theme.C_MUTED};">'
            f'{html.escape(text)}</div>')

    # ── 交互 ──
    def _send(self):
        q = self.input.text().strip()
        if not q or not self._session.ok:
            return
        if self._thread is not None and self._thread.isRunning():
            return
        self._append_msg("你", q)
        self.input.clear()
        self.input.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.send_btn.setText("思考中...")
        self._pending_q = q
        self._thread = _AskThread(self._session, q, self)
        self._thread.result.connect(self._on_answer)
        self._thread.start()

    def _on_answer(self, ans):
        if getattr(self, "_thread", None) is not None:
            self._thread.result.disconnect(self._on_answer)
            self._thread = None
        self._pending_q = None
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.input.setFocus()
        if ans:
            self._append_msg("AI", ans)
        else:
            self._append_system("本次回答失败或为空 (网络/模型错误), 请重试或换个问法。")

    def _clear(self):
        self._session.reset()
        self.view.clear()

    def keyPressEvent(self, ev: QKeyEvent):  # noqa: N802 (Qt 命名)
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and self.input.hasFocus():
            self._send()
            return
        super().keyPressEvent(ev)
