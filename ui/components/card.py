"""卡片容器: 统一面板/圆角/边框观感 (objectName 供 QSS 或内联样式定制)。"""
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from .. import theme


class Card(QFrame):
    """基础卡片: panel 背景 + 边框 + 圆角。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet(
            f"QFrame#card {{ background:{theme.C_PANEL};"
            f"border:1px solid {theme.C_BORDER};"
            f"border-radius:{theme.radius('md')}px; }}")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(theme.spacing("2"), theme.spacing("2"),
                                     theme.spacing("2"), theme.spacing("2"))
        self._lay.setSpacing(theme.spacing("1"))

    def add_widget(self, w):
        self._lay.addWidget(w)
        return w

    def add_layout(self, lay):
        self._lay.addLayout(lay)
        return lay

    @property
    def layout_(self):
        return self._lay


class CardContent(QWidget):
    """卡片内容区: 垂直布局的默认容器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.spacing("1"))
        self._lay = lay

    def add_widget(self, w, stretch=0):
        self._lay.addWidget(w, stretch)
        return w


class CardFooter(QWidget):
    """卡片底部操作区: 一排按钮靠左, 右侧弹性留白。"""

    def __init__(self, actions=None, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.spacing("1"))
        for w in (actions or []):
            h.addWidget(w)
        h.addStretch(1)
