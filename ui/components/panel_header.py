"""面板标题栏: 强调色竖条 + 标题 + 右侧动作区。"""
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .. import theme


class PanelHeader(QWidget):
    """用法: header = PanelHeader('信号汇总'); header.add_action(btn)。"""

    def __init__(self, title="", parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(2, theme.spacing("1"), 2, 2)
        h.setSpacing(theme.spacing("1") + 2)

        strip = QFrame()
        strip.setFixedSize(5, 16)
        strip.setAutoFillBackground(True)
        sp = strip.palette()
        sp.setColor(QPalette.ColorRole.Window, QColor(theme.C_ACCENT))
        strip.setPalette(sp)
        h.addWidget(strip)
        self._strip = strip

        lab = QLabel(title)
        lab.setObjectName("panelHead")
        h.addWidget(lab)
        h.addStretch(1)
        self._label = lab

    def label(self):
        return self._label

    def apply_theme(self):
        """主题切换后重刷强调色条 (调色板构造期烧入, 需显式刷新)。"""
        sp = self._strip.palette()
        sp.setColor(QPalette.ColorRole.Window, QColor(theme.C_ACCENT))
        self._strip.setPalette(sp)

    def add_action(self, w):
        """把动作控件插入 stretch 之前 (右侧动作区)。"""
        self.layout().insertWidget(self.layout().count() - 1, w)
        return w

    def add_stretch(self, stretch=1):
        self.layout().addStretch(stretch)
