"""分节列表: 分析结论左侧的节标题导航列表。"""
from PyQt6.QtWidgets import QAbstractItemView, QListWidget


class SectionList(QListWidget):
    """与主窗口原内联实现同款: objectName=sectionList 走全局 QSS。"""

    def __init__(self, width=150, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionList")
        self.setFixedWidth(width)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
