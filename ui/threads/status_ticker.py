"""状态栏滚动头条: 一组短消息横向滚动/逐条轮播。

单条消息过长时在限定宽度内横向滚动 (marquee); 多条消息按序停留轮播。
点按整条横幅把当前消息对应的标的载入分析 (消息带 code 时)。

性能注意: set_messages / add_messages 自带 80ms debounce 合并, 避免
后台线程高频率刷新触发 UI 动画连续重建进而把状态栏滚动画卡。
"""
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import QLabel

from ui import theme
from wyckoff.config import (
    EVENT_BEAR,
    EVENT_BULL,
    TICKER_MAX_ITEMS,
    TICKER_MIN_VSA_WINRATE,
    TICKER_MIN_WINRATE,
    TICKER_ROT_MS,
    TICKER_SCROLL_MS,
    TICKER_SCROLL_SPEED,
    VSA_BEAR,
    VSA_BULL,
)


class StatusTicker(QLabel):
    clicked_code = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self.setMinimumWidth(160)
        self.setToolTip("点击载入当前标的分析")
        self._msgs = []            # [(text, color, code)]
        self._cur = 0
        self._off = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(TICKER_SCROLL_MS)
        self._timer.timeout.connect(self._step)
        self._rot = QTimer(self)
        self._rot.setSingleShot(True)
        self._rot.timeout.connect(self._advance)
        # debounce: 1 帧内多次 set/add 合并应用
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(80)
        self._debounce.timeout.connect(self._flush_pending)
        self._pending_set = None  # None / list
        self._pending_add = []    # 累积待合并的新增

    # ── 消息管理 ──
    def set_messages(self, msgs):
        """msgs: [(text, color, code), ...]; 空则清空停止动画。带 80ms debounce。"""
        self._pending_set = list(msgs or [])[:TICKER_MAX_ITEMS]
        self._pending_add = []  # set 会覆盖掉 add 缓存
        if not self._debounce.isActive():
            self._debounce.start()

    def add_messages(self, msgs):
        """把新消息合并进现有头条 (按文本去重, 新的在前)。带 debounce, 1帧内批量合并。"""
        msgs = list(msgs or [])
        if not msgs:
            return
        self._pending_add = list(msgs) + list(self._pending_add)
        if not self._debounce.isActive():
            self._debounce.start()

    def _flush_pending(self):
        """实际应用 pending_set / pending_add。同一次 debounce 窗口里两者都有的话, 先 set 再 add (正确语义)。"""
        applied = False
        if self._pending_set is not None:
            msgs = self._pending_set
            self._pending_set = None
            self._apply_messages(msgs, reset=True)
            applied = True
        if self._pending_add:
            adds = self._pending_add
            self._pending_add = []
            self._apply_messages(adds, reset=False)
            applied = True
        return applied

    def _apply_messages(self, msgs, reset: bool):
        if reset:
            self._msgs = list(msgs or [])[:TICKER_MAX_ITEMS]
            self._cur = 0
            self._off = 0.0
            self._rot.stop()
            if not self._msgs:
                self._timer.stop()
                self.setText("")
                self.update()
                return
            if not self._timer.isActive():
                self._timer.start()
            self.update()
            if len(self._msgs) > 1:
                self._rot.start(TICKER_ROT_MS)
            return
        # add_messages: 合并到现有 _msgs (按文本去重, 新在前)
        if not msgs:
            return
        seen = set()
        merged = []
        for m in msgs + self._msgs:
            if not m or not m[0]:
                continue
            if m[0] in seen:
                continue
            seen.add(m[0])
            merged.append(m)
        self._msgs = merged[:TICKER_MAX_ITEMS]
        if not self._timer.isActive():
            self._timer.start()
        self._cur = 0
        self._off = 0.0
        self._rot.start(TICKER_ROT_MS)
        self.update()

    def clear(self):
        self.set_messages([])

    def flush_now(self):
        """同步入口: 立即触发 debounce flush, 确保 set/add 的消息落到 _msgs。

        供单元测试/需要立即读 _msgs 的场景用, 避免等 80ms 定时器。
        生产 UI 路径不要调, 让 debounce 自然合并节流。
        """
        if self._debounce.isActive():
            self._debounce.stop()
        self._flush_pending()

    def current_code(self):
        if not self._msgs:
            return ""
        return self._msgs[self._cur][2] or ""

    # ── 动画 ──
    def _step(self):
        self._off += TICKER_SCROLL_SPEED
        self.update()

    def _advance(self):
        if len(self._msgs) > 1:
            self._cur = (self._cur + 1) % len(self._msgs)
        self._off = 0.0
        self._rot.start(TICKER_ROT_MS)
        self.update()

    def paintEvent(self, _ev):
        if not self._msgs:
            super().paintEvent(_ev)
            return
        text, color, _code = self._msgs[self._cur]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor(color))
        fm = QFontMetrics(self.font())
        tw = fm.horizontalAdvance(text)
        w = self.width()
        if tw <= w - 8:
            x = max(0.0, (w - tw) / 2)
        else:
            span = tw + 24
            cycle = span + w
            x = -(self._off % cycle)
        text_y = (self.height() - fm.ascent() - fm.descent()) / 2 + fm.ascent()
        painter.drawText(int(x), int(text_y), text)
        painter.end()

    def mousePressEvent(self, _ev):
        code = self.current_code()
        if code:
            self.clicked_code.emit(code)
        super().mousePressEvent(_ev)

    def enterEvent(self, ev):
        """鼠标悬停 → 暂停横向滚动与轮播, 方便看清/点击。"""
        self._timer.stop()
        self._rot.stop()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        """鼠标移开 → 恢复滚动。"""
        if self._msgs:
            if not self._timer.isActive():
                self._timer.start()
            if len(self._msgs) > 1 and not self._rot.isActive():
                self._rot.start(TICKER_ROT_MS)
        super().leaveEvent(ev)


def signal_color(kind, sig_type, theme_module=None):
    """按信号方向取状态栏着色 (A股红涨绿跌; 中性用琥珀)。"""
    tm = theme_module or theme
    if kind == "event":
        if sig_type in EVENT_BULL:
            return tm.C_UP
        if sig_type in EVENT_BEAR:
            return tm.C_DOWN
    else:  # vsa
        if sig_type in VSA_BULL:
            return tm.C_UP
        if sig_type in VSA_BEAR:
            return tm.C_DOWN
    return tm.C_AMBER


def build_ticker_msgs(rich_by_code, min_winrate=None,
                      min_vsa_winrate=None,
                      max_items=None):
    """从扫描结果筛选高实测命中信号, 生成滚动头条消息。

    rich_by_code: {code: scan_stock_signals 结果 dict} (含 events/vsa 明细)。
    仅保留实测胜率 (贝叶斯收缩) ≥ 对应阈值 (事件用 min_winrate, VSA 用
    min_vsa_winrate) 的事件/VSA 标签, 按胜率降序, 每只股票最多 2 条
    (事件+各取最高命中 VSA), 总条数限 max_items。
    返回 [(text, color, code), ...]。
    """
    if min_winrate is None:
        min_winrate = TICKER_MIN_WINRATE
    if min_vsa_winrate is None:
        min_vsa_winrate = TICKER_MIN_VSA_WINRATE
    if max_items is None:
        max_items = TICKER_MAX_ITEMS
    from wyckoff.signal_accuracy import win_rate_of
    scored = []
    for code, r in (rich_by_code or {}).items():
        name = r.get("name") or code
        tag = str(code)[-6:]
        hits = []
        for e in r.get("events") or []:
            t = e["type"]
            wr = win_rate_of("event", t, 20)
            if wr >= min_winrate:
                hits.append((wr, f"{name}({tag}) {t} 实测命中{wr * 100:.0f}%",
                             "event", t))
        for s in r.get("vsa") or []:
            lb = s["label"]
            wr = win_rate_of("vsa", lb, 20)
            if wr >= min_vsa_winrate:
                hits.append((wr, f"{name}({tag}) VSA-{lb} 实测命中{wr * 100:.0f}%",
                             "vsa", lb))
        hits.sort(key=lambda x: -x[0])
        scored.extend((wr, text, signal_color(kind, sig), code)
                      for wr, text, kind, sig in hits[:2])
    scored.sort(key=lambda x: -x[0])
    return [(text, color, code) for _wr, text, color, code in scored[:max_items]]
