"""流式布局: 控件按行排列, 宽度不足自动换行 (信号汇总卡片等响应式场景)。

Qt 官方 examples/widgets/layouts/flowlayout 的 PySide6 移植, 按 token 化间距改造。
"""
from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    """margin/h_spacing/v_spacing 单位为像素 (传 theme.spacing('n') 即可)。"""

    def __init__(self, parent=None, margin=0, h_spacing=6, v_spacing=6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        else:
            self.setContentsMargins(0, 0, 0, 0)
        self._h_sp = h_spacing
        self._v_sp = v_spacing
        self._items = []

    def __del__(self):
        while self._items:
            self._items.pop(0)

    # ── QLayout 接口 ──
    def addItem(self, item):
        self._items.append(item)

    def addWidget(self, w):
        # QLayout.addWidget 的默认实现会调 addChildWidget 把控件挂到 parentWidget,
        # 这里被覆盖后必须手动补上, 否则控件无父级 → 不显示
        self.addChildWidget(w)
        self.addItem(_WidgetItem(w))
        self.update()  # 触发 invalidate → LayoutRequest → 重新布局

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        if 0 <= i < len(self._items):
            return self._items[i]
        return None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do_layout(QRect(0, 0, w, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def spacing(self):
        return self._h_sp

    def set_spacing(self, h, v=None):
        self._h_sp = h
        self._v_sp = v if v is not None else h
        self.update()

    # ── 核心: 逐行摆放 ──
    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_sp
            if next_x - self._h_sp > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_sp
                next_x = x + hint.width() + self._h_sp
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class _WidgetItem(QLayoutItem):
    """PyQt6 无公开 QWidgetItem 构造, 用子类包装真实控件。

    sizeHint/minimumSize 需按 QWidgetItem 的语义用 min/max 尺寸收口,
    否则 setFixedSize/setFixedHeight 会被忽略 (行高塌陷、卡片互相重叠)。
    """

    def __init__(self, widget: QWidget):
        super().__init__()
        self._w = widget

    def _clamped(self, s: QSize) -> QSize:
        w = self._w
        s = s.expandedTo(w.minimumSizeHint())
        s = s.boundedTo(w.maximumSize())
        s = s.expandedTo(w.minimumSize())
        return s

    def sizeHint(self):
        return self._clamped(self._w.sizeHint())

    def minimumSize(self):
        return self._clamped(self._w.minimumSizeHint())

    def maximumSize(self):
        return self._w.maximumSize()

    def expandingDirections(self):
        return self._w.expandingDirections()

    def setGeometry(self, rect):
        self._w.setGeometry(rect)

    def geometry(self):
        return self._w.geometry()

    def isEmpty(self):
        return self._w.isHidden()

    def widget(self):
        return self._w

    def spacerItem(self):
        return None

    def layout(self):
        return None

    def invalidate(self):
        pass
